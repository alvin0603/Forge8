"""Static input planning only; these tests never run target Python source."""
import hashlib
import json
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
