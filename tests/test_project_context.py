"""Owned source strings are parsed/read, never executed, to check auto context."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge8 import discovery, explain
from forge8.desk import _valid_project_context


class ProjectContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="forge8-context-plan-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()

    def prepare(self, files, names):
        for name, text in files.items():
            (self.source / name).write_bytes(text.encode("utf-8"))
        prepared = explain.prepare_explanation(self.source, self.root / "run", "Explain the result.", "project-context")
        catalogue = discovery.prepare_discovery(prepared)
        candidates = tuple({"id": key, **row} for key, row in catalogue.candidates.items() if row["name"] in names)
        self.assertEqual(len(candidates), len(names))
        return prepared, catalogue, candidates

    def assert_complete(self, plan, candidates):
        actions = explain._focus_actions(plan.focus, focus_origin="project_candidates")
        read = {(item.path, line) for item in actions for line in range(item.start_line, item.end_line + 1)}
        required = {(item["path"], line) for item in candidates
            for line in range(item["start_line"], item["end_line"] + 1)}
        self.assertLessEqual(required, read)
        self.assertLessEqual(len(read), 240)
        return read

    def test_constant_helper_and_original_definition_are_retained_in_pinned_raw_source(self):
        text = ('DEFAULT = "中文"\r\nraise RuntimeError("never execute")\r\n'
            'def entry(value):\r\n    return helper(value, DEFAULT)\r\n\r\n'
            'def helper(value, fallback):\r\n    return value or fallback\r\n')
        prepared, catalogue, candidates = self.prepare({"app.py": text}, ["entry"])
        plan = discovery.plan_project_focus(prepared, catalogue, candidates)
        self.assertEqual(plan.supplements, ("app.py:1-1", "app.py:6-7"))
        self.assertEqual(plan.skipped, ())
        self.assertTrue({("app.py", line) for line in (1, 3, 4, 6, 7)} <= self.assert_complete(plan, candidates))
        self.assertEqual((self.source / "app.py").read_bytes(), text.encode("utf-8"))

    def test_one_hop_does_not_expand_references_inside_added_helper(self):
        text = 'SECOND = 9\n\ndef entry():\n    return helper()\n\ndef helper():\n    return SECOND\n'
        args = self.prepare({"app.py": text}, ["entry"])
        plan = discovery.plan_project_focus(*args)
        self.assertEqual(plan.supplements, ("app.py:6-7",))
        self.assertNotIn(("app.py", 1), self.assert_complete(plan, args[2]))

    def test_long_candidate_is_split_but_kept_complete_with_short_constant(self):
        text = 'LIMIT = 7\n\ndef entry():\n' + '    # retained\n' * 85 + '    return LIMIT\n'
        args = self.prepare({"app.py": text}, ["entry"])
        plan = discovery.plan_project_focus(*args)
        self.assertEqual(plan.supplements, ("app.py:1-1",))
        self.assertGreaterEqual(len(plan.focus), 2)
        self.assert_complete(plan, args[2])

    def test_all_six_original_candidates_survive_context_packing(self):
        text = 'LIMIT = 7\n\n' + '\n'.join(f'def step{i}():\n    return LIMIT\n' for i in range(6))
        args = self.prepare({"app.py": text}, [f"step{i}" for i in range(6)])
        plan = discovery.plan_project_focus(*args)
        self.assertEqual(plan.supplements, ("app.py:1-1",))
        self.assert_complete(plan, args[2])

    def test_gap_only_reference_does_not_seed_a_supplement(self):
        text = 'EXTRA = 99\n\ndef first(): return 1\nuse(EXTRA)\ndef second(): return 2\n'
        added, skipped = discovery._context_candidates({"app.py": text},
            {"app.py": [(3, 3), (5, 5)]}, ("app.py:3-5",))
        self.assertEqual((added, skipped), ((), {}))

    def test_enriched_plan_preserves_every_baseline_gap_line_too(self):
        text = ('def first():\n    return VALUE\nVALUE = 9\n'
            'def second():\n    return helper(VALUE)\n\n'
            'def helper(value):\n    return value + 1\n')
        args = self.prepare({"app.py": text}, ["first", "second"])
        base = discovery.plan_candidate_focus(*args)
        original = {(action.path, line) for action in explain._focus_actions(base,
            focus_origin="project_candidates") for line in range(action.start_line, action.end_line + 1)}
        self.assertIn(("app.py", 3), original, "the referenced declaration is already visible in a packing gap")
        plan = discovery.plan_project_focus(*args)
        self.assertEqual(plan.supplements, ("app.py:7-8",))
        self.assertLessEqual(original, self.assert_complete(plan, args[2]))

    def test_duplicate_inside_window_and_conditional_declarations_are_not_auto_added(self):
        text = ('VALUE = 1\n\ndef first(): return VALUE\nVALUE = 2\n'
            'def second(): return VALUE + MAYBE\nif condition:\n    MAYBE = 3\n')
        added, skipped = discovery._context_candidates({"app.py": text},
            {"app.py": [(3, 3), (5, 5)]}, ("app.py:3-5",))
        self.assertEqual(added, ())
        self.assertEqual(skipped, {"ambiguous": 1, "conditional": 1})

    def test_shadowed_parameter_does_not_add_unrelated_module_declaration(self):
        text = 'VALUE = 9\n\ndef entry(VALUE):\n    return VALUE\n'
        added, skipped = discovery._context_candidates({"app.py": text},
            {"app.py": [(3, 4)]}, ("app.py:3-4",))
        self.assertEqual((added, skipped), ((), {}))
        self.assertEqual(discovery._context_candidates({"app.py": text},
            {"app.py": [(3, 4)]}, ("app.py:1-4",)), ((), {}))

    def test_local_closure_and_comprehension_names_do_not_seed_module_context(self):
        cases = [
            ('VALUE = 9\ndef entry():\n    VALUE = 2\n    return VALUE\n', 2, 4),
            ('VALUE = 9\ndef outer():\n    VALUE = 2\n    def entry():\n        return VALUE\n', 4, 5),
            ('VALUE = 9\ndef entry():\n    return [VALUE for VALUE in range(3)]\n', 2, 3),
            ('VALUE = 9\ndef entry():\n    return (lambda VALUE: VALUE)(3)\n', 2, 3),
        ]
        for text, first, last in cases:
            with self.subTest(text=text):
                self.assertEqual(discovery._context_candidates({"app.py": text},
                    {"app.py": [(first, last)]}, (f"app.py:{first}-{last}",)), ((), {}))

    def test_parameter_default_keeps_real_global_dependency_separate_from_body(self):
        text = 'VALUE = 9\ndef entry(VALUE=VALUE):\n    return VALUE\n'
        self.assertEqual(discovery._context_candidates({"app.py": text},
            {"app.py": [(2, 3)]}, ("app.py:2-3",)), (("app.py:1-1",), {}))

    def test_class_global_does_not_make_nested_comprehension_a_module_dependency(self):
        text = ('VALUE = 0\ndef outer():\n    VALUE = 1\n    class Holder:\n'
            '        global VALUE\n        items = [VALUE for item in (0,)]\n')
        self.assertEqual(discovery._context_candidates({"app.py": text},
            {"app.py": [(6, 6)]}, ("app.py:6-6",)), ((), {}))

    def test_annotation_only_and_unbinding_never_become_unique_auto_context(self):
        cases = [
            'VALUE: int\ndef entry(): return VALUE\n',
            'VALUE = 1\ndel VALUE\ndef entry(): return VALUE\n',
            'VALUE = 1\nfrom unknown import *\ndef entry(): return VALUE\n',
        ]
        for text in cases:
            last = len(text.splitlines())
            with self.subTest(text=text):
                added, _ = discovery._context_candidates({"app.py": text},
                    {"app.py": [(last, last)]}, (f"app.py:{last}-{last}",))
                self.assertEqual(added, ())

    def test_control_and_augmented_writes_prevent_false_unique_module_supplement(self):
        statements = ['for VALUE in values:\n    pass', 'with manager as VALUE:\n    pass',
            'try:\n    run()\nexcept Exception as VALUE:\n    pass',
            'match item:\n    case {"key": VALUE}:\n        pass',
            '(VALUE := other)', 'VALUE += 1']
        for statement in statements:
            text = 'VALUE = 1\n' + statement + '\ndef entry(): return VALUE\n'
            last = len(text.splitlines())
            with self.subTest(statement=statement):
                added, _ = discovery._context_candidates({"app.py": text},
                    {"app.py": [(last, last)]}, (f"app.py:{last}-{last}",))
                self.assertEqual(added, ())

    def test_four_declaration_limit_and_whole_declaration_line_limit_are_disclosed(self):
        text = ''.join(f'V{i} = {i}\n' for i in range(5)) + '\ndef entry():\n    return V0 + V1 + V2 + V3 + V4\n'
        added, skipped = discovery._context_candidates({"app.py": text},
            {"app.py": [(7, 8)]}, ("app.py:7-8",))
        self.assertEqual(added, tuple(f"app.py:{i}-{i}" for i in range(1, 5)))
        self.assertEqual(skipped, {"limit": 1})
        text = 'def helper():\n' + '    # source\n' * 39 + '    return 1\n\ndef entry(): return helper()\n'
        self.assertEqual(discovery._context_candidates({"app.py": text},
            {"app.py": [(43, 43)]}, ("app.py:43-43",)), ((), {"limit": 1}))

    def test_optional_evidence_overflow_falls_back_to_identical_base_plan(self):
        text = 'VALUE = "' + 'x' * 2100 + '"\n\ndef entry():\n    return VALUE\n'
        args = self.prepare({"app.py": text}, ["entry"])
        baseline = discovery.plan_candidate_focus(*args)
        plan = discovery.plan_project_focus(*args)
        self.assertEqual(plan.focus, baseline)
        self.assertEqual(plan.supplements, ())
        self.assertEqual(dict(plan.skipped), {"capacity": 1})
        self.assert_complete(plan, args[2])

    def test_six_file_windows_cannot_squeeze_out_candidates_for_distant_constant(self):
        files = {f"file{i}.py": f"VALUE = 7\n" + "\n" * 80 + f"def entry{i}(): return VALUE\n" for i in range(6)}
        args = self.prepare(files, [f"entry{i}" for i in range(6)])
        baseline = discovery.plan_candidate_focus(*args)
        plan = discovery.plan_project_focus(*args)
        self.assertEqual(plan.focus, baseline)
        self.assertEqual(plan.supplements, ())
        self.assertEqual(dict(plan.skipped), {"capacity": 4, "limit": 2})
        self.assert_complete(plan, args[2])

    def test_static_unavailability_keeps_the_full_base_plan_and_discloses_no_analysis(self):
        args = self.prepare({"app.py": 'def entry(): return 7\n'}, ["entry"])
        with patch.object(discovery, "selected_context", return_value={"status": "limited", "candidates": []}):
            plan = discovery.plan_project_focus(*args)
        self.assertEqual(plan.focus, ("app.py:1-1",))
        self.assertEqual(dict(plan.skipped), {"unavailable": 1})

    def test_snapshot_drift_during_inspection_is_not_a_capacity_fallback(self):
        args = self.prepare({"app.py": 'VALUE = 7\n\ndef entry(): return VALUE\n'}, ["entry"])
        original = discovery.selected_context
        def drift(*items, **kwargs):
            result = original(*items, **kwargs)
            (Path(args[0].snapshot.snapshot_root) / "app.py").write_text("changed\n", encoding="utf-8")
            return result
        with patch.object(discovery, "selected_context", side_effect=drift):
            with self.assertRaises(explain.ExplanationError) as error:
                discovery.plan_project_focus(*args)
        self.assertNotIsInstance(error.exception, discovery.SelectionRequired)


class ProjectContextMetadataTests(unittest.TestCase):
    def test_optional_metadata_requires_exact_bounded_retained_coordinates(self):
        focus = [{"path": "app.py", "start_line": 1, "end_line": 80}]
        valid = {"added": [{"path": "app.py", "start_line": 1, "end_line": 3}], "skipped": {"ambiguous": 1}}
        self.assertTrue(_valid_project_context({}, focus))
        self.assertTrue(_valid_project_context({"context": valid}, focus))
        self.assertTrue(_valid_project_context({"context": {"added": [], "skipped": {}}}, []))
        invalid = [None, {}, {**valid, "unknown": True}, {**valid, "skipped": {"binding_proved": 1}},
            {**valid, "skipped": {"limit": True}}, {**valid, "skipped": {"limit": 0}},
            {**valid, "skipped": {"limit": 385}}, {**valid, "added": valid["added"] * 2}]
        invalid += [{**valid, "added": [{"path": path, "start_line": first, "end_line": last}]}
            for path, first, last in [("other.py", 1, 3), ("app.py", 1, 41), ("app.py", 79, 81), ("app.py", True, 3)]]
        for context in invalid:
            with self.subTest(context=context):
                self.assertFalse(_valid_project_context({"context": context}, focus))
        self.assertFalse(_valid_project_context({"context": valid}, []))


if __name__ == "__main__":
    unittest.main()
