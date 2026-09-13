"""Static input planning only; these tests never run target Python source."""
import hashlib
import json
import sys
import unittest
from unittest import mock

from forge8.experiments import prepare_input_search, prepare_inputs


def raw(args, kwargs=None):
    return json.dumps({"args": args, "kwargs": kwargs or {}}, ensure_ascii=True).encode()


class InputSearchTests(unittest.TestCase):
    def test_seed_exact_and_plan_identity(self):
        seed = b'{ "kwargs":{}, "args": [9007199254740993] }\n'
        plan = prepare_input_search(seed)
        self.assertEqual(plan, prepare_input_search(seed))
        self.assertEqual(plan["seed_input_text"], seed.decode())
        self.assertEqual(plan["inputs"][0], {"input_text": seed.decode(), "location": "seed"})
        content = {key: value for key, value in plan.items() if key != "sha256"}
        digest = hashlib.sha256(json.dumps(content, ensure_ascii=True, allow_nan=False,
                                          separators=(",", ":")).encode("ascii")).hexdigest()
        self.assertEqual(plan["sha256"], digest)
        values = [prepare_inputs(item["input_text"].encode())["args"][0] for item in plan["inputs"]]
        self.assertIn(9007199254740992, values)
        self.assertIn(9007199254740994, values)
        self.assertEqual(plan["max_initializations"], len(values) * 2)
        self.assertEqual(plan["max_seconds"], 120)

    def test_round_robin_locations_before_second_replacement(self):
        plan = prepare_input_search(raw([5, {"a": 7, "b": 9}], {"enabled": True}))
        self.assertEqual([item["location"] for item in plan["inputs"][:5]],
                         ["seed", "/args/0", "/args/1/a", "/args/1/b", "/kwargs/enabled"])
        seed = json.loads(plan["seed_input_text"])
        for item in plan["inputs"][1:]:
            value = json.loads(item["input_text"])
            differences = sum(a != b for a, b in zip(
                [seed["args"][0], *seed["args"][1].values(), seed["kwargs"]["enabled"]],
                [value["args"][0], *value["args"][1].values(), value["kwargs"]["enabled"]]))
            self.assertEqual(differences, 1)

    def test_preserves_dict_order_even_when_seed_envelope_is_reversed(self):
        plan = prepare_input_search(b'{"kwargs":{"z":5,"a":6},"args":[{"y":7,"b":8}]}')
        for item in plan["inputs"]:
            value = json.loads(item["input_text"])
            self.assertEqual(list(value), ["kwargs", "args"])
            self.assertEqual(list(value["kwargs"]), ["z", "a"])
            self.assertEqual(list(value["args"][0]), ["y", "b"])

    def test_bools_floats_null_and_whitespace_are_not_coerced(self):
        cases = [(True, [False]), (1.0, [0.0, -1.0, 2.0]), (None, [False, 0, ""]),
                 ("  alpha", ["", " ", "\t", "alpha", "  alph"])]
        for seed, expected in cases:
            with self.subTest(seed=seed):
                plan = prepare_input_search(raw([seed]))
                values = [json.loads(item["input_text"])["args"][0] for item in plan["inputs"][1:]]
                self.assertEqual([json.dumps(value) for value in values], [json.dumps(value) for value in expected])

    def test_finite_float_extremes_and_signed_zero(self):
        for seed in (1.7976931348623157e308, -1.7976931348623157e308, -0.0):
            plan = prepare_input_search(raw([seed]))
            for item in plan["inputs"]:
                prepare_inputs(item["input_text"].encode())
        texts = [item["input_text"] for item in prepare_input_search(raw([-0.0]))["inputs"]]
        self.assertTrue(any(':[0.0]' in text for text in texts))

    def test_cap_dedup_empty_and_location_bound(self):
        plan = prepare_input_search(raw(list(range(50))))
        self.assertEqual(len(plan["inputs"]), 12)
        self.assertTrue(plan["limited"])
        self.assertEqual(len({item["input_text"] for item in plan["inputs"]}), 12)
        empty = prepare_input_search(raw([[], {}]))
        self.assertEqual(len(empty["inputs"]), 1)
        self.assertFalse(empty["limited"])
        long_key = prepare_input_search(raw([], {"x" * 300: 1}))
        self.assertEqual(long_key["inputs"][1]["location"], "scalar 1")
        pointer = prepare_input_search(raw([], {"a/b~c": 1}))
        self.assertEqual(pointer["inputs"][1]["location"], "/kwargs/a~1b~0c")
        for key in ("\ud800", "\n", "\u202e"):
            plan = prepare_input_search(raw([], {key: 1}))
            self.assertEqual(plan["inputs"][1]["location"], "scalar 1")
            json.dumps(plan, ensure_ascii=False).encode("utf-8")

    def test_large_unicode_and_surrogates_never_produce_oversized_inputs(self):
        for seed in (raw(["\ud800"]), json.dumps({"args": ["界" * 5000], "kwargs": {}}, ensure_ascii=False).encode()):
            plan = prepare_input_search(seed)
            for item in plan["inputs"]:
                self.assertLessEqual(len(item["input_text"].encode("utf-8")), 16384)
                prepare_inputs(item["input_text"].encode("utf-8"))

    def test_rejects_invalid_inputs_without_repair(self):
        for seed in (b'{}', b'{"args":[],"args":[],"kwargs":{}}', b'{"args":[NaN],"kwargs":{}}',
                     b'{"args":[1e999],"kwargs":{}}', raw(["x" * 17000])):
            with self.subTest(seed=seed[:40]), self.assertRaises(ValueError):
                prepare_input_search(seed)

    def test_adjacent_integer_digit_cap_skips_only_that_replacement(self):
        limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
        if not 1 <= limit <= 16000:
            self.skipTest("no bounded native integer digit limit")
        seed = b'{"args":[' + b"9" * limit + b'],"kwargs":{}}'
        plan = prepare_input_search(seed)
        self.assertEqual(plan["inputs"][0]["input_text"], seed.decode())
        self.assertTrue(plan["limited"])
        self.assertGreater(len(plan["inputs"]), 1)
        for item in plan["inputs"]:
            prepare_inputs(item["input_text"].encode())


class SourceInputSearchTests(unittest.TestCase):
    def plan(self, code, seed=None, after=None):
        before = code.encode("utf-8")
        return prepare_input_search(raw([5]) if seed is None else seed,
                                    sources=(before, before if after is None else after.encode("utf-8")),
                                    entry="target")

    def hinted_values(self, plan):
        return [(json.loads(item["input_text"]), item["hint"])
                for item in plan["inputs"] if "hint" in item]

    def test_distant_thresholds_precede_nearby_with_both_source_lines(self):
        plan = self.plan("def target(x):\n    return x < 100\n", after="\ndef target(x):\n    return x <= 200\n")
        hints = self.hinted_values(plan)
        self.assertEqual([value["args"][0] for value, _ in hints], [100, 200, 99, 199, 101, 201])
        self.assertEqual([hint for _, hint in hints],
                         [{"side": side, "line": line} for _ in range(3)
                          for side, line in (("before", 2), ("after", 3))])
        self.assertEqual([json.loads(item["input_text"])["args"][0] for item in plan["inputs"][-5:]],
                         [0, 1, -1, 4, 6])
        self.assertEqual(plan["strategy"], "source-v1")
        self.assertEqual(plan["max_initializations"], 24)
        self.assertEqual(plan["max_seconds"], 120)

    def test_exact_seed_dictionary_order_bigints_and_pins(self):
        seed = b'{ "kwargs":{"z":5}, "args":[9007199254740993] }\n'
        source = "def target(x, *, z):\n    return x > 9007199254741000\n"
        plan = self.plan(source, seed)
        self.assertEqual(plan["inputs"][0], {"input_text": seed.decode(), "location": "seed"})
        self.assertEqual(plan["sources"], {"before": hashlib.sha256(source.encode()).hexdigest(),
                                           "after": hashlib.sha256(source.encode()).hexdigest(), "entry": "target"})
        self.assertEqual(json.loads(plan["inputs"][1]["input_text"])["args"][0], 9007199254741000)
        for item in plan["inputs"]:
            self.assertEqual(list(json.loads(item["input_text"])), ["kwargs", "args"])
        changed = self.plan(source, seed, after=source + "# a different retained module\n")
        self.assertEqual(plan["inputs"], changed["inputs"])
        self.assertNotEqual(plan["sha256"], changed["sha256"])
        content = {key: value for key, value in plan.items() if key != "sha256"}
        self.assertEqual(plan["sha256"], hashlib.sha256(json.dumps(content, ensure_ascii=True,
            allow_nan=False, separators=(",", ":")).encode("ascii")).hexdigest())

    def test_signed_literals_reverse_comparison_membership_and_exact_types(self):
        cases = [([5], "-100 < x", [-100, -101, -99]),
                 ([5], "x in (+100, 200)", [100, 99, 101, 200, 199, 201]),
                 (["loose"], "x in ['strict', 'safe']", ["strict", "safe"]),
                 ([True], "x is False", [False]),
                 ([1], "x == True", []), ([True], "x == 100", []),
                 ([5.0], "x < 100", []), ([5], "x < 100.0", []),
                 ([None], "x is None", []), (["a"], "x == b'strict'", [])]
        for args, comparison, expected in cases:
            with self.subTest(comparison=comparison, args=args):
                plan = self.plan(f"def target(x):\n    return {comparison}\n", raw(args))
                self.assertEqual([value["args"][0] for value, _ in self.hinted_values(plan)], expected)

    def test_positional_keyword_only_and_omitted_default_mapping(self):
        source = "def target(x, /, y=1000, *, mode='default'):\n    if x < 100: pass\n    if y < 200: pass\n    return mode == 'strict'\n"
        plan = self.plan(source, raw([5], {"mode": "loose"}))
        hints = [item for item in plan["inputs"] if "hint" in item]
        self.assertEqual([item["location"] for item in hints], ["/args/0"] * 3 + ["/kwargs/mode"])
        for item in plan["inputs"]:
            payload = json.loads(item["input_text"])
            self.assertEqual(len(payload["args"]), 1)
            self.assertEqual(list(payload["kwargs"]), ["mode"])
        keyword = self.plan("def target(x):\n    return x < 100\n", raw([], {"x": 5}))
        self.assertEqual(keyword["inputs"][1]["location"], "/kwargs/x")

    def test_unsupported_argument_shapes_do_not_guess(self):
        cases = [("x, *rest", raw([5])), ("x, **rest", raw([5])),
                 ("x", raw([5], {"x": 9})), ("x", raw([5, 9])),
                 ("x, /", raw([], {"x": 5})), ("x", raw([], {"other": 5})),
                 ("x, y", raw([5])), ("x, *, y", raw([5]))]
        for signature, seed in cases:
            with self.subTest(signature=signature, seed=seed):
                plan = self.plan(f"def target({signature}):\n    return x < 100\n", seed)
                self.assertEqual(plan["inputs"], prepare_input_search(seed)["inputs"])

    def test_nested_scopes_dataflow_and_noncomparison_literals_are_ignored(self):
        source = '''def target(x):
    """x < 100"""
    y = x
    def nested(x): return x < 101
    async def asynchronous(): return x < 102
    class Nested:
        value = x < 103
    check = lambda: x < 104
    a = [x for x in [] if x < 105]
    b = {x for x in [] if x < 106}
    c = {x: x for x in [] if x < 107}
    d = (x for x in [] if x < 108)
    if y < 109: pass
    if x.real < 110: pass
    if x[0] < 111: pass
    if 0 < x < 112: pass
    if x < 100 + 13: pass
    if x in (115, dynamic()): pass
    return 114
'''
        plan = self.plan(source)
        self.assertEqual(plan["inputs"], prepare_input_search(raw([5]))["inputs"])

    def test_normalized_identifier_spellings_do_not_gain_hints(self):
        for source in ("def target(Ｋ):\n    return K < 100\n",
                       "def target(K):\n    return Ｋ < 100\n"):
            self.assertEqual(self.hinted_values(self.plan(source)), [])

    def test_physical_line_endings_keep_ast_hint_coordinates(self):
        for separator in ("\n", "\r\n", "\r"):
            plan = self.plan(separator.join(("def target(x):", "    return x < 100", "")))
            self.assertEqual(plan["inputs"][1]["hint"], {"side": "before", "line": 2})
            self.assertEqual(json.loads(plan["inputs"][1]["input_text"])["args"], [100])

    def test_duplicate_sources_hints_are_deduplicated_and_bounded(self):
        source = "def target(x):\n" + "    if x < 100: pass\n" * 100 + "    return x < 200\n"
        plan = self.plan(source)
        self.assertEqual([value["args"][0] for value, _ in self.hinted_values(plan)],
                         [100, 99, 101, 200, 199, 201])
        source = "def target(x):\n" + "".join(f"    if x < {100 * n}: pass\n" for n in range(1, 101))
        capped = self.plan(source)
        self.assertEqual(len(self.hinted_values(capped)), 6)
        self.assertTrue(capped["limited"])
        self.assertLessEqual(len(capped["inputs"]), 12)
        self.assertEqual(len({item["input_text"] for item in capped["inputs"]}), len(capped["inputs"]))
        long_membership = self.plan("def target(x):\n    return x in (" + ",".join(map(str, range(100, 117))) + ")\n")
        self.assertEqual(self.hinted_values(long_membership), [])

    def test_first_32_scalar_locations_still_bound_source_hints(self):
        seed = raw([list(range(32)), 5])
        plan = self.plan("def target(values, x):\n    return x < 100\n", seed)
        self.assertEqual(self.hinted_values(plan), [])
        self.assertTrue(plan["limited"])
        self.assertEqual(plan["inputs"], prepare_input_search(seed)["inputs"])

    def test_source_admission_and_budgets_are_not_bypassed_by_no_hints(self):
        valid = b"def target(x): return x\n"
        bad_sources = [b"def target(: pass", b"def other(x): return x\n", b"async def target(x): return x\n",
                       valid + valid, valid + b"#" * 65536, b"\xff", valid + b"x=" + b"[" * 300 + b"]" * 300]
        for bad in bad_sources:
            with self.subTest(bad=bad[:40]), self.assertRaises(ValueError):
                prepare_input_search(raw([5]), sources=(valid, bad), entry="target")
        for sources, entry in (([valid, valid], "target"), ((valid,), "target"),
                               ((valid, "text"), "target"), ((valid, valid), None),
                               ((valid, valid), "__target"), (None, "target")):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                prepare_input_search(raw([5]), sources=sources, entry=entry)
        with mock.patch("forge8.experiments._MAX_CALL_AST_NODES", 5), self.assertRaises(ValueError):
            self.plan("def target(x): return x < 100\n")

    def test_oversized_hints_are_skipped_and_target_code_is_inert(self):
        source = "raise RuntimeError('must not execute')\n@dangerous()\ndef target(x=also_dangerous()):\n    return x == " + repr("z" * 17000) + "\n"
        with mock.patch("forge8.experiments.run_experiment", side_effect=AssertionError("must not execute")):
            plan = self.plan(source, raw(["a"]))
        self.assertTrue(plan["limited"])
        self.assertEqual(self.hinted_values(plan), [])
        for item in plan["inputs"]:
            self.assertLessEqual(len(item["input_text"].encode()), 16384)
            prepare_inputs(item["input_text"].encode())


if __name__ == "__main__":
    unittest.main()
