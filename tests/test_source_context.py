"""Owned source strings are parsed, never executed or imported as Python modules."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from forge8 import source_context


class SelectedContextTests(unittest.TestCase):
    def context(self, text, start, end, *, path="module.py"):
        result = source_context.selected_context(path, text, start, end)
        self.assertEqual(set(result), {"status", "reason", "candidates", "bindings", "semantics_verified", "scope"})
        self.assertIs(result["semantics_verified"], False)
        self.assertEqual(result["scope"], "same-file lexical scopes")
        self.assertIsInstance(result["candidates"], list)
        self.assertIsInstance(result["bindings"], list)
        self.assertLessEqual(len(result["bindings"]), 64)
        bindings = {row["id"]: row for row in result["bindings"]}
        self.assertEqual(len(bindings), len(result["bindings"]))
        for index, binding in enumerate(result["bindings"], 1):
            self.assertEqual(set(binding), {"id", "name", "classification", "scope",
                "scope_line", "use_lines", "reason"})
            self.assertEqual(binding["id"], f"B{index}")
            self.assertIn(binding["classification"], {"parameter", "local", "free",
                "module", "class", "annotation", "unresolved"})
            self.assertIs(type(binding["scope_line"]), int)
            self.assertGreaterEqual(binding["scope_line"], 1)
            self.assertEqual(binding["use_lines"], sorted(set(binding["use_lines"])))
            self.assertTrue(all(start <= line <= end for line in binding["use_lines"]))
        for candidate in result["candidates"]:
            self.assertEqual(set(candidate),
                {"name", "kind", "start_line", "end_line", "use_lines", "conditional", "binding_id"})
            binding = bindings[candidate["binding_id"]]
            self.assertEqual((candidate["name"], candidate["use_lines"]),
                (binding["name"], binding["use_lines"]))
            self.assertIs(type(candidate["conditional"]), bool)
            self.assertEqual(candidate["use_lines"], sorted(set(candidate["use_lines"])))
            self.assertTrue(all(start <= line <= end for line in candidate["use_lines"]))
        return result

    def available(self, text, start, end, *, path="module.py"):
        result = self.context(text, start, end, path=path)
        self.assertEqual((result["status"], result["reason"]), ("available", None))
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 64 * 1024)
        return [{key: value for key, value in row.items() if key != "binding_id"}
            for row in result["candidates"]]

    def refused(self, text, start, end, status, reason, *, path="module.py"):
        result = self.context(text, start, end, path=path)
        self.assertEqual((result["status"], result["reason"]), (status, reason))
        self.assertEqual(result["candidates"], [], "refused scans must not publish partial candidates")
        self.assertEqual(result["bindings"], [], "refused scans must not publish partial bindings")

    def test_omitted_default_has_original_declaration_and_selected_use_coordinates(self):
        text = 'DEFAULT = {"label": "unknown"}\n\ndef get_record(cache, key):\n    return cache.get(key) or DEFAULT\n'
        for path, content in (("before/cache.py", text),
                              ("after/cache.py", text.replace("cache.get(key) or DEFAULT", "cache.get(key, DEFAULT)"))):
            with self.subTest(path=path):
                self.assertEqual(self.available(content, 3, 4, path=path), [{
                    "name": "DEFAULT", "kind": "assignment", "start_line": 1,
                    "end_line": 1, "use_lines": [4], "conditional": False}])

    def test_use_lines_are_sorted_deduplicated_and_selection_local(self):
        text = "DEFAULT = 7\noutside = DEFAULT\ndef read():\n    first = DEFAULT\n    return DEFAULT + DEFAULT\nlater = DEFAULT\n"
        candidate, = self.available(text, 3, 5)
        self.assertEqual(candidate["use_lines"], [4, 5])
        self.assertEqual(candidate["name"], "DEFAULT")

    def test_fully_selected_declaration_is_omitted_but_partial_is_not(self):
        text = 'DEFAULT = (\n    {"label": "unknown"}\n)\ndef read():\n    return DEFAULT\n'
        self.assertEqual(self.available(text, 1, 5), [])
        candidate, = self.available(text, 3, 5)
        self.assertEqual((candidate["start_line"], candidate["end_line"]), (1, 3))

    def test_include_selected_retains_inside_and_outside_same_name_alternatives(self):
        text = "VALUE = 1\nVALUE = 2\ndef read():\n    return VALUE\n"
        default = self.context(text, 2, 4)
        explicit_false = source_context.selected_context("module.py", text, 2, 4,
            include_selected=False)
        self.assertEqual(json.dumps(default, ensure_ascii=False).encode("utf-8"),
            json.dumps(explicit_false, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(default["candidates"], [{"name": "VALUE", "kind": "assignment",
            "start_line": 1, "end_line": 1, "use_lines": [4], "conditional": False, "binding_id": "B1"}])
        included = source_context.selected_context("module.py", text, 2, 4,
            include_selected=True)
        self.assertEqual(included, {**default, "candidates": [default["candidates"][0],
            {"name": "VALUE", "kind": "assignment", "start_line": 2,
             "end_line": 2, "use_lines": [4], "conditional": False, "binding_id": "B1"}]})

    def test_include_selected_keeps_already_visible_whole_decorated_definition(self):
        text = "@decorate\ndef helper():\n    return 1\ndef read():\n    return helper()\n"
        default = self.context(text, 1, 5)
        self.assertEqual(default["candidates"], [])
        included = source_context.selected_context("module.py", text, 1, 5,
            include_selected=True)
        self.assertEqual(included, {**default, "candidates": [{"name": "helper",
            "kind": "function", "start_line": 1, "end_line": 3,
            "use_lines": [5], "conditional": False, "binding_id": "B2"}]})

    def test_include_selected_requires_a_keyword_boolean(self):
        for value in (None, 0, 1, "true", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                source_context.selected_context("module.py", "VALUE = 1\n", 1, 1,
                    include_selected=value)
        with self.assertRaises(TypeError):
            source_context.selected_context("module.py", "VALUE = 1\n", 1, 1, True)

    def test_include_selected_counts_visible_declarations_toward_existing_limits(self):
        text = "VALUE = 1\n" * 65 + "result = VALUE\n"
        self.assertEqual(self.available(text, 1, 66), [])
        included = source_context.selected_context("module.py", text, 1, 66,
            include_selected=True)
        self.assertEqual((included["status"], included["reason"], included["candidates"]),
            ("limited", "candidate_limit", []))
        self.assertIs(included["semantics_verified"], False)
        with patch.object(source_context, "_MAX_METADATA_BYTES", 160):
            included = source_context.selected_context("module.py", "VALUE = 1\nresult = VALUE\n",
                1, 2, include_selected=True)
        self.assertEqual((included["status"], included["reason"], included["candidates"]),
            ("limited", "metadata_limit", []))
        self.assertIs(included["semantics_verified"], False)

    def test_only_load_names_are_references_not_assignment_targets(self):
        self.assertEqual(self.available("VALUE = 1\nVALUE = 2\n", 2, 2), [])

    def test_duplicate_and_conditional_assignments_remain_separate_lexical_candidates(self):
        text = ("if enabled:\n    VALUE = 1\nelse:\n    VALUE = 2\ntry:\n"
            "    import package as VALUE\nexcept ImportError:\n    VALUE = 3\n"
            "VALUE = 4\ndef read():\n    return VALUE\n")
        candidates = self.available(text, 10, 11)
        self.assertEqual([(item["name"], item["start_line"], item["conditional"]) for item in candidates],
            [("VALUE", 2, True), ("VALUE", 4, True), ("VALUE", 6, True),
             ("VALUE", 8, True), ("VALUE", 9, False)])
        self.assertEqual([item["kind"] for item in candidates],
            ["assignment", "assignment", "import", "assignment", "assignment"])

    def test_duplicate_function_declarations_are_not_resolved_to_last_definition(self):
        text = "def helper():\n    return 1\ndef helper():\n    return 2\ndef read():\n    return helper()\n"
        self.assertEqual([(item["start_line"], item["end_line"]) for item in self.available(text, 5, 6)],
            [(1, 2), (3, 4)])

    def test_import_aliases_use_bound_lexical_name_without_resolving_modules(self):
        text = ("import package.sub\nimport other as alias\n"
            "from .helpers import Worker as Renamed, answer\nfrom missing import *\n"
            "def read():\n    return package, alias, Renamed, answer, missing\n")
        candidates = self.available(text, 5, 6)
        self.assertEqual({item["name"]: (item["kind"], item["start_line"], item["end_line"])
            for item in candidates}, {"package": ("import", 1, 1), "alias": ("import", 2, 2),
                "Renamed": ("from import", 3, 3), "answer": ("from import", 3, 3)})

    def test_assignment_unpacking_annotation_and_definition_kinds(self):
        text = ("LEFT, RIGHT = (1, 2)\nCOUNT: int = 3\ndef sync(): pass\n"
            "async def async_work(): pass\nclass Widget: pass\ndef read():\n"
            "    return LEFT, RIGHT, COUNT, sync, async_work, Widget\n")
        self.assertEqual({item["name"]: item["kind"] for item in self.available(text, 6, 7)}, {
            "LEFT": "assignment", "RIGHT": "assignment", "COUNT": "annotated assignment",
            "sync": "function", "async_work": "async function", "Widget": "class"})

    def test_function_and_class_body_declarations_are_not_module_candidates(self):
        text = ("def outer():\n    LOCAL = 1\n    def helper():\n        return 1\n"
            "    class Nested:\n        pass\nclass Holder:\n    CLASS_VALUE = 2\n"
            "    def method(self):\n        return 1\ndef read():\n"
            "    return LOCAL, helper, Nested, CLASS_VALUE, method\n")
        self.assertEqual(self.available(text, 11, 12), [])

    def test_parameter_shadowing_navigates_to_parameter_not_module_declaration(self):
        text = "DEFAULT = 7\ndef read(DEFAULT):\n    return DEFAULT\n"
        self.assertEqual(self.available(text, 2, 3), [])
        result = self.context(text, 3, 3)
        self.assertEqual([(row["kind"], row["start_line"]) for row in result["candidates"]],
            [("parameter", 2)])
        self.assertEqual(result["bindings"][0]["classification"], "parameter")
        self.assertEqual(result["bindings"][0]["scope"], "read")

    def test_attribute_name_and_transitive_initializer_are_not_resolved(self):
        text = ("DEFAULT = 7\nclass Service:\n    def DEFAULT(self):\n        return 7\n"
            "service = Service()\ndef read():\n    return service.DEFAULT()\n")
        candidate, = self.available(text, 6, 7)
        self.assertEqual((candidate["name"], candidate["start_line"]), ("service", 5))

    def test_source_import_raise_comments_and_fake_code_strings_are_never_executed(self):
        text = ("from forge8_owned_never_import_source import Marker\n"
            'raise RuntimeError("NEVER EXECUTE THIS READING SOURCE")\n'
            'FAKE = "DEFAULT = 999"\n# DEFAULT = 123\ndef read():\n    return DEFAULT\n')
        with patch("builtins.exec", side_effect=AssertionError("source execution forbidden")), \
                patch("builtins.eval", side_effect=AssertionError("source evaluation forbidden")):
            self.assertEqual(self.available(text, 5, 6), [])

    def test_unicode_crlf_and_decorators_preserve_whole_original_line_range(self):
        text = "常數 = 7\r\n@decorate\r\ndef 工具():\r\n    return 常數\r\n\r\ndef caller():\r\n    return 工具()\r\n"
        candidate, = self.available(text, 6, 7, path="after/工具.py")
        self.assertEqual(candidate, {"name": "工具", "kind": "function", "start_line": 2,
            "end_line": 4, "use_lines": [7], "conditional": False})
        self.assertEqual(self.available(text, 1, 7), [])

    def test_non_python_and_syntax_failure_have_explicit_noncomplete_status(self):
        self.refused("int value = 1;\n", 1, 1, "unsupported", "language", path="source.cpp")
        self.refused("def broken(:\n", 1, 1, "unavailable", "syntax_or_version")
        with patch.object(source_context.ast, "parse", side_effect=RecursionError("PRIVATE PARSER DETAIL")):
            self.refused("VALUE = 1\n", 1, 1, "unavailable", "parse_depth")

    def test_nonphysical_separators_and_unencodable_text_are_not_line_mapped(self):
        for separator in "\v\f\x1c\x1d\x1e\x85\u2028\u2029":
            with self.subTest(separator=repr(separator)):
                self.refused("# comment" + separator + "\nVALUE = 1\n", 1, 1,
                    "unavailable", "line_separators")
        self.refused("# \ud800\n", 1, 1, "unavailable", "utf8")

    def test_invalid_types_and_ranges_raise_instead_of_claiming_no_context(self):
        for path, text, start, end in ((None, "x=1\n", 1, 1), (1, "x=1\n", 1, 1),
                ("m.py", None, 1, 1), ("m.py", b"x=1\n", 1, 1),
                ("m.py", "x=1\n", True, 1), ("m.py", "x=1\n", 1, True),
                ("m.py", "x=1\n", 1.0, 1), ("m.py", "x=1\n", 1, "1"),
                ("m.py", "x=1\n", 0, 1), ("m.py", "x=1\n", 2, 1),
                ("m.py", "x=1\n", 1, 2), ("m.py", "x=1\n" * 81, 1, 81)):
            with self.subTest(path=path, text=text, start=start, end=end), self.assertRaises(ValueError):
                source_context.selected_context(path, text, start, end)

    def test_source_limit_counts_utf8_bytes_before_parsing(self):
        text = "#" + "é" * (256 * 1024 // 2) + "\n"
        with patch.object(source_context.ast, "parse") as parse:
            self.refused(text, 1, 1, "limited", "source_limit")
        parse.assert_not_called()

    def test_node_limit_never_leaks_partial_candidates(self):
        with patch.object(source_context, "_MAX_NODES", 2):
            self.refused("DEFAULT = 7\ndef read():\n    return DEFAULT\n", 2, 3, "limited", "node_limit")

    def test_name_limit_accepts_64_and_refuses_65_without_partial_list(self):
        for count in (64, 65):
            names = [f"NAME{index}" for index in range(count)]
            text = "".join(f"{name} = 1\n" for name in names) + "def read():\n    return " + ", ".join(names) + "\n"
            if count == 64:
                self.assertEqual(len(self.available(text, count + 1, count + 2)), 64)
            else:
                self.refused(text, count + 1, count + 2, "limited", "name_limit")

    def test_candidate_limit_counts_duplicate_alternatives_not_only_distinct_names(self):
        for count in (64, 65):
            text = "VALUE = 1\n" * count + "def read():\n    return VALUE\n"
            if count == 64:
                self.assertEqual(len(self.available(text, count + 1, count + 2)), 64)
            else:
                self.refused(text, count + 1, count + 2, "limited", "candidate_limit")

    def test_metadata_limit_refuses_whole_result_instead_of_truncating(self):
        with patch.object(source_context, "_MAX_METADATA_BYTES", 160):
            self.refused("DEFAULT = 7\ndef read():\n    return DEFAULT\n", 2, 3,
                "limited", "metadata_limit")

    def test_assignment_anywhere_in_function_prevents_module_fallback(self):
        text = "VALUE = 1\ndef read():\n    first = VALUE\n    VALUE = 2\n    return VALUE\n"
        result = self.context(text, 3, 3)
        binding, = result["bindings"]
        self.assertEqual((binding["classification"], binding["scope"], binding["reason"]),
            ("local", "read", None))
        self.assertEqual([(row["kind"], row["start_line"]) for row in result["candidates"]],
            [("assignment", 4)], "this identifies a local declaration, not successful runtime lookup")
        result = self.context(text, 3, 5)
        self.assertEqual(result["bindings"][0]["use_lines"], [3, 5])
        self.assertEqual(result["candidates"], [])

    def test_nested_helper_uses_enclosing_function_not_module_helper(self):
        text = ('def helper(): return "module"\ndef outer():\n'
            '    def helper():\n        return "closure"\n'
            '    def inner():\n        return helper()\n    return inner\n')
        result = self.context(text, 6, 6)
        binding, = result["bindings"]
        self.assertEqual((binding["classification"], binding["scope"], binding["scope_line"]),
            ("free", "outer.inner", 5))
        self.assertEqual([(row["start_line"], row["end_line"]) for row in result["candidates"]], [(3, 4)])

    def test_nonlocal_and_global_writes_join_the_actual_owner_inventory(self):
        text = ("VALUE = 1\ndef outer():\n    VALUE = 2\n    def inner():\n"
            "        nonlocal VALUE\n        VALUE += 1\n        return VALUE\n    return inner\n")
        result = self.context(text, 6, 6)
        self.assertEqual(result["bindings"][0]["classification"], "free")
        self.assertEqual([(row["kind"], row["start_line"]) for row in result["candidates"]],
            [("assignment", 3)])
        all_rows = source_context.selected_context("m.py", text, 6, 6, include_selected=True)
        self.assertEqual([(row["kind"], row["conditional"]) for row in all_rows["candidates"]],
            [("assignment", False), ("augmented assignment", True)])
        text = ("VALUE = 1\ndef mutate():\n    global VALUE\n    VALUE = 2\n"
            "def read():\n    return VALUE\n")
        result = self.context(text, 6, 6)
        self.assertEqual(result["bindings"][0]["classification"], "module")
        self.assertEqual([(row["start_line"], row["conditional"]) for row in result["candidates"]],
            [(1, False), (4, True)])

    def test_augmented_assignment_is_a_read_without_an_explicit_load_node(self):
        text = "COUNT = 0\ndef increment():\n    global COUNT\n    COUNT += 1\n"
        result = self.context(text, 4, 4)
        binding, = result["bindings"]
        self.assertEqual((binding["name"], binding["classification"], binding["use_lines"]),
            ("COUNT", "module", [4]))
        self.assertEqual(result["candidates"][0]["start_line"], 1)

    def test_defaults_and_decorators_use_enclosing_scope_not_parameters(self):
        text = ("VALUE = 1\nDECORATE = identity\n@DECORATE\n"
            "def read(VALUE=VALUE):\n    return VALUE\n")
        result = source_context.selected_context("m.py", text, 3, 5, include_selected=True)
        rows = {(row["name"], row["classification"]): row for row in result["bindings"]}
        self.assertEqual(rows["VALUE", "module"]["scope"], "<module>")
        self.assertEqual(rows["VALUE", "module"]["use_lines"], [4])
        self.assertEqual(rows["VALUE", "parameter"]["scope"], "read")
        self.assertEqual(rows["VALUE", "parameter"]["use_lines"], [5])
        self.assertEqual(rows["DECORATE", "module"]["use_lines"], [3])
        origins = {row["binding_id"]: row["start_line"] for row in result["candidates"]}
        self.assertEqual(origins[rows["VALUE", "module"]["id"]], 1)
        self.assertEqual(origins[rows["VALUE", "parameter"]["id"]], 4)

    def test_lambda_parameter_and_outer_call_argument_are_separate_bindings(self):
        text = "VALUE = 1\nanswer = (lambda VALUE: VALUE)(VALUE)\n"
        result = source_context.selected_context("m.py", text, 2, 2, include_selected=True)
        values = [row for row in result["bindings"] if row["name"] == "VALUE"]
        self.assertEqual({row["classification"] for row in values}, {"module", "parameter"})
        origins = {row["binding_id"]: row["kind"] for row in result["candidates"]}
        self.assertEqual({origins[row["id"]] for row in values}, {"assignment", "parameter"})

    def test_same_line_anonymous_scope_ambiguity_is_not_guessed(self):
        text = "VALUE = 1\npair = (lambda VALUE: VALUE, lambda VALUE: VALUE)\n"
        result = self.context(text, 2, 2)
        self.assertEqual(len(result["bindings"]), 2)
        self.assertEqual({row["classification"] for row in result["bindings"]}, {"unresolved"})
        self.assertEqual({row["reason"] for row in result["bindings"]}, {"scope_mapping"})
        self.assertEqual(result["candidates"], [])

    def test_comprehension_element_and_outermost_iterable_have_different_scopes(self):
        text = "VALUE = [1, 2]\nresult = [VALUE for VALUE in VALUE]\n"
        result = source_context.selected_context("m.py", text, 2, 2, include_selected=True)
        self.assertEqual({row["classification"] for row in result["bindings"]}, {"module", "local"})
        rows = {row["classification"]: row for row in result["bindings"]}
        self.assertEqual(rows["module"]["scope"], "<module>")
        self.assertEqual(rows["local"]["scope"], "<listcomp>")
        candidates = {row["binding_id"]: row for row in result["candidates"]}
        self.assertEqual(candidates[rows["module"]["id"]]["start_line"], 1)
        self.assertEqual(candidates[rows["local"]["id"]]["kind"], "loop target")

    def test_class_namespace_and_method_closure_are_not_the_same_binding(self):
        text = ("VALUE = 0\ndef outer():\n    VALUE = 1\n    class Holder:\n"
            "        VALUE = 2\n        copy = VALUE\n"
            "        def method(self):\n            return VALUE\n    return Holder\n")
        result = self.context(text, 6, 8)
        rows = {row["classification"]: row for row in result["bindings"] if row["name"] == "VALUE"}
        self.assertEqual(rows["class"]["scope"], "outer.Holder")
        self.assertEqual(rows["class"]["reason"], "class_namespace_lookup")
        self.assertEqual(rows["free"]["scope"], "outer.Holder.method")
        candidates = [row for row in result["candidates"] if row["binding_id"] == rows["free"]["id"]]
        self.assertEqual([row["start_line"] for row in candidates], [3])

    def test_private_class_names_are_mangled_without_matching_unrelated_module_name(self):
        text = ("__marker = 0\nclass Holder:\n    __marker = 1\n"
            "    def read(self, value=__marker):\n        return __marker\n")
        result = self.context(text, 4, 5)
        rows = {row["classification"]: row for row in result["bindings"]}
        self.assertEqual(rows["class"]["reason"], "class_namespace_lookup")
        self.assertEqual(rows["module"]["scope"], "Holder.read")
        self.assertEqual([(row["name"], row["start_line"]) for row in result["candidates"]],
            [("__marker", 3)])

    def test_control_and_expression_bindings_are_not_invisible_to_inventory(self):
        text = ("VALUE = 0\nfor VALUE in values:\n    pass\n"
            "with context as VALUE:\n    pass\n"
            "if (VALUE := compute()):\n    pass\n"
            "match subject:\n    case {'item': VALUE}:\n        pass\n"
            "def read():\n    return VALUE\n")
        result = self.context(text, 12, 12)
        self.assertEqual([row["kind"] for row in result["candidates"]],
            ["assignment", "loop target", "with target", "named assignment", "pattern target"])
        self.assertTrue(all(row["conditional"] for row in result["candidates"][1:]))
        self.assertEqual(len(result["bindings"]), 1)

    def test_exception_target_and_delete_caution_prevent_clean_binding_claim(self):
        text = ("ERROR = None\ntry:\n    operation()\nexcept Exception as ERROR:\n"
            "    print(ERROR)\ndef read():\n    return ERROR\n")
        result = self.context(text, 7, 7)
        self.assertEqual(result["bindings"][0]["reason"], "unbinding")
        self.assertEqual([row["kind"] for row in result["candidates"]], ["assignment", "exception target"])
        text = "VALUE = 1\ndel VALUE\ndef read():\n    return VALUE\n"
        result = self.context(text, 4, 4)
        self.assertEqual(result["bindings"][0]["reason"], "unbinding")
        self.assertEqual(result["candidates"][0]["start_line"], 1)

    def test_annotation_only_binding_and_annotation_uses_are_explicit(self):
        text = "VALUE: int\ndef read():\n    return VALUE\n"
        result = self.context(text, 3, 3)
        self.assertEqual(result["bindings"][0]["reason"], "annotation_only")
        result = self.context(text, 1, 1)
        self.assertEqual([(row["name"], row["classification"], row["reason"])
            for row in result["bindings"]], [("int", "annotation", "annotation_scope")])
        self.assertEqual(result["candidates"], [])

    def test_symbol_table_syntax_failure_and_depth_are_explicit_without_partial_output(self):
        self.refused("def read():\n    nonlocal missing\n    return missing\n", 3, 3,
            "unavailable", "syntax_or_version")
        with patch.object(source_context.symtable, "symtable", side_effect=RecursionError("PRIVATE")):
            self.refused("VALUE = 1\nresult = VALUE\n", 2, 2, "unavailable", "parse_depth")
        with patch.object(source_context.symtable, "symtable") as table, \
                patch.object(source_context, "_MAX_NODES", 2):
            self.refused("VALUE = 1\nresult = VALUE\n", 2, 2, "limited", "node_limit")
        table.assert_not_called()

    def test_distinct_use_scope_bindings_have_their_own_all_or_empty_limit(self):
        text = "VALUE = 1\n" + "".join(f"def f{i}(): return VALUE\n" for i in range(65))
        self.refused(text, 2, 66, "limited", "binding_limit")
        result = self.context(text, 2, 65)
        self.assertEqual(len(result["bindings"]), 64)
        self.assertEqual(len(result["candidates"]), 64)
        self.assertEqual({row["name"] for row in result["bindings"]}, {"VALUE"})

    def test_local_import_alias_navigates_to_local_import_not_same_module_name(self):
        text = ("alias = None\ndef read():\n    import never_import_source as alias\n"
            "    return alias\n")
        result = self.context(text, 4, 4)
        self.assertEqual(result["bindings"][0]["classification"], "local")
        self.assertEqual([(row["kind"], row["start_line"]) for row in result["candidates"]], [("import", 3)])

    def test_comprehension_captures_real_outer_local_without_leaking_iteration_target(self):
        text = "VALUE = 99\ndef read(items):\n    VALUE = 1\n    return [VALUE + item for item in items]\n"
        result = self.context(text, 4, 4)
        value, = [row for row in result["bindings"] if row["name"] == "VALUE"]
        self.assertEqual((value["classification"], value["scope"]), ("free", "read.<listcomp>"))
        self.assertEqual([row["start_line"] for row in result["candidates"]
            if row["binding_id"] == value["id"]], [3])
        item, = [row for row in result["bindings"] if row["name"] == "item"]
        self.assertEqual(item["classification"], "local")
        items, = [row for row in result["bindings"] if row["name"] == "items"]
        self.assertEqual((items["classification"], items["scope"]), ("parameter", "read"))

    def test_nested_comprehensions_keep_target_and_outer_iterable_scope(self):
        text = ("def read(items):\n"
            "    return [[outer + inner for inner in outer] for outer in items]\n")
        result = source_context.selected_context("m.py", text, 2, 2, include_selected=True)
        outer = [row for row in result["bindings"] if row["name"] == "outer"]
        self.assertEqual({(row["classification"], row["scope"]) for row in outer},
            {("local", "read.<listcomp>"), ("free", "read.<listcomp>.<listcomp>")})
        self.assertEqual(len({row["id"] for row in outer}), 2)
        for binding in outer:
            candidate, = [row for row in result["candidates"] if row["binding_id"] == binding["id"]]
            self.assertEqual(candidate["kind"], "loop target")

    def test_lambda_inside_comprehension_finds_isolated_target_after_inlining(self):
        text = "item = 99\ndef read(items):\n    return [lambda: item for item in items]\n"
        result = source_context.selected_context("m.py", text, 3, 3, include_selected=True)
        item, = [row for row in result["bindings"] if row["name"] == "item"]
        self.assertEqual((item["classification"], item["scope"]),
            ("free", "read.<listcomp>.<lambda>"))
        candidate, = [row for row in result["candidates"] if row["binding_id"] == item["id"]]
        self.assertEqual((candidate["kind"], candidate["start_line"]), ("loop target", 3))

    def test_class_comprehension_skips_class_local_but_outer_iterable_does_not(self):
        text = ("VALUE = 0\nclass Holder:\n    VALUE = [1]\n"
            "    result = [VALUE for item in VALUE]\n")
        result = self.context(text, 4, 4)
        rows = {row["classification"]: row for row in result["bindings"] if row["name"] == "VALUE"}
        self.assertEqual(rows["module"]["scope"], "Holder.<listcomp>")
        self.assertEqual(rows["class"]["scope"], "Holder")
        self.assertEqual(rows["class"]["reason"], "class_namespace_lookup")
        self.assertEqual([row["start_line"] for row in result["candidates"]
            if row["binding_id"] == rows["module"]["id"]], [1])

    def test_class_comprehension_can_capture_outer_function_across_class_local(self):
        text = ("VALUE = 0\ndef outer():\n    VALUE = 1\n    class Holder:\n"
            "        VALUE = 2\n        result = [VALUE for item in (1,)]\n    return Holder\n")
        result = self.context(text, 6, 6)
        value, = [row for row in result["bindings"] if row["name"] == "VALUE"]
        self.assertEqual(value["classification"], "free")
        self.assertEqual([row["start_line"] for row in result["candidates"]
            if row["binding_id"] == value["id"]], [3])

    def test_comprehension_walrus_writes_module_or_function_owner_not_iteration_scope(self):
        text = ("VALUE = 0\nvalues = [(VALUE := item) for item in items]\n"
            "def read():\n    return VALUE\n")
        result = self.context(text, 4, 4)
        self.assertEqual([row["kind"] for row in result["candidates"]],
            ["assignment", "named assignment"])
        self.assertTrue(result["candidates"][1]["conditional"])
        text = "def read(items):\n    values = [(VALUE := item) for item in items]\n    return VALUE\n"
        result = self.context(text, 3, 3)
        self.assertEqual(result["bindings"][0]["classification"], "local")
        self.assertEqual([(row["kind"], row["start_line"]) for row in result["candidates"]],
            [("named assignment", 2)])

    def test_nested_comprehension_walrus_escapes_all_comprehensions_and_respects_nonlocal(self):
        text = ("def outer():\n    VALUE = 0\n    def inner(items):\n"
            "        nonlocal VALUE\n"
            "        values = [[(VALUE := item) for item in group] for group in items]\n"
            "        return VALUE\n    return inner\n")
        result = self.context(text, 6, 6)
        self.assertEqual(result["bindings"][0]["classification"], "free")
        self.assertEqual([(row["kind"], row["start_line"]) for row in result["candidates"]],
            [("assignment", 2), ("named assignment", 5)])

    def test_unknown_scope_write_taints_possible_outer_owner_instead_of_fake_uniqueness(self):
        text = "VALUE = 0\ndef mutate():\n    global VALUE\n    VALUE = 2\nresult = VALUE\n"
        with patch.object(source_context, "_table_type", return_value="unknown"):
            result = self.context(text, 5, 5)
        binding, = result["bindings"]
        self.assertEqual((binding["classification"], binding["reason"]),
            ("module", "unsupported_binding"))
        self.assertEqual(result["candidates"][0]["start_line"], 1)

    def test_wildcard_import_cautions_module_candidate_without_resolving_or_importing_target(self):
        text = ("VALUE = 0\nfrom forge8_owned_never_import_source import *\n"
            "def read():\n    return VALUE\n")
        with patch("builtins.exec", side_effect=AssertionError("source execution forbidden")), \
                patch("builtins.eval", side_effect=AssertionError("source evaluation forbidden")):
            result = self.context(text, 4, 4)
        binding, = result["bindings"]
        self.assertEqual((binding["classification"], binding["reason"]),
            ("module", "unsupported_binding"))
        self.assertEqual([(row["kind"], row["start_line"]) for row in result["candidates"]],
            [("assignment", 1)])

    def test_implicit_class_cell_never_points_to_outer_same_named_local(self):
        for expression in ('def method(self):\n            return __class__',
                'items = [__class__ for item in (0,)]'):
            text = ('def outer():\n    __class__ = "outer"\n    class Holder:\n        '
                + expression + '\n    return Holder\n')
            line = 5 if expression.startswith('def') else 4
            with self.subTest(expression=expression):
                result = self.context(text, line, line)
                binding, = [row for row in result["bindings"] if row["name"] == "__class__"]
                self.assertEqual((binding["classification"], binding["reason"]),
                    ("unresolved", "implicit_class_cell"))
                self.assertFalse(any(row["binding_id"] == binding["id"] for row in result["candidates"]))

    def test_nearer_function_parameter_can_shadow_implicit_class_cell(self):
        text = ('class Holder:\n    def method(self, __class__):\n'
            '        def inner():\n            return __class__\n')
        result = self.context(text, 4, 4)
        binding, = result["bindings"]
        self.assertEqual(binding["classification"], "free")
        self.assertEqual([(row["kind"], row["start_line"]) for row in result["candidates"]],
            [("parameter", 2)])

    def test_class_global_declaration_does_not_change_nested_comprehension_scope(self):
        text = ('VALUE = 0\ndef outer():\n    VALUE = 1\n    class Holder:\n'
            '        global VALUE\n        items = [VALUE for item in (0,)]\n    return Holder\n')
        result = self.context(text, 6, 6)
        binding, = [row for row in result["bindings"] if row["name"] == "VALUE"]
        self.assertEqual((binding["classification"], binding["reason"]), ("free", None))
        self.assertEqual([row["start_line"] for row in result["candidates"]
            if row["binding_id"] == binding["id"]], [3])


if __name__ == "__main__":
    unittest.main()
