from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from forge8 import changes


class ChangeCatalogueTests(unittest.TestCase):
    def catalogue(self, old, new, path="cache.py"):
        return changes.build_change_catalogue({path: old}, {path: new})

    def test_changed_callee_retains_default_and_does_not_invent_caller_change(self):
        old = ("DEFAULT = 7\n\ndef lookup(cache, key):\n"
            "    return cache.get(key) or DEFAULT\n\n"
            "def caller(cache):\n    return lookup(cache, 'x')\n")
        new = old.replace("cache.get(key) or DEFAULT", "cache.get(key, DEFAULT)")
        before = {"cache.py": old, "caller.py": "from cache import lookup\nvalue = lookup({}, 'x')\n"}
        after = {**before, "cache.py": new}
        frozen = copy.deepcopy((before, after))
        result = changes.build_change_catalogue(before, after)
        self.assertEqual([item["path"] for item in result["files"]], ["cache.py"])
        entry = result["files"][0]
        self.assertEqual(entry["hunks"], [{"before": {"start": 3, "count": 1},
            "after": {"start": 3, "count": 1}}])
        self.assertEqual(entry["units"], [{"id": "U1", "name": "lookup", "kind": "function",
            "status": "modified", "before": {"start_line": 3, "end_line": 4},
            "after": {"start_line": 3, "end_line": 4}}])
        self.assertEqual((before, after), frozen)
        self.assertIn("DEFAULT = 7", before["cache.py"])
        self.assertIn("DEFAULT = 7", after["cache.py"])
        self.assertFalse(result["semantics_verified"])
        self.assertNotIn("return cache.get", json.dumps(result))

    def test_same_name_different_scopes_or_files_are_not_paired(self):
        result = self.catalogue("class Left:\n    def run(self): return 1\n",
            "class Right:\n    def run(self): return 2\n")
        units = result["files"][0]["units"]
        self.assertEqual({item["name"] for item in units}, {"Left", "Left.run", "Right", "Right.run"})
        self.assertTrue(all((item["before"] is None) != (item["after"] is None) for item in units))
        moved = changes.build_change_catalogue({"old.py": "def run(): return 1\n"},
            {"new.py": "def run(): return 2\n"})
        self.assertEqual([(item["path"], item["status"]) for item in moved["files"]],
            [("new.py", "added"), ("old.py", "absent")])
        self.assertTrue(all((unit["before"] is None) != (unit["after"] is None)
            for item in moved["files"] for unit in item["units"]))

    def test_function_rename_is_explicit_one_sided_not_a_guessed_pair(self):
        result = self.catalogue("def first(): return 1\n", "def second(): return 1\n")
        units = result["files"][0]["units"]
        self.assertEqual([(item["name"], item["status"]) for item in units],
            [("first", "absent"), ("second", "added")])

    def test_duplicate_overloads_and_redefinitions_fall_back_to_hunks(self):
        for source in ("@overload\ndef run(x: int): ...\n@overload\ndef run(x: str): ...\n",
                "def run(): return 1\ndef run(): return 2\n"):
            with self.subTest(source=source):
                changed = source.replace("int", "float") if "int" in source else source.replace("2", "3")
                entry = self.catalogue(source, changed)["files"][0]
                self.assertEqual(entry["units"], [])
                self.assertEqual(entry["detail"]["reasons"], ["ambiguous_definitions"])
                self.assertTrue(entry["hunks"])

    def test_changed_stub_is_not_an_implementation_pair(self):
        entry = self.catalogue("def run(): ...\n", "def run(): return 1\n")["files"][0]
        self.assertEqual(entry["units"], [])
        self.assertEqual(entry["detail"]["reasons"], ["stub_definitions"])

    def test_unchanged_definition_moving_lines_is_not_listed_as_changed(self):
        entry = self.catalogue("def stable(): return 1\n", "# a note\ndef stable(): return 1\n")["files"][0]
        self.assertEqual(entry["units"], [])
        self.assertEqual(entry["hunks"][0]["before"], {"start": 0, "count": 0})

    def test_line_endings_and_final_newline_only_are_explicit_not_behavior_claims(self):
        for old, new in (("def run(): return 1\n", "def run(): return 1\r\n"),
                ("def run(): return 1", "def run(): return 1\n")):
            with self.subTest(new=new):
                entry = self.catalogue(old, new)["files"][0]
                self.assertEqual(entry["status"], "modified")
                self.assertTrue(entry["line_endings_only"])
                self.assertEqual(entry["hunks"], [])
                self.assertEqual(entry["units"], [])
                self.assertEqual(entry["detail"]["reasons"], ["line_endings_only"])

    def test_added_absent_and_empty_files_have_no_invented_citable_lines(self):
        result = changes.build_change_catalogue({"gone.py": "def gone(): pass\n", "empty.txt": ""},
            {"new.py": "def new(): pass\n", "blank.txt": ""})
        entries = {entry["path"]: entry for entry in result["files"]}
        self.assertEqual(entries["gone.py"]["status"], "absent")
        self.assertEqual(entries["gone.py"]["hunks"][0]["after"], {"start": 0, "count": 0})
        self.assertIsNone(entries["gone.py"]["units"][0]["after"])
        self.assertEqual(entries["new.py"]["hunks"][0]["before"], {"start": 0, "count": 0})
        self.assertIsNone(entries["new.py"]["units"][0]["before"])
        for path in ("empty.txt", "blank.txt"):
            self.assertFalse(entries[path]["line_endings_only"])
            self.assertEqual(entries[path]["hunks"], [])

    def test_non_python_and_parse_failure_keep_exact_hunks_without_source_text(self):
        for path, old, new in (("main.cpp", "int run() { return 1; }\n", "int run() { return 2; }\n"),
                ("broken.py", "def broken(:\n", "def broken(x:\n")):
            with self.subTest(path=path):
                entry = self.catalogue(old, new, path)["files"][0]
                self.assertEqual(entry["detail"]["hunks"], "exact")
                self.assertEqual(entry["detail"]["units"], "unavailable")
                self.assertTrue(entry["hunks"])
                self.assertEqual(entry["units"], [])
                self.assertNotIn(old, json.dumps(entry))

    def test_large_repetitive_input_uses_linear_enclosing_span_without_diff_or_ast(self):
        old = "same\n" * 1100 + "old\n" + "same\n" * 1100
        new = "same\n" * 1100 + "new\n" + "same\n" * 1100
        with patch.object(changes, "SequenceMatcher") as matcher, patch.object(changes, "_python_outline") as outline:
            entry = self.catalogue(old, new)["files"][0]
        matcher.assert_not_called()
        outline.assert_not_called()
        self.assertEqual(entry["detail"]["hunks"], "coarse")
        self.assertEqual(entry["detail"]["reasons"], ["diff_work_limit"])
        self.assertEqual(entry["hunks"], [{"before": {"start": 1100, "count": 1},
            "after": {"start": 1100, "count": 1}}])

    def test_coarse_span_explicitly_may_include_unchanged_middle(self):
        old, new = "a\nkeep\nb\n", "x\nkeep\ny\n"
        with patch.object(changes, "_DIFF_PAIRS", 0):
            entry = self.catalogue(old, new)["files"][0]
        self.assertEqual(entry["detail"]["hunks"], "coarse")
        self.assertEqual(entry["hunks"][0], {"before": {"start": 0, "count": 3},
            "after": {"start": 0, "count": 3}})

    def test_catalogue_is_sorted_deterministic_and_keeps_all_1000_paths_under_both_bounds(self):
        before = {f"f{n:04}.py": "value = 1\n" for n in range(999, -1, -1)}
        after = {path: "value = 2\n" for path in reversed(before)}
        result = changes.build_change_catalogue(before, after)
        self.assertEqual([entry["path"] for entry in result["files"]], sorted(before))
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), changes.MAX_CATALOGUE_BYTES)
        self.assertLessEqual(sum(len(entry["hunks"])+len(entry["units"]) for entry in result["files"]), 300)
        self.assertTrue(result["detail_limited"])
        self.assertTrue(any(entry["detail"]["hunks"] == "unavailable" for entry in result["files"]))
        self.assertEqual(result, changes.build_change_catalogue(dict(reversed(before.items())), after))

    def test_unicode_metadata_byte_budget_never_truncates_a_path(self):
        before = {"讀取.py": "def 函式(): return 1\n"}
        after = {"讀取.py": "def 函式(): return 2\n"}
        full = changes.build_change_catalogue(before, after)
        limit = len(json.dumps(full, ensure_ascii=False).encode("utf-8")) - 1
        with patch.object(changes, "MAX_CATALOGUE_BYTES", limit):
            limited = changes.build_change_catalogue(before, after)
        self.assertEqual(limited["files"][0]["path"], "讀取.py")
        self.assertLessEqual(len(json.dumps(limited, ensure_ascii=False).encode("utf-8")), limit)
        self.assertTrue(limited["detail_limited"])

    def test_unrepresentable_inventory_fails_explicitly_never_reports_no_changes(self):
        with self.assertRaisesRegex(changes.ChangeCatalogueError, "file_limit"):
            changes.build_change_catalogue({}, {f"f{n}.py": "" for n in range(1001)})
        with patch.object(changes, "MAX_CATALOGUE_BYTES", 100):
            with self.assertRaisesRegex(changes.ChangeCatalogueError, "path_inventory_limit"):
                self.catalogue("x\n", "y\n")

    def test_identical_maps_are_empty_without_a_semantic_or_execution_claim(self):
        result = self.catalogue("def same(): return 1\n", "def same(): return 1\n")
        self.assertEqual(result["files"], [])
        self.assertFalse(result["detail_limited"])
        self.assertFalse(result["semantics_verified"])


if __name__ == "__main__":
    unittest.main()
