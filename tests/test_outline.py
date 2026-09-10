from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from forge8 import desk as desk_module
from forge8 import discovery as discovery_module
from test_desk import DeskFixture


class PythonOutlineTests(unittest.TestCase):
    def outline(self, text, path="sample.py"):
        return desk_module._python_outline(path, text)

    def test_qualified_lexical_names_classes_nested_functions_and_async(self):
        source = (
            "class Outer:\n"
            "    def build(self):\n"
            "        class Inner:\n"
            "            async def run(self):\n"
            "                return 1\n"
            "        return Inner\n"
            "if enabled:\n"
            "    def conditional():\n"
            "        pass\n"
        )
        result = self.outline(source)
        self.assertEqual(result["status"], "available")
        self.assertEqual([(item["name"], item["kind"]) for item in result["items"]], [
            ("Outer", "class"), ("Outer.build", "function"),
            ("Outer.build.Inner", "class"), ("Outer.build.Inner.run", "async function"),
            ("conditional", "function"),
        ])
        self.assertEqual(result["items"][3], {"name": "Outer.build.Inner.run",
            "kind": "async function", "start_line": 4, "definition_line": 4,
            "end_line": 5, "stub": False})

    def test_multiline_decorators_comments_and_signatures_include_actual_at_line(self):
        source = (
            "class Context:\n"
            "    @(\n"
            "        # commentary with @ does not begin the decorator\n"
            "        decorator\n"
            "        @ another\n"
            "    )\n"
            "    @factory(\n"
            "        'argument',\n"
            "    )\n"
            "    async def invoke(\n"
            "        self,\n"
            "        value,\n"
            "    ):\n"
            "        return value\n"
        )
        item = self.outline(source)["items"][1]
        self.assertEqual((item["name"], item["start_line"], item["definition_line"], item["end_line"]),
            ("Context.invoke", 2, 10, 14))

    def test_repeated_overload_declarations_are_not_deduplicated_or_resolved(self):
        source = (
            "class Context:\n"
            "    @overload\n"
            "    def invoke(self, value: int): ...\n"
            "    @overload\n"
            "    def invoke(self, value: str):\n"
            "        'A syntactic stub.'\n"
            "        ...\n"
            "    @overload\n"
            "    def invoke(self, value):\n"
            "        return value\n"
            "class Command:\n"
            "    def invoke(self): pass\n"
        )
        items = self.outline(source)["items"]
        invokes = [item for item in items if item["name"] == "Context.invoke"]
        self.assertEqual(len(invokes), 3)
        self.assertEqual([item["stub"] for item in invokes], [True, True, False])
        self.assertEqual([item["start_line"] for item in invokes], [2, 4, 8])
        self.assertEqual(items[-1]["name"], "Command.invoke")

    def test_stub_labels_only_literal_ellipsis_with_optional_docstring(self):
        source = (
            "def bare(): ...\n"
            "def documented():\n    'documentation'\n    ...\n"
            "def pass_only(): pass\n"
            "def docs_only(): 'documentation'\n"
            "def not_implemented(): raise NotImplementedError\n"
            "def returns_ellipsis(): return ...\n"
            "def multiple():\n    ...\n    ...\n"
            "class Stub: ...\n"
        )
        self.assertEqual([item["stub"] for item in self.outline(source)["items"]],
            [True, True, False, False, False, False, False, True])

    def test_crlf_cr_lf_and_unicode_identifiers_preserve_display_line_numbers(self):
        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=repr(newline)):
                source = newline.join(["# comment", "class 類別:", "    @decorator",
                    "    def 讀取(self):", "        return 1", ""])
                item = self.outline(source)["items"][1]
                self.assertEqual(item["name"], "類別.讀取")
                self.assertEqual((item["start_line"], item["definition_line"], item["end_line"]), (3, 4, 5))

    def test_other_splitlines_separators_disable_outline_before_parsing(self):
        for separator in "\v\f\x1c\x1d\x1e\x85\u2028\u2029":
            with self.subTest(separator=repr(separator)), patch.object(discovery_module.ast, "parse") as parse:
                result = self.outline("# first" + separator + "second\ndef good(): pass\n")
                self.assertEqual(result, {"status": "unavailable", "items": [], "reason": "line_separators"})
                parse.assert_not_called()

    def test_unsupported_and_empty_python_sources_have_distinct_statuses(self):
        with patch.object(discovery_module.ast, "parse") as parse:
            self.assertEqual(self.outline("void run() {}", "sample.cpp"),
                {"status": "unsupported", "items": [], "reason": "language"})
            parse.assert_not_called()
        self.assertEqual(self.outline("# no declarations\n", "sample.pyi"),
            {"status": "available", "items": []})

    def test_syntax_and_local_parser_version_fall_back_without_error_details(self):
        self.assertEqual(self.outline("def broken(:\n"),
            {"status": "unavailable", "items": [], "reason": "syntax_or_version"})
        result = self.outline("type Alias = int\ndef read(): pass\n")
        self.assertEqual(result["status"], "available" if sys.version_info >= (3, 12) else "unavailable")
        with patch.object(discovery_module.ast, "parse", side_effect=RecursionError("private source")):
            self.assertEqual(self.outline("def read(): pass\n"),
                {"status": "unavailable", "items": [], "reason": "parse_depth"})

    def test_actual_node_item_and_source_budgets_return_limited_not_partial_results(self):
        cases = (
            ("node_limit", "x = 1\n" * 14_000),
            ("item_limit", "\n".join(f"def f{n}(): pass" for n in range(301))),
            ("source_limit", "#" + "x" * desk_module.MAX_REPOSITORY_FILE_BYTES),
        )
        for reason, source in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.outline(source), {"status": "limited", "items": [], "reason": reason})

    def test_metadata_budget_counts_utf8_bytes_and_returns_no_partial_outline(self):
        source = "\n".join(f"def 函式{n}_{'字' * 100}(): pass" for n in range(200))
        self.assertLess(len(source.encode("utf-8")), desk_module.MAX_REPOSITORY_FILE_BYTES)
        self.assertEqual(self.outline(source), {"status": "limited", "items": [], "reason": "metadata_limit"})
        source = "def ordinary(): pass\n"
        result = self.outline(source)
        exact = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        with patch.object(discovery_module, "_OUTLINE_FILE_BYTES", exact):
            self.assertEqual(self.outline(source), result)
        with patch.object(discovery_module, "_OUTLINE_FILE_BYTES", exact - 1):
            self.assertEqual(self.outline(source)["reason"], "metadata_limit")


class OutlineDeskTests(DeskFixture):
    def test_original_admitted_text_is_parsed_once_and_served_from_cached_metadata(self):
        source = "# a hidden character: \u200b\n@unknown_decorator()\ndef read(): return 1\n"
        (self.source / "main.py").write_bytes(source.encode("utf-8"))
        original_parse = ast.parse
        with patch.object(discovery_module.ast, "parse", wraps=original_parse) as parse:
            project = self.desk.refresh()
        parse.assert_called_once_with(source)
        (self.source / "main.py").write_text("def changed(): pass\n", encoding="utf-8")
        with (
            patch.object(Path, "read_bytes", side_effect=AssertionError("no filesystem read")),
            patch.object(discovery_module.ast, "parse", side_effect=AssertionError("no repeated parsing")),
            patch.object(desk_module, "_run_explain_cli") as inference,
        ):
            view = self.desk.source_view("0", project["version"])
        inference.assert_not_called()
        self.assertIn("\\u200b", view["lines"][0])
        self.assertEqual(view["outline"]["items"][0]["name"], "read")
        self.assertEqual(view["outline"]["items"][0]["start_line"], 2)
        self.assertEqual(set(vars(self.desk)) & {"tree", "ast", "raw_source", "raw_text"}, set())

    def test_refresh_replaces_outline_and_rejects_stale_file_version(self):
        old = self.desk.project["version"]
        (self.source / "main.py").write_text("class New:\n    def method(self): pass\n", encoding="utf-8")
        project = self.desk.refresh()
        with self.assertRaises(ValueError):
            self.desk.source_view("0", old)
        view = self.desk.source_view("0", project["version"])
        self.assertEqual([item["name"] for item in view["outline"]["items"]], ["New", "New.method"])

    def test_project_metadata_budget_marks_later_files_limited(self):
        (self.source / "a.py").write_text("def small(): pass\n", encoding="utf-8")
        (self.source / "b.py").write_text("\n".join(f"def function_{n}(): pass" for n in range(4)), encoding="utf-8")
        with patch.object(desk_module, "_OUTLINE_CACHE_BYTES", 512):
            project = self.desk.refresh()
        outlines = {item["path"]: self.desk.source_view(item["id"], project["version"])["outline"] for item in project["files"]}
        self.assertEqual(outlines["a.py"]["status"], "available")
        self.assertEqual(outlines["b.py"], {"status": "limited", "items": [], "reason": "metadata_limit"})
        self.assertLessEqual(sum(len(json.dumps(value, ensure_ascii=False).encode("utf-8")) for value in outlines.values()), 512)

    def test_parse_failure_preserves_browsing_and_literal_search(self):
        (self.source / "main.py").write_text("def unfinished(\n", encoding="utf-8")
        project = self.desk.refresh()
        view = self.desk.source_view("0", project["version"])
        self.assertEqual(view["lines"], ["def unfinished("])
        self.assertEqual(view["outline"]["status"], "unavailable")
        self.assertEqual(self.desk.search("unfinished")["matches"][0]["line"], 1)
