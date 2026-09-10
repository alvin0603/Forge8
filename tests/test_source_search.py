"""Owned inert cache fixtures; no source parsing, execution, model or filesystem."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from forge8 import source_search


def definition(name, start, end=None, *, kind="function", declaration=None, stub=False):
    return {"name": name, "kind": kind, "start_line": start,
            "definition_line": start if declaration is None else declaration,
            "end_line": start if end is None else end, "stub": stub}


def cache(*specifications):
    files, contents, outlines = [], {}, {}
    for index, (path, lines, items) in enumerate(specifications):
        identifier = str(index)
        files.append({"id": identifier, "path": path, "lines": len(lines)})
        contents[identifier] = lines
        outlines[identifier] = {"status": "available", "items": items}
    return files, contents, outlines


class SourceSearchTests(unittest.TestCase):
    def test_unicode_underscore_dot_camel_and_acronym_terms_preserve_first_order(self):
        empty = ([], {}, {})
        result = source_search.keyword_definitions("HTTPServer.user_config résumé_count Straße", *empty)
        self.assertEqual(result, {"terms": ["http", "server", "user", "config", "résumé", "count", "strasse"],
            "matches": [], "total_matches": 0, "truncated": False, "unavailable_files": 0})
        self.assertEqual(source_search.keyword_definitions("Foo_Bar foo.bar FOOBar", *empty)["terms"],
                         ["foo", "bar"])
        self.assertEqual(source_search.keyword_definitions("設定_目錄 設定", *empty)["terms"], ["設定", "目錄"])
        # No stemming, synonym expansion, Chinese segmentation or stopword removal.
        self.assertEqual(source_search.keyword_definitions("the settings Linux", *empty)["terms"],
                         ["the", "settings", "linux"])

    def test_query_admission_limits_words_not_raw_punctuation_or_repeated_terms(self):
        for query in (None, True, 1, "", " ", "x" * 129, "...___", "a\n", "a\t", "a\x00",
                      "a\u202e", "a\u2028", "a\ud800", "a b c d e f g h i"):
            with self.subTest(query=repr(query)), self.assertRaises(ValueError):
                source_search.keyword_definitions(query, [], {}, {})
        self.assertEqual(len(source_search.keyword_definitions("a b c d e f g h", [], {}, {})["terms"]), 8)
        self.assertEqual(source_search.keyword_definitions("x" * 128, [], {}, {})["terms"], ["x" * 128])
        self.assertEqual(source_search.keyword_definitions("x " * 60, [], {}, {})["terms"], ["x"])

    def test_all_terms_can_cross_name_path_and_own_source_with_explicit_first_hits(self):
        lines = ["def prepareRequest(self, request):",
                 "    headers = merge_setting(request.headers, self.headers)",
                 "    return headers"]
        values = cache(("src/client/http_service.py", lines, [definition("Session.prepareRequest", 1, 3)]))
        result = source_search.keyword_definitions("PREPARE http headers", *values)
        row = result["matches"][0]
        self.assertEqual(row["matched_terms"], [
            {"term": "prepare", "field": "name", "line": None},
            {"term": "http", "field": "path", "line": None},
            {"term": "headers", "field": "source", "line": 2},
        ])
        self.assertEqual(row["preview"], {"line": 2, "text": lines[1]})
        self.assertEqual(row["file"], "0")
        self.assertEqual(row["path"], values[0][0]["path"])
        # Name takes precedence over path/source; complete words, not substrings.
        rows = source_search.keyword_definitions("request headers", *values)["matches"]
        self.assertEqual(rows[0]["matched_terms"][0], {"term": "request", "field": "name", "line": None})
        for query in ("prepare missing", "prep", "merged headers"):
            self.assertEqual(source_search.keyword_definitions(query, *values)["matches"], [])

    def test_nested_functions_and_classes_are_source_exclusion_barriers(self):
        lines = [
            "def outer():",                         # 1
            "    own_start = 1",                    # 2
            "    def inner():",                     # 3
            "        needle = 2",                   # 4
            "        return needle",                # 5
            "    class HiddenType:",                # 6
            "        barrier_secret = 3",           # 7
            "        def method(self):",            # 8
            "            method_token = 4",         # 9
            "            return method_token",      # 10
            "    return own_finish",                # 11
        ]
        items = [definition("outer", 1, 11), definition("outer.inner", 3, 5),
                 definition("outer.HiddenType", 6, 10, kind="class"),
                 definition("outer.HiddenType.method", 8, 10)]
        values = cache(("owned.py", lines, items))
        result = source_search.keyword_definitions("outer needle", *values)
        self.assertEqual([row["name"] for row in result["matches"]], ["outer.inner"])
        self.assertEqual(result["matches"][0]["matched_terms"][1]["line"], 4)
        self.assertEqual(source_search.keyword_definitions("barrier secret", *values)["matches"], [])
        self.assertEqual([row["name"] for row in source_search.keyword_definitions("method token", *values)["matches"]],
                         ["outer.HiddenType.method"])
        result = source_search.keyword_definitions("own finish", *values)
        self.assertEqual([row["name"] for row in result["matches"]], ["outer"])
        self.assertEqual(result["matches"][0]["matched_terms"][1]["line"], 11)

    def test_comments_docstrings_and_decorators_are_lexical_hits_not_behavior_claims(self):
        lines = ["@memoize", "def read():", '    """needle documentation"""',
                 "    # needle comment", "    return 'needle'"]
        values = cache(("owned.py", lines, [definition("read", 1, 5, declaration=2)]))
        row = source_search.keyword_definitions("needle memoize", *values)["matches"][0]
        self.assertEqual(row["matched_terms"], [
            {"term": "needle", "field": "source", "line": 3},
            {"term": "memoize", "field": "source", "line": 1},
        ])
        self.assertEqual(row["preview"], {"line": 1, "text": "@memoize"})
        name_row = source_search.keyword_definitions("read", *values)["matches"][0]
        self.assertEqual(name_row["preview"], {"line": 2, "text": "def read():"})

    def test_ranking_is_name_then_path_then_span_path_and_definition_line(self):
        specs = [
            ("z.py", ["def merge_headers():"] + ["    pass"] * 8,
             [definition("merge_headers", 1, 9)]),
            ("headers.py", ["def merge():", "    return 1"], [definition("merge", 1, 2)]),
            ("a.py", ["def merge(): return 'headers'"], [definition("merge", 1)]),
            ("y.py", ["def first(): return 'merge headers'", "def second(): return 'merge headers'"],
             [definition("first", 1), definition("second", 2)]),
            ("b.py", ["def plain(): return 'merge headers'"], [definition("plain", 1)]),
            ("aa.py", ["def plain():", "    return 'merge headers'"], [definition("plain", 1, 2)]),
        ]
        expected = [("z.py", 1), ("headers.py", 1), ("a.py", 1),
                    ("b.py", 1), ("y.py", 1), ("y.py", 2), ("aa.py", 1)]
        for ordered in (specs, list(reversed(specs))):
            result = source_search.keyword_definitions("merge headers", *cache(*ordered))
            self.assertEqual([(row["path"], row["definition_line"]) for row in result["matches"]], expected)

    def test_stubs_async_and_duplicate_names_remain_distinct_but_classes_are_not_results(self):
        lines = ["def repeated(): ...", "def repeated(): pass", "async def repeated(): pass",
                 "class Repeated: pass"]
        items = [definition("repeated", 1, stub=True), definition("repeated", 2),
                 definition("repeated", 3, kind="async function"),
                 definition("Repeated", 4, kind="class")]
        result = source_search.keyword_definitions("repeated", *cache(("owned.pyi", lines, items)))
        self.assertEqual(result["total_matches"], 3)
        self.assertEqual([row["definition_line"] for row in result["matches"]], [1, 2, 3])
        self.assertEqual([row["stub"] for row in result["matches"]], [True, False, False])
        self.assertEqual(result["matches"][2]["kind"], "async function")

    def test_exact_total_top40_and_unavailable_count_survive_earlier_result_overflow(self):
        lines = ["def repeated(): pass"] * 43
        items = [definition("repeated", number) for number in range(1, 44)]
        files, contents, outlines = cache(("owned.py", lines, list(reversed(items))),
            ("broken.py", ["broken"], []), ("limited.pyi", ["limited"], []), ("README.md", [], []))
        outlines["1"] = {"status": "unavailable", "items": [], "reason": "syntax_or_version"}
        outlines["2"] = outlines["3"] = {"status": "limited", "items": [], "reason": "metadata_limit"}
        result = source_search.keyword_definitions("repeated", files, contents, outlines)
        self.assertEqual(result["total_matches"], 43)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["unavailable_files"], 2)
        self.assertEqual([row["definition_line"] for row in result["matches"]], list(range(1, 41)))
        outlines["0"]["items"] = items[:40]
        result = source_search.keyword_definitions("repeated", files, contents, outlines)
        self.assertEqual(result["total_matches"], 40)
        self.assertFalse(result["truncated"])

    def test_preview_alone_is_bounded_and_oversized_metadata_fails_whole_response(self):
        line = "def needle(): return '" + "x" * 1000 + "'"
        row = source_search.keyword_definitions("needle", *cache(("owned.py", [line],
            [definition("needle", 1)])))["matches"][0]
        self.assertEqual(row["preview"]["text"], line[:240])
        specs = [(f"file{index:02}.py", ["def needle(): pass"],
                  [definition("A" * 1800 + ".needle", 1)]) for index in range(40)]
        with self.assertRaisesRegex(ValueError, "response exceeds 65536 bytes"):
            source_search.keyword_definitions("needle", *cache(*specs))

    def test_single_interval_sweep_does_not_rescan_outer_source_or_reparse_cache(self):
        class CountingLines(list):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        lines = CountingLines(["def outer():", "    def inner():", "        return needle",
                               "    return outer_only"])
        values = cache(("owned.py", lines, [definition("outer", 1, 4), definition("outer.inner", 2, 3)]))
        original = deepcopy(values)
        lines.iterations = 0
        with patch("ast.parse", side_effect=AssertionError("no reparse")), \
                patch("builtins.compile", side_effect=AssertionError("no compilation")), \
                patch("builtins.exec", side_effect=AssertionError("no execution")), \
                patch("builtins.eval", side_effect=AssertionError("no evaluation")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("no file reads")), \
                patch("subprocess.Popen", side_effect=AssertionError("no processes")):
            result = source_search.keyword_definitions("needle", *values)
        self.assertEqual(lines.iterations, 1)
        self.assertEqual(values, original)
        self.assertEqual([row["name"] for row in result["matches"]], ["outer.inner"])

    def test_bad_cached_coordinates_or_crossing_intervals_never_invent_source_matches(self):
        invalid = [definition("needle", 0), definition("needle", True), definition("needle", 1, 4),
                   definition("needle", 2, 3, declaration=1)]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                source_search.keyword_definitions("needle", *cache(("owned.py", ["x"] * 3, [item])))
        for items in ([definition("first", 1, 3), definition("second", 2, 4)],
                      [definition("first", 1, 3), definition("second", 1, 3)]):
            with self.subTest(items=items), self.assertRaisesRegex(ValueError, "overlap ambiguously"):
                source_search.keyword_definitions("needle", *cache(("owned.py", ["needle"] * 4, items)))


if __name__ == "__main__":
    unittest.main()
