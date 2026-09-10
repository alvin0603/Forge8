"""Owned source bytes are parsed as AST only, never imported or executed."""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

from forge8 import source_calls


def inventory(*specifications):
    files, contents, reads = [], {}, []
    for index, (path, source) in enumerate(specifications):
        raw = source.encode("utf-8") if isinstance(source, str) else source
        identifier = f"F{index}"
        files.append({"id": identifier, "path": path, "lines": len(raw.splitlines())})
        contents[identifier] = raw

    def read(file):
        reads.append(file["id"])
        raw = contents[file["id"]]
        return raw, hashlib.sha256(raw).hexdigest()

    return files, read, reads


class SourceCallsTests(unittest.TestCase):
    def scan(self, query, *sources, checkpoint=lambda: None):
        files, read, reads = inventory(*sources)
        result = source_calls.call_occurrences(query, files, read, checkpoint)
        self.assertEqual(set(result), {"query", "column_unit", "semantics_verified", "matches",
            "total_matches", "truncated", "inspected_files", "skipped_files",
            "uninspected_files", "total_files", "stop_reason"})
        self.assertEqual(result["query"], query)
        self.assertEqual(result["column_unit"], "utf8_bytes")
        self.assertIs(result["semantics_verified"], False)
        self.assertEqual(result["total_files"], len(files))
        self.assertEqual(result["inspected_files"] + sum(result["skipped_files"].values())
                         + result["uninspected_files"], len(files))
        self.assertIs(result["truncated"], result["total_matches"] > len(result["matches"]))
        self.assertLessEqual(len(result["matches"]), 40)
        for row in result["matches"]:
            self.assertEqual(set(row), {"file", "path", "source_sha256", "kind", "name",
                "start_line", "start_column", "end_line", "end_column", "name_line", "name_column", "preview"})
            self.assertEqual(row["name"], query)
            self.assertLessEqual(len(row["preview"]), 240)
            self.assertTrue(all(c.isprintable() for c in row["preview"]))
            file = next(file for file in files if file["id"] == row["file"])
            raw, digest = read(file)
            self.assertEqual(row["source_sha256"], digest)
            name = raw.splitlines()[row["name_line"] - 1]
            self.assertEqual(name[row["name_column"]:row["name_column"] + len(query.encode("utf-8"))],
                             query.encode("utf-8"))
        return result

    def test_exact_calls_only_not_comments_references_aliases_or_computed_callees(self):
        source = ('# foo()\ntext = "foo()"\nreference = foo\nalias = foo\nalias()\n'
                  'getattr(obj, "foo")()\nfoo(1)\nobj.foo(2)\nobj.myfoo(3)\n')
        result = self.scan("foo", ("calls.py", source))
        self.assertEqual([(row["start_line"], row["kind"], row["name_column"])
                          for row in result["matches"]], [(7, "name", 0), (8, "attribute", 4)])
        self.assertEqual([row["preview"] for row in result["matches"]], ["foo(1)", "obj.foo(2)"])
        self.assertEqual(result["total_matches"], 2)

    def test_shadowing_is_still_spelling_and_no_caller_attribution_is_invented(self):
        source = ("@foo()\ndef work(foo=foo()):\n    return foo()\n"
                  "class Child(foo()):\n    value = foo()\n")
        result = self.scan("foo", ("scope.py", source))
        self.assertEqual([row["start_line"] for row in result["matches"]], [1, 2, 3, 4, 5])
        self.assertTrue(all("caller" not in row for row in result["matches"]))

    def test_nested_same_line_occurrences_are_distinct_and_coordinate_ordered(self):
        result = self.scan("foo", ("nested.py", "foo(foo(1)); foo(2)\n"))
        self.assertEqual([(row["start_column"], row["end_column"])
                          for row in result["matches"]], [(0, 11), (4, 10), (13, 19)])
        self.assertEqual([row["preview"] for row in result["matches"]],
                         ["foo(foo(1))", "foo(1)", "foo(2)"])

    def test_multiline_attribute_terminal_and_parenthesized_name_coordinates(self):
        source = "(\n  obj\n  .foo\n)(\n  1\n)\n(foo)(2)\n"
        result = self.scan("foo", ("multiline.py", source))
        first, second = result["matches"]
        self.assertEqual((first["start_line"], first["start_column"], first["end_line"],
                          first["end_column"], first["name_line"], first["name_column"]),
                         (1, 0, 6, 1, 3, 3))
        self.assertEqual((second["start_line"], second["start_column"], second["end_column"],
                          second["name_column"]), (7, 0, 8, 1))
        self.assertIn("\\n", first["preview"])

    def test_raw_unicode_identifier_spelling_is_not_nfkc_equivalence(self):
        source = "K(); K(); obj.K(); obj.K(); ﬀ(); ff()\n"
        for query, expected in (("K", 2), ("K", 2), ("ﬀ", 1), ("ff", 1)):
            with self.subTest(query=query):
                result = self.scan(query, ("unicode.py", source))
                self.assertEqual(result["total_matches"], expected)
        result = self.scan("讀取", ("unicode.py", "標籤 = 1; 讀取(); 物件.讀取()\n"))
        self.assertEqual([row["name_column"] for row in result["matches"]],
                         [len("標籤 = 1; ".encode()), len("標籤 = 1; 讀取(); 物件.".encode())])

    def test_bom_and_crlf_return_raw_file_byte_columns(self):
        result = self.scan("foo", ("bom.PYI", b"\xef\xbb\xbffoo()\r\nfoo()\rfoo()"))
        self.assertEqual([(row["start_line"], row["start_column"], row["end_column"],
                           row["name_column"]) for row in result["matches"]],
                         [(1, 3, 8, 3), (2, 0, 5, 0), (3, 0, 5, 0)])
        self.assertEqual([row["preview"] for row in result["matches"]], ["foo()"] * 3)

    def test_preview_is_bounded_and_escapes_controls_without_changing_coordinates(self):
        source = 'foo(\n\t"' + "界" * 300 + '\u202e")\n'
        row, = self.scan("foo", ("preview.py", source))["matches"]
        self.assertLessEqual(len(row["preview"]), 240)
        self.assertTrue(row["preview"].endswith("…"))
        self.assertTrue(row["preview"].startswith('foo(\\n\\t"'))
        short, = self.scan("foo", ("preview.py", 'foo("\u202e")\n'))["matches"]
        self.assertEqual(short["preview"], 'foo("\\u202e")')
        self.assertEqual(row["end_column"], len(('\t"' + "界" * 300 + '\u202e")').encode()))

    def test_first_forty_file_order_rows_but_total_counts_every_completed_match(self):
        result = self.scan("foo", ("z.py", "foo()\n" * 38), ("a.py", "foo()\n" * 9))
        self.assertEqual(result["total_matches"], 47)
        self.assertIs(result["truncated"], True)
        self.assertEqual([row["path"] for row in result["matches"]], ["z.py"] * 38 + ["a.py"] * 2)
        self.assertEqual([row["start_line"] for row in result["matches"][-2:]], [1, 2])
        self.assertEqual(result["inspected_files"], 2)

    def test_physical_order_not_ast_field_order_when_keyword_precedes_starred_argument(self):
        source = "outer(option=foo(1), *foo(2))\n"
        rows = self.scan("foo", ("order.py", source))["matches"]
        self.assertEqual([row["preview"] for row in rows], ["foo(1)", "foo(2)"])

    def test_empty_inventory_and_empty_python_file_are_complete_not_limited(self):
        for sources, expected in (((), 0), ((("empty.py", ""),), 1)):
            with self.subTest(sources=sources):
                result = self.scan("foo", *sources)
                self.assertEqual(result["inspected_files"], expected)
                self.assertEqual(result["matches"], [])
                self.assertEqual(result["skipped_files"], {})
                self.assertIsNone(result["stop_reason"])

    def test_query_rejected_before_any_read_or_checkpoint(self):
        def forbidden(*args):
            self.fail("invalid query reached a callback")
        for query in (None, True, 1, "", " foo", "foo ", "foo.bar", "foo()", "for", "True",
                      "foo\n", "foo\u202e", "\ud800", "x" * 129):
            with self.subTest(query=repr(query)), self.assertRaises(ValueError):
                source_calls.call_occurrences(query, [], forbidden, forbidden)
        self.assertEqual(self.scan("x" * 128)["query"], "x" * 128)

    def test_file_inventory_is_validated_before_read_and_is_not_mutated(self):
        files, read, reads = inventory(("module.py", "foo()\n"))
        original = deepcopy(files)
        source_calls.call_occurrences("foo", files, read, lambda: None)
        self.assertEqual(files, original)
        for change in ({"path": "../module.py"}, {"path": "/module.py"}, {"path": "a\\b.py"},
                       {"path": "a//b.py"}, {"lines": True}, {"extra": 1}):
            malformed = [{**files[0], **change}]
            with self.subTest(change=change), self.assertRaises(ValueError):
                source_calls.call_occurrences("foo", malformed, lambda _file: self.fail("read"), lambda: None)
        with self.assertRaises(ValueError):
            source_calls.call_occurrences("foo", files * 2, read, lambda: None)
        self.assertEqual(reads, ["F0"])

    def test_skip_reasons_cover_entire_files_and_do_not_read_non_python(self):
        sources = (("notes.txt", "foo()"), ("large.py", b"#" * (256 * 1024 + 1)),
                   ("bad.py", "foo()\n("), ("encoding.py", b"foo()\n\xff"),
                   ("physical.py", 'foo("\u2028")'), ("good.py", "foo()\n"))
        files, read, reads = inventory(*sources)
        result = source_calls.call_occurrences("foo", files, read, lambda: None)
        self.assertEqual(result["skipped_files"], {"not_python": 1, "source_size": 1,
            "syntax": 1, "encoding": 1, "nonphysical_lines": 1})
        self.assertEqual((result["inspected_files"], result["uninspected_files"], result["total_matches"]), (1, 0, 1))
        self.assertNotIn("F0", reads)
        self.assertEqual(result["matches"][0]["path"], "good.py")

    def test_declared_physical_line_count_mismatch_is_coordinate_skip(self):
        files, read, _ = inventory(("calls.py", "foo()\n"))
        files[0]["lines"] = 2
        result = source_calls.call_occurrences("foo", files, read, lambda: None)
        self.assertEqual(result["skipped_files"], {"coordinates": 1})
        self.assertEqual(result["matches"], [])

    def test_bad_coordinate_in_later_matching_call_discards_earlier_matches(self):
        source = "foo()\nfoo()\n"
        tree = ast.parse(source)
        tree.body[1].value.end_col_offset = 100
        with patch.object(source_calls.ast, "parse", return_value=tree):
            result = self.scan("foo", ("coordinates.py", source))
        self.assertEqual(result["skipped_files"], {"coordinates": 1})
        self.assertEqual((result["total_matches"], result["matches"]), (0, []))

    def test_read_identity_or_stale_checkpoint_errors_are_not_coverage_skips(self):
        files, read, _ = inventory(("calls.py", "foo()\n"))
        for error in (ValueError("retained source changed"), OSError("read refused")):
            def refusal(_file):
                raise error
            with self.subTest(error=type(error)), self.assertRaises(type(error)):
                source_calls.call_occurrences("foo", files, refusal, lambda: None)
        with self.assertRaises(ValueError):
            source_calls.call_occurrences("foo", files, lambda _file: (b"foo()\n", "0" * 64), lambda: None)
        def stale():
            raise ValueError("snapshot changed")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            source_calls.call_occurrences("foo", files, read, stale)

    def test_file_node_limit_discards_partial_file_but_scans_later_file(self):
        with patch.object(source_calls, "_MAX_FILE_NODES", 10):
            result = self.scan("foo", ("large.py", "foo()\n" * 4), ("small.py", "foo()\n"))
        self.assertEqual(result["skipped_files"], {"node_limit": 1})
        self.assertEqual(result["inspected_files"], 1)
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["matches"][0]["path"], "small.py")
        self.assertIsNone(result["stop_reason"])

    def test_aggregate_budget_commits_only_prior_complete_files(self):
        first_nodes = len(list(ast.walk(ast.parse("foo()\n"))))
        with patch.object(source_calls, "_MAX_QUERY_NODES", first_nodes + 4):
            result = self.scan("foo", ("first.py", "foo()\n"),
                               ("partial.py", "foo()\n" * 4), ("later.py", "foo()\n"))
        self.assertEqual((result["stop_reason"], result["inspected_files"], result["uninspected_files"]),
                         ("node_budget", 1, 2))
        self.assertEqual(result["skipped_files"], {})
        self.assertEqual(result["total_matches"], 1)
        self.assertIs(result["truncated"], False, "incomplete coverage is not row truncation")

    def test_cooperative_time_stop_inside_ast_discards_current_and_uninspected_files(self):
        calls = 0
        def checkpoint():
            nonlocal calls
            calls += 1
            if calls == 3:  # Before file, before first node, then after 256 nodes.
                raise source_calls.ScanStopped("time_limit")
        result = self.scan("foo", ("partial.py", "foo()\n" * 100),
                           ("later.py", "foo()\n"), checkpoint=checkpoint)
        self.assertEqual((result["stop_reason"], result["inspected_files"], result["uninspected_files"]),
                         ("time_limit", 0, 2))
        self.assertEqual(result["total_matches"], 0)
        self.assertEqual(result["matches"], [])
        self.assertEqual(calls, 3)

    def test_stop_before_read_and_after_walk_both_prevent_partial_publication(self):
        for at in (1, 3, 4):  # File boundary, completed walk, completed row formatting.
            calls = 0
            def checkpoint():
                nonlocal calls
                calls += 1
                if calls == at:
                    raise source_calls.ScanStopped("time_limit")
            with self.subTest(at=at):
                result = self.scan("foo", ("calls.py", "foo()\n"), checkpoint=checkpoint)
                self.assertEqual((result["inspected_files"], result["uninspected_files"]), (0, 1))
                self.assertEqual((result["total_matches"], result["matches"]), (0, []))

    def test_target_source_is_not_imported_or_executed(self):
        source = 'raise RuntimeError("HOST MUST NEVER RUN THIS")\nfoo()\n'
        with patch("builtins.exec", side_effect=AssertionError("target exec")), \
                patch("importlib.import_module", side_effect=AssertionError("target import")):
            result = self.scan("foo", ("inert.py", source))
        self.assertEqual(result["total_matches"], 1)


if __name__ == "__main__":
    unittest.main()
