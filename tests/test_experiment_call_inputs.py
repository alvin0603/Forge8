from __future__ import annotations

import json
import math
import unittest
from unittest.mock import patch

from forge8 import experiments


class ExperimentCallInputsTests(unittest.TestCase):
    """Only static parsing of inert source; never import or execute its contents."""

    def prepare(self, source: str, *, entry: str = "entry", start: int = 1, end: int | None = None):
        return experiments.prepare_call_inputs(source.encode("utf-8"), entry, start,
                                               len(source.splitlines()) if end is None else end)

    def test_exact_bigint_chinese_quotes_newlines_kwargs_null_and_coordinates_without_execution(self) -> None:
        source = ('import FORGE8_SOURCE_MUST_NEVER_BE_IMPORTED\r\n'
                  'raise AssertionError("source must never execute")\r\n'
                  'result = entry(\r\n'
                  '    9007199254740993, "你好", \'say "yes"\\nnext\',\r\n'
                  '    [None, True, False, -17, +2.5],\r\n'
                  '    options={"label": "中文", "empty": None},\r\n'
                  ')\r\n')
        with patch("builtins.exec", side_effect=AssertionError("no execution")), \
                patch("builtins.eval", side_effect=AssertionError("no evaluation")):
            result = self.prepare(source, start=3, end=7)
        expected = {"args": [9007199254740993, "你好", 'say "yes"\nnext', [None, True, False, -17, 2.5]],
                    "kwargs": {"options": {"label": "中文", "empty": None}}}
        self.assertEqual(result, {"entry": "entry", "start": 3, "end": 7, "call_start": 3,
                                  "call_end": 7, "input_text": json.dumps(expected, ensure_ascii=False,
                                                                        allow_nan=False, indent=2)})
        self.assertIs(type(json.loads(result["input_text"])["args"][0]), int)
        self.assertEqual(experiments.prepare_inputs(result["input_text"].encode("utf-8")), expected)

    def test_negative_zero_empty_inputs_and_adjacent_constant_strings(self) -> None:
        result = json.loads(self.prepare('entry(-0.0, +0.0, -0, "a" "b")')["input_text"])
        self.assertEqual(result["args"], [-0.0, 0.0, 0, "ab"])
        self.assertEqual(math.copysign(1, result["args"][0]), -1)
        self.assertEqual(json.loads(self.prepare("entry()")["input_text"]), {"args": [], "kwargs": {}})

    def test_unicode_columns_and_exact_spelling_without_binding_claim(self) -> None:
        result = self.prepare('文字 = entry("值")\n')
        self.assertEqual(json.loads(result["input_text"])["args"], ["值"])
        shadowed = 'def example(entry):\n    return entry(3)\n'
        self.assertEqual(json.loads(self.prepare(shadowed, start=2)["input_text"])["args"], [3])
        with self.assertRaises(ValueError):
            self.prepare("ｅｎｔｒｙ(3)\n")

    def test_strings_comments_attributes_and_aliases_are_not_matching_calls(self) -> None:
        for source in ('"entry(1)"\n# entry(2)\n', "obj.entry(1)\n", "alias(1)\n"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.prepare(source)
        result = self.prepare('# entry(99)\ntext = "entry(100)"\nentry(4)\n')
        self.assertEqual((result["call_start"], result["call_end"]), (3, 3))

    def test_multiple_nested_and_partial_matching_calls_are_rejected(self) -> None:
        for source, start, end in (("entry(1); entry(2)\n", 1, 1), ("entry(entry(1))\n", 1, 1),
                                   ("entry(\n  1\n)\n", 1, 2), ("entry(\n  1\n)\n", 2, 3),
                                   ("entry(1)\nentry(\n  2\n)\n", 1, 2),
                                   ("entry(\n  1\n)\nentry(2)\n", 3, 4)):
            with self.subTest(source=source, start=start, end=end), self.assertRaises(ValueError):
                self.prepare(source, start=start, end=end)
        result = self.prepare("entry(1)\nentry(2)\n", start=2, end=2)
        self.assertEqual(json.loads(result["input_text"])["args"], [2])

    def test_expressions_and_non_json_literal_types_never_get_evaluated(self) -> None:
        expressions = ("value", "1 + 2", "danger()", "__import__('os')", "obj.value", "x[0]",
                       "(1, 2)", "{1, 2}", "b'bytes'", "1j", "...", "f'constant'",
                       "[x for x in values]", "{x: 1 for x in values}", "-True", "--1",
                       "*values", "**values", "[1, *values]", "{'a': 1, **values}", "{1: 'value'}")
        for expression in expressions:
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                self.prepare(f"entry({expression})\n")

    def test_duplicate_dictionary_and_keyword_names_are_rejected(self) -> None:
        for source in ('entry({"a": 1, "a": 2})', 'entry({"a": 1, "\\x61": 2})',
                       "entry(label=1, label=2)", "entry(options={'a': 1, 'a': 2})"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.prepare(source)

    def test_nonfinite_floats_and_unrepresentable_utf8_string_are_rejected(self) -> None:
        for value in ("1e999", "-1e999", "+1e999", "'\\ud800'"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.prepare(f"entry({value})")

    def test_literal_depth_and_aggregate_node_limits(self) -> None:
        self.prepare("entry(" + "[" * 23 + "0" + "]" * 23 + ")")
        with self.assertRaisesRegex(ValueError, "24-depth"):
            self.prepare("entry(" + "[" * 24 + "0" + "]" * 24 + ")")
        self.prepare("entry([" + ",".join(["0"] * 1023) + "])")
        with self.assertRaisesRegex(ValueError, "1024-node"):
            self.prepare("entry([" + ",".join(["0"] * 1024) + "])")
        with self.assertRaisesRegex(ValueError, "1024-node"):
            self.prepare("entry([" + ",".join(["0"] * 512) + "],[" + ",".join(["0"] * 511) + "])")

    def test_source_ast_selection_and_generated_json_limits(self) -> None:
        with patch.object(experiments.ast, "parse") as parse, self.assertRaisesRegex(ValueError, "256 KiB"):
            experiments.prepare_call_inputs(b"#" * (256 * 1024 + 1), "entry", 1, 1)
        parse.assert_not_called()
        huge_tree = "x=0\n" * 12_500 + "entry()\n"
        with self.assertRaisesRegex(ValueError, "50000 AST nodes"):
            self.prepare(huge_tree, start=12_501, end=12_501)
        self.prepare("entry()" + " " * (4000 - len("entry()")))
        for source in ("entry()" + " " * 3994, 'entry("' + "中" * 1400 + '")',
                       "entry()\n" + "#\n" * 80):
            with self.subTest(length=len(source)), self.assertRaises(ValueError):
                self.prepare(source)
        # Deep formatting can exceed the JSON cap despite a small source range
        # and fewer than 1024 literal nodes; the existing input gate still applies.
        expanded = "entry(" + "[" * 20 + "[" + ",".join(["0"] * 400) + "]" + "]" * 20 + ")"
        with self.assertRaisesRegex(ValueError, "16 KiB"):
            self.prepare(expanded)

    def test_invalid_types_ranges_source_syntax_and_nonphysical_lines(self) -> None:
        invalid = [("entry()", "entry", 1, 1), (b"entry()", None, 1, 1),
                   (b"entry()", "__entry", 1, 1), (b"entry()", "obj.entry", 1, 1),
                   (b"entry()", "entry", True, 1), (b"entry()", "entry", 1, False),
                   (b"entry()", "entry", 0, 1), (b"entry()", "entry", 2, 1),
                   (b"entry()", "entry", 1, 2), (b"", "entry", 1, 1),
                   (b"\xff", "entry", 1, 1), (b"entry(\n", "entry", 1, 1),
                   (b"entry()\x00", "entry", 1, 1)]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                experiments.prepare_call_inputs(*args)
        for separator in "\v\f\x1c\x1d\x1e\x85\u2028\u2029":
            with self.subTest(separator=repr(separator)), self.assertRaisesRegex(ValueError, "nonphysical"):
                self.prepare(f"# text{separator}entry()", end=1)
        for cookie in ("latin-1", "no_such_encoding"):
            with self.subTest(cookie=cookie), self.assertRaises(ValueError):
                self.prepare(f'# coding: {cookie}\nentry("中文")\n', start=2)


if __name__ == "__main__":
    unittest.main()
