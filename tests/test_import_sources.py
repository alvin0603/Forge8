"""Owned source strings only; no source imports, bytecode or model execution."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from forge8 import import_sources


class ImportSourcesTests(unittest.TestCase):
    def trace(self, files, *, path="entry.py", name="Public", kind="from import",
            first=1, last=None, conditional=False):
        calls = []
        def read(target):
            self.assertIn(target, files)
            self.assertNotEqual(target, path, "initial retained text is supplied directly")
            calls.append(target)
            return files[target]
        candidate = {"name": name, "kind": kind, "start_line": first,
            "end_line": first if last is None else last, "conditional": conditional,
            "use_lines": [first], "binding_id": "B1"}
        result = import_sources.trace_import(list(files), path, files[path], candidate, read)
        self.assertEqual(set(result), {"scope", "semantics_verified", "status", "reason", "routes"})
        self.assertEqual(result["scope"], "snapshot import candidates")
        self.assertIs(result["semantics_verified"], False)
        self.assertLessEqual(len(result["routes"]), 32)
        if result["status"] != "available":
            self.assertEqual(result["routes"], [], "no partial successful routes on refusal")
        for route in result["routes"]:
            self.assertEqual(set(route), {"outcome", "reason", "steps"})
            self.assertIn(route["outcome"], {"declaration", "module", "unresolved", "cycle"})
            self.assertTrue(1 <= len(route["steps"]) <= 8)
            self.assertEqual(route["steps"][0], {"path": path, "name": name, "kind": kind,
                "start_line": first, "end_line": first if last is None else last,
                "conditional": conditional})
            for step in route["steps"]:
                self.assertEqual(set(step), {"path", "name", "kind", "start_line", "end_line", "conditional"})
                self.assertIn(step["path"], files)
                self.assertIs(type(step["conditional"]), bool)
                if step["kind"] == "module":
                    self.assertIsNone(step["start_line"])
                    self.assertIsNone(step["end_line"])
                else:
                    self.assertIs(type(step["start_line"]), int)
                    self.assertTrue(1 <= step["start_line"] <= step["end_line"]
                        <= len(files[step["path"]].splitlines()))
        self.assertEqual(len(calls), len(set(calls)), "each retained target is read at most once")
        return result, calls

    def destinations(self, result, outcome="declaration"):
        return [(route["steps"][-1]["path"], route["steps"][-1]["name"])
            for route in result["routes"] if route["outcome"] == outcome]

    def test_local_alias_follows_exact_import_statement_without_executing_source(self):
        files = {"entry.py": "def entry():\n    from helper import Actual as Public\n    return Public\n",
            "helper.py": 'raise RuntimeError("never execute")\ndef Actual():\n    return 1\n'}
        with patch("builtins.exec", side_effect=AssertionError("target execution forbidden")), \
                patch("builtins.eval", side_effect=AssertionError("target evaluation forbidden")):
            result, calls = self.trace(files, first=2)
        self.assertEqual(self.destinations(result), [("helper.py", "Actual")])
        self.assertEqual(calls, ["helper.py"])
        self.assertEqual(result["routes"][0]["steps"][-1]["start_line"], 2)

    def test_root_and_src_are_alternatives_not_an_import_precedence(self):
        files = {"entry.py": "from helper import Public\n",
            "helper.py": "Public = 1\n", "src/helper.py": "Public = 2\n"}
        result, _ = self.trace(files)
        self.assertEqual(set(self.destinations(result)), {("helper.py", "Public"), ("src/helper.py", "Public")})

    def test_relative_imports_stay_within_their_conventional_root(self):
        files = {"src/pkg/entry.py": "from .helpers import Public\n",
            "src/pkg/helpers.py": "Public = 1\n", "pkg/helpers.py": "Public = 2\n"}
        result, calls = self.trace(files, path="src/pkg/entry.py")
        self.assertEqual(self.destinations(result), [("src/pkg/helpers.py", "Public")])
        self.assertNotIn("pkg/helpers.py", calls)
        files["src/pkg/entry.py"] = "from ...helpers import Public\n"
        result, calls = self.trace(files, path="src/pkg/entry.py")
        self.assertEqual(result["routes"][0]["reason"], "relative_escape")
        self.assertEqual(calls, [])

    def test_two_reexports_retain_every_real_import_and_destination(self):
        files = {"entry.py": "from pkg import Public\n",
            "pkg/__init__.py": "from .api import Actual as Public\n",
            "pkg/api.py": "from .impl import Worker as Actual\n",
            "pkg/impl.py": "class Worker:\n    pass\n"}
        result, _ = self.trace(files)
        route, = result["routes"]
        self.assertEqual(route["outcome"], "declaration")
        self.assertEqual([row["path"] for row in route["steps"]],
            ["entry.py", "pkg/__init__.py", "pkg/api.py", "pkg/impl.py"])
        self.assertEqual(route["steps"][-1]["name"], "Worker")

    def test_relative_import_cannot_treat_search_root_as_an_unnamed_package(self):
        for prefix in ("", "src/"):
            for entry, statement in (("pkg/entry.py", "from ..helpers import Public\n"),
                    ("entry.py", "from .helpers import Public\n")):
                with self.subTest(prefix=prefix, entry=entry):
                    files = {prefix + entry: statement, prefix + "helpers.py": "Public = 1\n"}
                    result, calls = self.trace(files, path=prefix + entry)
                    self.assertEqual(result["routes"][0]["reason"], "relative_escape")
                    self.assertEqual(calls, [])
                    files[prefix + "__init__.py"] = ""
                    result, _ = self.trace(files, path=prefix + entry)
                    self.assertEqual(self.destinations(result), [(prefix + "helpers.py", "Public")])

    def test_namespace_directory_can_lead_to_real_child_without_fake_init_file(self):
        files = {"entry.py": "from pkg import child as Public\n", "pkg/child.py": ""}
        result, calls = self.trace(files)
        self.assertEqual(self.destinations(result, "module"), [("pkg/child.py", "child")])
        self.assertEqual(calls, ["pkg/child.py"])
        self.assertEqual(result["routes"][0]["steps"][-1]["start_line"], None)
        files["entry.py"] = "import pkg as Public\n"
        result, calls = self.trace(files, kind="import")
        self.assertEqual(result["routes"][0]["reason"], "namespace_package")
        self.assertEqual(calls, [])

    def test_package_attribute_and_child_module_are_both_visible(self):
        files = {"entry.py": "from pkg import Public\n",
            "pkg/__init__.py": "Public = 1\n", "pkg/Public.py": "OTHER = 2\n"}
        result, _ = self.trace(files)
        self.assertEqual(self.destinations(result), [("pkg/__init__.py", "Public")])
        self.assertEqual(self.destinations(result, "module"), [("pkg/Public.py", "Public")])
        self.assertEqual(next(row for row in result["routes"] if row["outcome"] == "module")["reason"],
            "submodule_candidate")

    def test_module_package_collision_and_conditional_definitions_are_not_chosen(self):
        files = {"entry.py": "from helper import Public\n", "helper.py": "Public = 0\n",
            "helper/__init__.py": "if flag:\n    Public = 1\nelse:\n    Public = 2\n"}
        result, _ = self.trace(files)
        self.assertEqual(len(self.destinations(result)), 3)
        self.assertEqual([row["steps"][-1]["conditional"] for row in result["routes"]], [False, True, True])

    def test_unaliased_dotted_import_browses_bound_package_not_submodule(self):
        files = {"entry.py": "import pkg.tools\n", "pkg/__init__.py": "", "pkg/tools.py": "VALUE = 1\n"}
        result, calls = self.trace(files, kind="import", name="pkg")
        route, = result["routes"]
        self.assertEqual((route["outcome"], route["reason"]),
            ("module", "imported_submodule_not_attribute_resolution"))
        self.assertEqual(self.destinations(result, "module"), [("pkg/__init__.py", "pkg")])
        self.assertEqual(calls, ["pkg/__init__.py"])
        files["entry.py"] = "import pkg.tools as Public\n"
        result, _ = self.trace(files, kind="import")
        self.assertEqual(self.destinations(result, "module"), [("pkg/tools.py", "pkg.tools")])

    def test_same_line_imports_and_duplicate_bound_aliases_branch(self):
        files = {"entry.py": "from one import A as Public; from two import B as Public\n",
            "one.py": "A = 1\n", "two.py": "B = 2\n"}
        result, _ = self.trace(files)
        self.assertEqual(set(self.destinations(result)), {("one.py", "A"), ("two.py", "B")})
        files["entry.py"] = "from one import A as Public, B as Public\n"
        files["one.py"] = "A = 1\nB = 2\n"
        result, _ = self.trace(files)
        self.assertEqual(set(self.destinations(result)), {("one.py", "A"), ("one.py", "B")})

    def test_cycle_is_explicit_without_reading_a_file_twice(self):
        files = {"entry.py": "from other import Public\n", "other.py": "from entry import Public\n"}
        result, calls = self.trace(files)
        route, = result["routes"]
        self.assertEqual((route["outcome"], route["reason"]), ("cycle", "cycle"))
        self.assertEqual(calls, ["other.py"])

    def test_external_missing_and_dynamic_exports_remain_unresolved(self):
        result, calls = self.trace({"entry.py": "from external_package import Public\n"})
        self.assertEqual(result["routes"][0]["reason"], "not_in_snapshot")
        self.assertEqual(calls, [])
        for source in ("from external_package import *\n",
                "def __getattr__(name):\n    return object()\n",
                "Public = 1\ndel Public\n"):
            with self.subTest(source=source):
                result, _ = self.trace({"entry.py": "from helper import Public\n", "helper.py": source})
                self.assertIn("dynamic_exports", [row["reason"] for row in result["routes"]])

    def test_explicit_import_ignores_all_and_does_not_chase_expression_aliases(self):
        files = {"entry.py": "from helper import Public\n",
            "helper.py": "__all__ = dangerous_call()\nPublic = Elsewhere\n"}
        result, _ = self.trace(files)
        self.assertEqual(self.destinations(result), [("helper.py", "Public")])
        self.assertEqual(result["routes"][0]["reason"], "expression_alias_not_followed")
        files["helper.py"] = "value = lambda: (Public := 1)\n"
        result, _ = self.trace(files)
        self.assertEqual(result["routes"][0]["reason"], "name_not_declared")
        files["helper.py"] = "value = lambda ignored=(Public := 1): ignored\n"
        result, _ = self.trace(files)
        self.assertEqual(self.destinations(result), [("helper.py", "Public")])
        self.assertEqual(result["routes"][0]["steps"][-1]["kind"], "named assignment")

    def test_crlf_unicode_and_multiline_import_keep_physical_coordinates(self):
        files = {"entry.py": "from helper import (\r\n    實作 as Public,\r\n)\r\n",
            "helper.py": "@decorate\r\ndef 實作():\r\n    return 1\r\n"}
        result, _ = self.trace(files, last=3)
        self.assertEqual(result["routes"][0]["steps"][-1]["start_line"], 1)
        self.assertEqual(result["routes"][0]["steps"][-1]["end_line"], 3)
        for changed, reason in (("Public = 1\u2028\n", "line_separators"),
                ("def broken(:\n", "syntax_or_version"), ("# \ud800\n", "utf8")):
            with self.subTest(reason=reason):
                result, _ = self.trace({**files, "helper.py": changed}, last=3)
                self.assertEqual((result["status"], result["reason"]), ("unavailable", reason))

    def test_definition_headers_are_module_expressions_but_bodies_are_not(self):
        for header in ("def configure(arg=(Public := 1)):",
                "async def configure(*, arg=(Public := 1)):",
                "@(Public := decorate)\ndef configure():",
                "class Configure((Public := Base)):",
                "class Configure(metaclass=(Public := Meta)):"):
            with self.subTest(header=header):
                files = {"entry.py": "from helper import Public\n",
                    "helper.py": "Public = old\n" + header + "\n    BodyOnly = 9\n"}
                result, _ = self.trace(files)
                self.assertEqual([route["steps"][-1]["kind"] for route in result["routes"]],
                    ["assignment", "named assignment"])
                self.assertEqual([route["steps"][-1]["start_line"] for route in result["routes"]], [1, 2])
                files["entry.py"] = "from helper import BodyOnly\n"
                result, _ = self.trace(files, name="BodyOnly")
                self.assertEqual(self.destinations(result), [])

    def test_limits_clear_previously_found_routes(self):
        files = {"entry.py": "from helper import Public\n",
            "helper.py": "Public = 1\n", "src/helper.py": "Public = 2\n"}
        for limit, value, reason in (("_MAX_FILES", 2, "file_limit"),
                ("_MAX_BYTES", 1, "byte_limit"), ("_MAX_NODES", 1, "node_limit"),
                ("_MAX_ROUTES", 1, "route_limit"), ("_MAX_STEPS", 1, "step_limit"),
                ("_MAX_METADATA_BYTES", 100, "metadata_limit"),
                ("_MAX_SOURCE_BYTES", 1, "source_limit")):
            with self.subTest(reason=reason), patch.object(import_sources, limit, value):
                result, _ = self.trace(files)
            self.assertEqual((result["status"], result["reason"], result["routes"]),
                ("limited", reason, []))

    def test_malformed_candidate_or_logical_paths_never_call_reader(self):
        reader = Mock()
        good = {"name": "Public", "kind": "from import", "start_line": 1,
            "end_line": 1, "conditional": False}
        for candidate in (None, {}, {**good, "start_line": True}, {**good, "end_line": 0},
                {**good, "conditional": 1}, {**good, "name": "Other"}, {**good, "kind": "assignment"},
                {**good, "kind": []}):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                import_sources.trace_import(["entry.py"], "entry.py", "from helper import Public\n", candidate, reader)
        for bad in ("../escape.py", "/escape.py", "a//b.py", "a\\b.py", "D:/escape.py", "./entry.py"):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                import_sources.trace_import([bad], bad, "from helper import Public\n", good, reader)
        reader.assert_not_called()

    def test_snapshot_callback_refusal_propagates_instead_of_becoming_unresolved(self):
        candidate = {"name": "Public", "kind": "from import", "start_line": 1,
            "end_line": 1, "conditional": False}
        for failure in (ValueError("retained source changed"), OSError("retained source unavailable")):
            reader = Mock(side_effect=failure)
            with self.subTest(failure=type(failure)), self.assertRaises(type(failure)):
                import_sources.trace_import(["entry.py", "helper.py"], "entry.py",
                    "from helper import Public\n", candidate, reader)
            reader.assert_called_once_with("helper.py")


if __name__ == "__main__":
    unittest.main()
