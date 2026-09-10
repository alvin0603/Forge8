from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from forge8 import discovery, explain
from forge8.inference import ChatResponse
from forge8.operator import AcceptanceGateResult
from test_explain import ScriptedBackend, passing_gate


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="forge8-discovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.text = ('raise RuntimeError("SOURCE MUST NEVER EXECUTE")\n'
            'def begin():\n    "BODY_ONLY_NOT_A_SUMMARY"\n    return 7\n')
        (self.source / "app.py").write_text(self.text, encoding="utf-8")
        self.question = "我是新同事，應該從哪些實作開始讀？"
        self.serial = 0

    def prepare(self):
        self.serial += 1
        return explain.prepare_explanation(self.source, self.root / f"run-{self.serial}",
            self.question, f"locate-{self.serial}")

    def assert_planned_reads(self, prepared, catalogue, candidates):
        focus = discovery.plan_candidate_focus(prepared, catalogue, candidates)
        self.assertIsInstance(focus, tuple)
        self.assertLessEqual(len(focus), 6)
        self.serial += 1
        focused = explain.prepare_explanation(self.source, self.root / f"focused-{self.serial}",
            prepared.question, f"focused-{self.serial}", focus=focus, focus_origin="project_candidates")
        self.assertEqual(focused.snapshot_sha256, prepared.snapshot_sha256)
        evidence, observed = [], set()
        with tempfile.TemporaryDirectory(dir=self.root) as scratch:
            artifacts = explain.ArtifactStore(Path(scratch))
            tools = explain.WorkspaceTools(explain.WorkspacePolicy(
                Path(prepared.snapshot.snapshot_root), allow_write=False,
                max_output_chars=4000), artifacts)
            for action in explain._focus_actions(focus, focus_origin="project_candidates"):
                result = tools.read_text(action.path, start_line=action.start_line, end_line=action.end_line)
                self.assertTrue(result.ok, result.error)
                self.assertEqual(result.preview, artifacts.read(result.artifact).decode("utf-8"))
                self.assertLessEqual(len(result.preview), 4000)
                lines = (self.source / action.path).read_text(encoding="utf-8").splitlines()
                self.assertEqual(result.preview, "\n".join(
                    f"{line:>6}|{lines[line - 1]}" for line in range(action.start_line, action.end_line + 1)))
                positions = {(action.path, line) for line in range(action.start_line, action.end_line + 1)}
                self.assertFalse(observed & positions, "overlapping candidates must not duplicate retained lines")
                observed.update(positions)
                item, reason = explain._focus_evidence(focused, artifacts, result, action, evidence)
                self.assertIsNotNone(item, reason)
            context = explain._reading_context(focused, evidence)
        required = {(row["path"], line) for row in candidates
            for line in range(row["start_line"], row["end_line"] + 1)}
        self.assertLessEqual(required, observed)
        self.assertLessEqual(len(observed), 240)
        self.assertLessEqual(len(explain._evidence_context(evidence)), 9000)
        self.assertLessEqual(len(context), 12000)
        self.assertTrue(context.endswith("USER QUESTION:\n" + prepared.question))
        return focus, evidence, observed

    def test_planner_retains_the_complete_long_definition_in_bounded_reads(self):
        (self.source / "app.py").write_text("def long_operation():\n"
            + "".join(f"    # step {i}\n" for i in range(118)) + "    return 7\n", encoding="utf-8")
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        candidates = discovery._parse_candidates('{"candidates":["D0001"]}', catalogue)
        self.assertEqual((candidates[0]["start_line"], candidates[0]["end_line"]), (1, 120))
        focus, _evidence, observed = self.assert_planned_reads(prepared, catalogue, candidates)
        self.assertGreaterEqual(len(focus), 2)
        self.assertEqual(observed, {("app.py", line) for line in range(1, 121)})

    def test_planner_keeps_decorators_stubs_and_contained_candidates_without_duplicates(self):
        (self.source / "app.py").write_text(
            "@decorate\ndef outer(value):\n    @inner_decorator\n    def inner():\n"
            "        return value\n    return inner()\n\n@overload\ndef api(value: int): ...\n"
            "\ndef api(value):\n    return outer(value)\n", encoding="utf-8")
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        candidates = discovery._parse_candidates(json.dumps({"candidates": list(catalogue.candidates)}), catalogue)
        self.assertEqual([row["stub"] for row in candidates], [False, False, True, False])
        _focus, evidence, observed = self.assert_planned_reads(prepared, catalogue, candidates)
        self.assertTrue({("app.py", line) for line in (1, 3, 8, 9, 12)} <= observed)
        self.assertIn("@overload", "\n".join(item.model_text for item in evidence))

    def test_planner_can_include_real_gap_lines_to_keep_all_six_candidates(self):
        (self.source / "app.py").write_text("\n\n".join(
            f"def step{i}(): return {i}" for i in range(6)) + "\n", encoding="utf-8")
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        candidates = discovery._parse_candidates(json.dumps({"candidates": list(catalogue.candidates)[::-1]}), catalogue)
        _focus, _evidence, observed = self.assert_planned_reads(prepared, catalogue, candidates)
        required = {("app.py", row["start_line"]) for row in candidates}
        self.assertEqual(len(required), 6)
        self.assertTrue(observed - required, "cheap gap lines can reduce repeated evidence headers")
        self.assertEqual(discovery.plan_candidate_focus(prepared, catalogue, candidates),
            discovery.plan_candidate_focus(prepared, catalogue, candidates), "planning must be deterministic")

    def test_planner_orders_files_by_first_candidate_not_alphabetically(self):
        (self.source / "other.py").write_text("def other(): return '中文'\n", encoding="utf-8")
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        candidates = discovery._parse_candidates('{"candidates":["D0002","D0001"]}', catalogue)
        focus, _evidence, _observed = self.assert_planned_reads(prepared, catalogue, candidates)
        self.assertTrue(focus[0].startswith("other.py:"))
        self.assertTrue(focus[-1].startswith("app.py:"))

    def test_planner_empty_or_unrepresentable_coverage_requires_manual_selection(self):
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        with self.assertRaises(discovery.SelectionRequired):
            discovery.plan_candidate_focus(prepared, catalogue, ())
        (self.source / "app.py").write_text("def long_operation():\n"
            + "    # source line\n" * 239 + "    return 7\n", encoding="utf-8")
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        candidates = discovery._parse_candidates('{"candidates":["D0001"]}', catalogue)
        self.assertEqual(candidates[0]["end_line"], 241)
        with self.assertRaises(discovery.SelectionRequired):
            discovery.plan_candidate_focus(prepared, catalogue, candidates)

    def test_planner_keeps_four_files_instead_of_using_manual_editor_capacity(self):
        for name in ("b.py", "c.py", "d.py"):
            (self.source / name).write_text("def short(): pass\n", encoding="utf-8")
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        candidates = discovery._parse_candidates(json.dumps({"candidates": list(catalogue.candidates)}), catalogue)
        self.assertEqual(len({row["path"] for row in candidates}), 4)
        focus, _evidence, _observed = self.assert_planned_reads(prepared, catalogue, candidates)
        self.assertEqual(len(focus), 4)

    def test_planner_keeps_six_distant_sites_across_three_files(self):
        for name in ("app.py", "b.py", "c.py"):
            (self.source / name).write_text("def first(): return 1\n" + "\n" * 89
                + "def second(): return 2\n", encoding="utf-8")
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        candidates = discovery._parse_candidates(json.dumps({"candidates": list(catalogue.candidates)}), catalogue)
        focus, _evidence, observed = self.assert_planned_reads(prepared, catalogue, candidates)
        self.assertEqual((len(focus), len(observed)), (6, 6))

    def test_planner_counts_gap_lines_in_the_unchanged_240_line_budget(self):
        blocks = [f"def part{i}():\n" + "    #\n" * 38 + "    return 7\n" for i in range(6)]
        (self.source / "app.py").write_text("\n".join(blocks), encoding="utf-8")
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        candidates = discovery._parse_candidates(json.dumps({"candidates": list(catalogue.candidates)}), catalogue)
        self.assertEqual(sum(row["end_line"] - row["start_line"] + 1 for row in candidates), 240)
        focus, _evidence, observed = self.assert_planned_reads(prepared, catalogue, candidates)
        self.assertEqual((len(focus), len(observed)), (6, 240))
        self.assertEqual(observed, {(row["path"], line) for row in candidates
            for line in range(row["start_line"], row["end_line"] + 1)})

    def test_planner_per_read_limit_counts_actual_numbered_characters_not_utf8_bytes(self):
        for length in (4000, 4001):
            with self.subTest(numbered_chars=length):
                prefix = "def readable(): return '"
                text = prefix + "中" * (length - 7 - len(prefix) - 1) + "'\n"
                self.assertEqual(len("     1|" + text.rstrip("\n")), length)
                (self.source / "app.py").write_text(text, encoding="utf-8")
                prepared = self.prepare()
                catalogue = discovery.prepare_discovery(prepared)
                candidates = discovery._parse_candidates('{"candidates":["D0001"]}', catalogue)
                if length == 4000:
                    focus, evidence, _observed = self.assert_planned_reads(prepared, catalogue, candidates)
                    self.assertEqual(focus, ("app.py:1-1",))
                    self.assertEqual(len(evidence[0].model_text), 4000)
                else:
                    with self.assertRaises(discovery.SelectionRequired):
                        discovery.plan_candidate_focus(prepared, catalogue, candidates)

    def test_planner_shared_budget_matches_real_evidence_registration(self):
        names = ("app.py", "b.py", "c.py")
        base = "def readable(): return '" + "x" * 2700 + "'"
        rows = [Mock(evidence_id=f"E{i + 1}", path=name, start_line=1, end_line=1,
            model_text="     1|" + base) for i, name in enumerate(names)]
        padding = 9000 - len(explain._evidence_context(rows))
        self.assertGreater(padding, 0)
        for extra in (0, 1):
            with self.subTest(shared_chars=9000 + extra):
                for i, name in enumerate(names):
                    text = base if i < 2 else base[:-1] + "x" * (padding + extra) + "'"
                    (self.source / name).write_text(text + "\n", encoding="utf-8")
                prepared = self.prepare()
                catalogue = discovery.prepare_discovery(prepared)
                candidates = discovery._parse_candidates(json.dumps({"candidates": list(catalogue.candidates)}), catalogue)
                if not extra:
                    _focus, evidence, _observed = self.assert_planned_reads(prepared, catalogue, candidates)
                    self.assertEqual(len(explain._evidence_context(evidence)), 9000)
                else:
                    with self.assertRaises(discovery.SelectionRequired):
                        discovery.plan_candidate_focus(prepared, catalogue, candidates)
                    with self.assertRaises(explain.ExplanationError):
                        explain._preflight_focus(replace(prepared, focus=tuple(name + ":1-1" for name in names)),
                            explain._focus_actions(tuple(name + ":1-1" for name in names)))

    def test_planner_matches_existing_context_preflight_with_long_paths_and_question(self):
        # Keep the real snapshot staging path below Windows' default MAX_PATH;
        # these filenames still reproduce the independent 12k prompt overflow.
        names = tuple(letter + "x" * 119 + ".py" for letter in "abc")
        base = "def readable(): return '" + "x" * 2700 + "'"
        rows = [Mock(evidence_id=f"E{i + 1}", path=name, start_line=1, end_line=1,
            model_text="     1|" + base) for i, name in enumerate(names)]
        padding = 9000 - len(explain._evidence_context(rows))
        self.assertGreater(padding, 0)
        for i, name in enumerate(names):
            text = base if i < 2 else base[:-1] + "x" * padding + "'"
            (self.source / name).write_text(text + "\n", encoding="utf-8")
            rows[i].model_text = "     1|" + text
        prepared = explain.prepare_explanation(self.source, self.root / "long-context",
            "Q" * 2000, "t" * 100)
        catalogue = discovery.prepare_discovery(prepared)
        candidates = discovery._parse_candidates(json.dumps({"candidates": [
            cid for cid, row in catalogue.candidates.items() if row["path"] in names]}), catalogue)
        focus = tuple(name + ":1-1" for name in names)
        focused = replace(prepared, focus=focus)
        self.assertEqual(len(explain._evidence_context(rows)), 9000)
        self.assertLessEqual(len(explain._reading_context(focused, rows)), 12000)
        self.assertGreater(len(explain._request_prefix(focused, rows)) + len(explain._FINAL_ACTION_SUFFIX) + 1, 12000)
        with self.assertRaises(explain.ExplanationError):
            explain._preflight_focus(focused, explain._focus_actions(focus))
        with self.assertRaises(discovery.SelectionRequired):
            discovery.plan_candidate_focus(prepared, catalogue, candidates)

    def test_planner_rejects_unbound_candidate_rows_as_errors_not_capacity(self):
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        (row,) = discovery._parse_candidates('{"candidates":["D0001"]}', catalogue)
        invalid = [({**row, "id": "D9999"},), ({**row, "path": "outside.py"},),
            ({**row, "start_line": 1},), ({**row, "end_line": 500},),
            ({**row, "name": "pretend"},), ({**row, "stub": 0},), ({**row, "extra": "not catalogue data"},),
            ({key: value for key, value in row.items() if key != "stub"},), (row, row), (1,)]
        for candidates in invalid:
            with self.subTest(candidates=candidates):
                with self.assertRaises(explain.ExplanationError) as caught:
                    discovery.plan_candidate_focus(prepared, catalogue, candidates)
                self.assertNotIsInstance(caught.exception, discovery.SelectionRequired)

    def test_planner_keeps_a_fitting_complete_plan_when_shorter_json_repeats_long_paths(self):
        names = tuple(letter + "x" * 119 + ".py" for letter in "abc")
        source_lines = [
            ["def first(): return '" + "x" * 900 + "'", "#" + "x" * 230,
             "def second(): return '" + "x" * 900 + "'"],
            ["def third(): return '" + "x" * 2900 + "'"],
            ["def fourth(): return '" + "x" * 2900 + "'" + " " * 399],
        ]
        for name, lines in zip(names, source_lines):
            (self.source / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        prepared = explain.prepare_explanation(self.source, self.root / "cost-plan", "Q" * 2000,
            "explain-20260909T045000Z-00000000")
        catalogue = discovery.prepare_discovery(prepared)
        candidates = discovery._parse_candidates(json.dumps({"candidates": [
            cid for cid, row in catalogue.candidates.items() if row["path"] in names]}), catalogue)
        split = ((names[0], 1, 1), (names[0], 3, 3), (names[1], 1, 1), (names[2], 1, 1))
        merged = ((names[0], 1, 3), (names[1], 1, 1), (names[2], 1, 1))
        for windows, expected in ((split, (8892, 12001)), (merged, (8940, 11920))):
            focus = tuple(f"{path}:{first}-{last}" for path, first, last in windows)
            views = [Mock(evidence_id=f"E{i}", path=path, start_line=first, end_line=last,
                model_text="\n".join(f"{line:>6}|{source_lines[names.index(path)][line - 1]}"
                    for line in range(first, last + 1)))
                for i, (path, first, last) in enumerate(windows, 1)]
            focused = replace(prepared, focus=focus, focus_origin="project_candidates")
            self.assertEqual((len(explain._evidence_context(views)),
                len(explain._request_prefix(focused, views)) + len(explain._FINAL_ACTION_SUFFIX) + 1), expected)
            self.assertLessEqual(len(explain._reading_context(focused, views)), 12000)
        focus = discovery.plan_candidate_focus(prepared, catalogue, candidates)
        self.assertEqual(focus, tuple(f"{path}:{first}-{last}" for path, first, last in merged))
        actual = explain.prepare_explanation(self.source, self.root / "actual-fit", prepared.question,
            prepared.task_id, focus=focus, focus_origin="project_candidates")
        self.assertEqual(actual.snapshot_sha256, prepared.snapshot_sha256)

    def test_planner_source_and_catalogue_drift_never_become_manual_selection(self):
        for target in ("source", "snapshot", "ingress", "catalogue", "memory"):
            with self.subTest(target=target):
                (self.source / "app.py").write_text(self.text, encoding="utf-8")
                prepared = self.prepare()
                catalogue = discovery.prepare_discovery(prepared)
                candidates = discovery._parse_candidates('{"candidates":["D0001"]}', catalogue)
                if target == "memory":
                    catalogue = replace(catalogue, context=catalogue.context + "changed")
                else:
                    path = {"source": self.source / "app.py",
                        "snapshot": Path(prepared.snapshot.snapshot_root) / "app.py",
                        "ingress": prepared.ingress_path,
                        "catalogue": prepared.run_root / "discovery-input.json"}[target]
                    path.write_text("changed\n", encoding="utf-8")
                with self.assertRaises(explain.ExplanationError) as caught:
                    discovery.plan_candidate_focus(prepared, catalogue, candidates)
                self.assertNotIsInstance(caught.exception, discovery.SelectionRequired)

        (self.source / "app.py").write_text(self.text, encoding="utf-8")
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        candidates = discovery._parse_candidates('{"candidates":["D0001"]}', catalogue)
        read = discovery._read_regular_file
        mutations = []

        def mutate_after_snapshot_read(*args):
            data = read(*args)
            (self.source / "app.py").write_text("changed after the pinned read\n", encoding="utf-8")
            mutations.append(True)
            return data

        with patch.object(discovery, "_read_regular_file", side_effect=mutate_after_snapshot_read):
            with self.assertRaises(explain.ExplanationError) as caught:
                discovery.plan_candidate_focus(prepared, catalogue, candidates)
        self.assertEqual(mutations, [True])
        self.assertNotIsInstance(caught.exception, discovery.SelectionRequired)

    def large_source(self):
        for name in ("a_bulk.py", "b_bulk.py"):
            (self.source / name).write_text("\n".join(
                f"def function_{i}_{'x' * 90}(): pass" for i in range(60)), encoding="utf-8")
        (self.source / "tests").mkdir(exist_ok=True)
        (self.source / "tests" / "test_route.py").write_text(
            "def test_route():\n    assert False\n", encoding="utf-8")
        (self.source / "types.pyi").write_text(
            "class API:\n    @overload\n    def send(self, item: int): ...\n"
            "    @overload\n    def send(self, item: str): ...\n"
            "    def send(self, item):\n        async def nested(): return item\n        return nested\n",
            encoding="utf-8")
        (self.source / "native.cpp").write_text("void unseen_body() {}\n", encoding="utf-8")
        (self.source / "empty.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.source / "README.md").write_text("R" * 1500 + "README_SUFFIX_NOT_SENT", encoding="utf-8")

    def test_large_catalogue_uses_two_requests_without_repeating_or_renumbering(self):
        self.large_source()
        for reader in ("qwen35", "gemma12b"):
            with self.subTest(reader=reader):
                prepared = self.prepare()
                catalogue = discovery.prepare_discovery(prepared)
                paths = ("types.pyi", "tests/test_route.py", "app.py")
                fids = tuple(next(key for key, path in catalogue.files.items() if path == wanted) for wanted in paths)
                allowed = [key for key, row in catalogue.candidates.items() if row["path"] in paths]
                chosen = [allowed[-1], allowed[0]]
                backend = ScriptedBackend([json.dumps({"files": fids}), json.dumps({"candidates": chosen})])
                gate = Mock(side_effect=passing_gate)
                outcome = discovery.run_discovery(prepared, catalogue, backend, model="test-model",
                    reader=reader, acceptance_gate=gate)
                self.assertTrue(outcome.ok, outcome.failure_reason)
                self.assertEqual(len(backend.requests), 2)
                self.assertEqual([row["id"] for row in outcome.candidates], chosen)
                gate.assert_called_once()
                first, second = backend.requests
                self.assertEqual(first.messages[1].content, catalogue.context)
                self.assertEqual(first.messages[0].content, discovery._FILE_DISCOVERY_PROMPT)
                self.assertEqual(second.messages[0].content, discovery._DISCOVERY_PROMPT)
                self.assertEqual(second.messages[1].content, discovery._definition_context(catalogue, fids))
                first_wire, second_wire = first.as_dict(), second.as_dict()
                for field in ("messages", "response_format"):
                    first_wire.pop(field); second_wire.pop(field)
                self.assertEqual(first_wire, second_wire)
                self.assertEqual((first.model, first.seed, first.max_tokens), ("test-model", 1, 4096))
                self.assertIs(first_wire["stream"], False)
                self.assertEqual(first.response_format["json_schema"]["schema"]["properties"]["files"]["items"]["enum"], list(catalogue.files))
                self.assertEqual(second.response_format["json_schema"]["schema"]["properties"]["candidates"]["items"]["enum"], allowed)
                self.assertEqual(outcome.as_dict()["scope"], {"mode": "files_then_definitions", "files": list(paths),
                    "total_files": len(prepared.snapshot.fingerprints), "total_functions": len(catalogue.candidates),
                    "selected_functions": len(allowed)})
                self.assertEqual(outcome.inference, {"calls": 2, "usage": {}, "timings": {}, "stages": [
                    {"stage": "files", "usage": {"prompt_tokens": 101, "completion_tokens": 21}, "timings": {"predicted_ms": 251.0}},
                    {"stage": "definitions", "usage": {"prompt_tokens": 102, "completion_tokens": 22}, "timings": {"predicted_ms": 252.0}}]})
                for filename, request in (("discovery-file-request.json", first), ("discovery-function-request.json", second)):
                    self.assertEqual(json.loads((prepared.run_root / filename).read_text(encoding="utf-8")), request.as_dict())
                for filename in ("discovery-file-response.json", "discovery-function-input.json", "discovery-function-response.json"):
                    self.assertTrue((prepared.run_root / filename).is_file())

    def test_large_map_keeps_all_paths_and_selected_catalogue_keeps_every_definition(self):
        self.large_source()
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        for fingerprint in prepared.snapshot.fingerprints:
            self.assertIn("FILE " + fingerprint.path, catalogue.context)
        self.assertNotIn("empty.py", catalogue.files.values())
        self.assertNotIn("native.cpp", catalogue.files.values())
        self.assertIn("R" * 1500, catalogue.context)
        self.assertNotIn("README_SUFFIX_NOT_SENT", catalogue.context)
        self.assertNotIn("function_0_", catalogue.context)
        self.assertLessEqual(len(catalogue.context), 12000)
        selected = ("types.pyi", "tests/test_route.py", "app.py")
        fids = tuple(next(fid for fid, path in catalogue.files.items() if path == name) for name in selected)
        context = discovery._definition_context(catalogue, fids)
        self.assertLess(context.index("FILE types.pyi"), context.index("FILE tests/test_route.py"))
        for cid, row in catalogue.candidates.items():
            if row["path"] in selected:
                self.assertIn(f"{cid} L{row['start_line']}-{row['end_line']} {row['name']}" + (" [stub]" if row["stub"] else ""), context)
            else:
                self.assertNotIn(cid + " ", context)
        for absent in ("SOURCE MUST NEVER EXECUTE", "BODY_ONLY_NOT_A_SUMMARY", "assert False", "return nested"):
            self.assertNotIn(absent, context)
        with self.assertRaises(TypeError):
            catalogue.files["F9999"] = "elsewhere.py"

    def test_file_ids_are_strict_and_definition_ids_cannot_escape_selected_files(self):
        self.large_source()
        catalogue = discovery.prepare_discovery(self.prepare())
        ids = list(catalogue.files)
        self.assertEqual(discovery._parse_files(json.dumps({"files": ids[2:0:-1]}), catalogue), tuple(ids[2:0:-1]))
        self.assertEqual(discovery._parse_files('{"files":[]}', catalogue), ())
        invalid = [json.dumps({"files": ids[:4]}), json.dumps({"files": [ids[0], ids[0]]}),
            '{"files":["F9999"]}', '{"files":["app.py"]}', '{"files":[1]}', '{"files":"F0001"}',
            '{"files":[],"answer":"no"}', '{"files":[],"files":[]}', '{"files":NaN}',
            '[]', '{}', '```json\n{"files":[]}\n```', '{"files":[]} trailing']
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                discovery._parse_files(text, catalogue)
        outside = next(cid for cid, row in catalogue.candidates.items() if row["path"] != "app.py")
        with self.assertRaises(ValueError):
            discovery._parse_candidates(json.dumps({"candidates": [outside]}), catalogue, allowed_files=("app.py",))

    def test_empty_files_finish_after_one_call_and_bad_responses_never_retry(self):
        self.large_source()
        for mode in ("empty", "bad_files", "file_length", "file_error", "wrong_subset", "definition_length", "definition_error"):
            with self.subTest(mode=mode):
                prepared = self.prepare()
                catalogue = discovery.prepare_discovery(prepared)
                fid = next(key for key, path in catalogue.files.items() if path == "app.py")
                outside = next(cid for cid, row in catalogue.candidates.items() if row["path"] != "app.py")
                first = ChatResponse(json.dumps({"files": [] if mode == "empty" else [fid]}), "stop", {}, {})
                second = ChatResponse(json.dumps({"candidates": [outside]}), "stop", {}, {})
                if mode == "bad_files":
                    first = ChatResponse('{"files":["F9999"]}', "stop", {}, {})
                elif mode == "file_length":
                    first = replace(first, finish_reason="length")
                elif mode == "file_error":
                    first = RuntimeError("synthetic transport error")
                elif mode == "definition_length":
                    second = replace(second, finish_reason="length")
                elif mode == "definition_error":
                    second = RuntimeError("synthetic transport error")
                backend, gate = Mock(spec=["chat"]), Mock(side_effect=passing_gate)
                backend.chat.side_effect = [first, second]
                outcome = discovery.run_discovery(prepared, catalogue, backend, model="test-model",
                    reader="qwen35", acceptance_gate=gate)
                expected_calls = 2 if mode.startswith("definition") or mode == "wrong_subset" else 1
                self.assertEqual(backend.chat.call_count, expected_calls)
                self.assertEqual(outcome.inference["calls"], expected_calls)
                gate.assert_called_once()
                self.assertEqual(outcome.ok, mode == "empty")
                self.assertEqual(outcome.candidates, ())
                if mode == "empty":
                    self.assertEqual(outcome.scope["files"], [])
                    self.assertEqual(outcome.scope["selected_functions"], 0)
                else:
                    self.assertNotIn("scope", outcome.as_dict())
                if expected_calls == 1:
                    self.assertFalse((prepared.run_root / "discovery-function-request.json").exists())

    def test_two_stage_mutation_cancellation_and_cleanup_never_publish_candidates(self):
        cases = [("first", target) for target in ("source", "snapshot", "ingress", "catalogue", "file_request", "cancel")]
        cases += [("second_progress", target) for target in ("file_response", "function_input", "function_request", "cancel", "interrupt")]
        cases += [("second", "function_input"), ("second", "cancel"),
            ("cleanup", "function_response"), ("cleanup", "cancel"), ("cleanup", "failed_gate"), ("before", "cancel")]
        for when, target in cases:
            with self.subTest(when=when, target=target):
                self.large_source()
                (self.source / "app.py").write_text(self.text, encoding="utf-8")
                prepared = self.prepare()
                catalogue = discovery.prepare_discovery(prepared)
                fid = next(key for key, path in catalogue.files.items() if path == "app.py")
                cid = next(key for key, row in catalogue.candidates.items() if row["path"] == "app.py")
                first_text, second_text = json.dumps({"files": [fid]}), json.dumps({"candidates": [cid]})
                backend = ScriptedBackend([])
                backend.cancel_event = threading.Event()
                mutations = []
                targets = {"source": self.source / "app.py", "snapshot": Path(prepared.snapshot.snapshot_root) / "app.py",
                    "ingress": prepared.ingress_path, "catalogue": prepared.run_root / "discovery-input.json"}
                for suffix in ("file_request", "file_response", "function_input", "function_request", "function_response"):
                    targets[suffix] = prepared.run_root / ("discovery-" + suffix.replace("_", "-") + ".json")

                def mutate():
                    if target == "cancel":
                        backend.cancel_event.set()
                    elif target == "interrupt":
                        mutations.append(target)
                        raise KeyboardInterrupt()
                    elif target != "failed_gate":
                        self.assertTrue(targets[target].is_file())
                        targets[target].write_text("changed\n", encoding="utf-8")
                    mutations.append(target)

                def first(_request):
                    if when == "first":
                        mutate()
                    return first_text

                def second(_request):
                    if when == "second":
                        mutate()
                    return second_text

                def progress(message):
                    if when == "second_progress" and "request 2/2" in message:
                        mutate()

                def cleanup():
                    if when == "cleanup":
                        mutate()
                    passed = passing_gate()
                    return AcceptanceGateResult(False, "synthetic failed cleanup", passed.evidence) if target == "failed_gate" else passed

                backend.outputs = [first, second]
                gate = Mock(side_effect=cleanup)
                if when == "before":
                    mutate()
                outcome = discovery.run_discovery(prepared, catalogue, backend, model="test-model",
                    reader="qwen35", acceptance_gate=gate, progress=progress)
                expected_calls = 0 if when == "before" else 1 if when in {"first", "second_progress"} else 2
                self.assertEqual(len(backend.requests), expected_calls)
                self.assertEqual(outcome.inference["calls"], expected_calls)
                # Callback assertions are caught by the runner; prove the intended
                # mutation actually happened rather than accepting that catch.
                self.assertEqual(mutations, [target])
                gate.assert_called_once()
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.candidates, ())
                self.assertNotIn("scope", outcome.as_dict())
                saved = json.loads((prepared.run_root / "discovery.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["candidates"], [])
                self.assertNotIn("scope", saved)

    def test_large_catalogue_files_readmes_and_candidate_binding_cannot_change(self):
        self.large_source()
        for field in ("files", "readmes", "candidates"):
            with self.subTest(field=field):
                prepared = self.prepare()
                original = discovery.prepare_discovery(prepared)
                value = {"files": {"F9999": "app.py"}, "readmes": ("CHANGED",), "candidates": {}}[field]
                backend = ScriptedBackend([])
                gate = Mock(side_effect=passing_gate)
                outcome = discovery.run_discovery(prepared, replace(original, **{field: value}), backend,
                    model="test-model", reader="qwen35", acceptance_gate=gate)
                self.assertEqual(backend.requests, [])
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.candidates, ())
                self.assertNotIn("scope", outcome.as_dict())
                gate.assert_called_once()

    def test_selected_file_combination_over_budget_cannot_trigger_a_second_call(self):
        self.large_source()
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        fids = tuple(fid for fid, path in catalogue.files.items() if path in {"a_bulk.py", "b_bulk.py"})
        for fid in fids:
            self.assertLessEqual(len(discovery._definition_context(catalogue, (fid,))), 12000)
        with self.assertRaisesRegex(ValueError, "Selected complete function catalogue"):
            discovery._definition_context(catalogue, fids)
        backend, gate = ScriptedBackend([json.dumps({"files": fids})]), Mock(side_effect=passing_gate)
        outcome = discovery.run_discovery(prepared, catalogue, backend, model="test-model",
            reader="qwen35", acceptance_gate=gate)
        self.assertFalse(outcome.ok)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(outcome.candidates, ())
        self.assertNotIn("scope", outcome.as_dict())
        gate.assert_called_once()

    def run_locator(self, outputs=None, *, reader="qwen35", gate=None):
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        backend = ScriptedBackend(outputs or ['{"candidates":["D0001"]}'])
        gate = Mock(side_effect=passing_gate) if gate is None else gate
        outcome = discovery.run_discovery(prepared, catalogue, backend, model="test-model",
            reader=reader, acceptance_gate=gate)
        return prepared, catalogue, backend, gate, outcome

    def test_catalogue_contains_every_admitted_function_without_body_summaries(self):
        (self.source / "tests").mkdir()
        (self.source / "tests" / "test_app.py").write_text(
            "def test_route():\n    assert False\n", encoding="utf-8")
        (self.source / "types.pyi").write_text(
            "class API:\n    @overload\n    def send(self, item: int): ...\n"
            "    @overload\n    def send(self, item: str): ...\n"
            "    def send(self, item):\n        async def nested(): return item\n        return nested\n",
            encoding="utf-8")
        (self.source / "native.cpp").write_text("void unseen_body() {}\n", encoding="utf-8")
        readme = "R" * 1500 + "README_SUFFIX_NOT_SENT"
        (self.source / "README.md").write_text(readme, encoding="utf-8")
        catalogue = discovery.prepare_discovery(self.prepare())
        rows = list(catalogue.candidates.values())
        self.assertEqual([row["name"] for row in rows],
            ["begin", "test_route", "API.send", "API.send", "API.send", "API.send.nested"])
        self.assertEqual([row["stub"] for row in rows], [False, False, True, True, False, False])
        self.assertEqual(list(catalogue.candidates), [f"D{i:04}" for i in range(1, 7)])
        self.assertEqual([row["start_line"] for row in rows[2:5]], [2, 4, 6])
        for path in ("app.py", "tests/test_app.py", "types.pyi", "native.cpp", "README.md"):
            self.assertIn("FILE " + path, catalogue.context)
        self.assertIn("R" * 1500, catalogue.context)
        self.assertIn(self.question, catalogue.context)
        for absent in ("SOURCE MUST NEVER EXECUTE", "BODY_ONLY_NOT_A_SUMMARY", "return nested",
                "unseen_body", "assert False", "README_SUFFIX_NOT_SENT"):
            self.assertNotIn(absent, catalogue.context)
        with self.assertRaises(TypeError):
            catalogue.candidates["D9999"] = rows[0]
        with self.assertRaises(TypeError):
            catalogue.candidates["D0001"]["name"] = "changed"

    def test_incomplete_python_index_or_empty_catalogue_is_not_silently_pruned(self):
        for source in ("def broken(\n", "# split\u2028here\ndef answer(): pass\n",
                "\n".join(f"def f{i}(): pass" for i in range(301)), "value = 1\n"):
            with self.subTest(source=source[:30]):
                (self.source / "app.py").write_text(source, encoding="utf-8")
                with self.assertRaises(ValueError):
                    discovery.prepare_discovery(self.prepare())

    def test_single_file_over_function_budget_stops_after_file_selection_without_clipping(self):
        (self.source / "app.py").write_text("\n".join(
            f"def function_{i}_{'x' * 90}(): pass" for i in range(150)), encoding="utf-8")
        prepared = self.prepare()
        catalogue = discovery.prepare_discovery(prepared)
        self.assertEqual(len(catalogue.candidates), 150)
        backend = ScriptedBackend([json.dumps({"files": list(catalogue.files)})])
        gate = Mock(side_effect=passing_gate)
        outcome = discovery.run_discovery(prepared, catalogue, backend, model="test-model",
            reader="qwen35", acceptance_gate=gate)
        self.assertFalse(outcome.ok)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(outcome.candidates, ())
        self.assertNotIn("scope", outcome.as_dict())
        self.assertFalse((prepared.run_root / "discovery-function-request.json").exists())
        gate.assert_called_once()

    def test_complete_file_map_over_budget_is_rejected_before_any_request(self):
        for index in range(90):
            (self.source / f"guide_{index}_{'x' * 120}.md").write_text("ordinary docs\n", encoding="utf-8")
        prepared = self.prepare()
        with self.assertRaisesRegex(ValueError, "context|catalogue|limit|budget"):
            discovery.prepare_discovery(prepared)
        self.assertFalse((prepared.run_root / "discovery-input.json").exists())

    def test_parser_accepts_only_unique_known_flat_ids_and_preserves_model_order(self):
        (self.source / "app.py").write_text("\n".join(
            f"def function_{i}(): pass" for i in range(7)), encoding="utf-8")
        catalogue = discovery.prepare_discovery(self.prepare())
        accepted = discovery._parse_candidates('{"candidates":["D0006","D0001"]}', catalogue)
        self.assertEqual([row["id"] for row in accepted], ["D0006", "D0001"])
        self.assertEqual(discovery._parse_candidates('{"candidates":[]}', catalogue), ())
        invalid = ['{"candidates":["D0001","D0001"]}', '{"candidates":["D9999"]}',
            '{"candidates":[1]}', '{"candidates":"D0001"}', '{"candidates":[{"id":"D0001"}]}',
            '{"candidates":[],"answer":"trust me"}', '{"candidates":[],"candidates":[]}',
            '{"candidates":NaN}', '[]', '{}', '```json\n{"candidates":[]}\n```',
            '{"candidates":[]} trailing', json.dumps({"candidates": [f"D{i:04}" for i in range(1, 8)]})]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                discovery._parse_candidates(text, catalogue)

    def test_one_request_uses_catalogue_only_and_gates_host_candidates(self):
        for reader, temperature, top_k in (("qwen35", .6, 20), ("gemma12b", 1.0, 64)):
            with self.subTest(reader=reader):
                prepared, catalogue, backend, gate, outcome = self.run_locator(reader=reader)
                gate.assert_called_once()
                self.assertTrue(outcome.ok, outcome.failure_reason)
                self.assertEqual(outcome.status, "located")
                self.assertEqual(len(backend.requests), 1)
                request = backend.requests[0]
                self.assertEqual(request.messages[1].content, catalogue.context)
                self.assertEqual(request.messages[0].content, discovery._DISCOVERY_PROMPT)
                self.assertEqual((request.model, request.temperature, request.top_p, request.top_k),
                    ("test-model", temperature, .95, top_k))
                self.assertEqual((request.max_tokens, request.seed), (4096, 1))
                self.assertIsNotNone(request.response_format)
                self.assertIs(request.as_dict()["stream"], False)
                self.assertEqual(outcome.as_dict()["candidates"], [{"id": "D0001", **catalogue.candidates["D0001"]}])
                self.assertTrue(outcome.source_unchanged)
                self.assertTrue(outcome.snapshot_unchanged)
                self.assertTrue(outcome.ingress_unchanged)
                self.assertEqual(outcome.inference["calls"], 1)
                self.assertEqual(outcome.as_dict()["catalogue_sha256"], catalogue.catalogue_sha256)
                self.assertNotIn("scope", outcome.as_dict())
                self.assertNotIn("stages", outcome.inference)
                self.assertIsNone(catalogue.files)
                self.assertEqual(catalogue.readmes, ())
                self.assertNotIn("files", json.loads(catalogue.input_bytes()))
                self.assertNotIn("readmes", json.loads(catalogue.input_bytes()))
                self.assertEqual(catalogue.context, "COMPLETE ADMITTED FILE / PYTHON FUNCTION CATALOGUE\n"
                    "FILE app.py\n  D0001 L2-4 begin\n\n\n\nUSER QUESTION\n" + self.question)
                self.assertEqual(json.loads(catalogue.input_bytes()), {
                    "kind": "forge8.discovery.input", "question": self.question,
                    "snapshot_sha256": prepared.snapshot_sha256, "system": discovery._DISCOVERY_PROMPT,
                    "context": catalogue.context, "candidates": {"D0001": dict(catalogue.candidates["D0001"])}})
                self.assertEqual((self.source / "app.py").read_text(encoding="utf-8"), self.text)
                self.assertTrue((prepared.run_root / "discovery-input.json").is_file())

    def test_malformed_nonstop_and_backend_failures_do_not_retry_or_return_candidates(self):
        for response in (ChatResponse('{"candidates":["D0001"]}', "length", {}, {}),
                ChatResponse('{"candidates":["D0001"]}', None, {}, {}),
                ChatResponse('{"candidates":["D9999"]}', "stop", {}, {}),
                RuntimeError("synthetic backend failure"), KeyboardInterrupt()):
            with self.subTest(response=type(response).__name__):
                prepared = self.prepare()
                catalogue = discovery.prepare_discovery(prepared)
                backend = Mock(spec=["chat"])
                if isinstance(response, BaseException):
                    backend.chat.side_effect = response
                else:
                    backend.chat.return_value = response
                gate = Mock(side_effect=passing_gate)
                outcome = discovery.run_discovery(prepared, catalogue, backend, model="test-model",
                    reader="qwen35", acceptance_gate=gate)
                backend.chat.assert_called_once(); gate.assert_called_once()
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.candidates, ())

    def test_failed_cleanup_never_exposes_otherwise_valid_candidates(self):
        passed = passing_gate()
        gate = Mock(return_value=AcceptanceGateResult(False, "synthetic shutdown failure", passed.evidence))
        _prepared, _catalogue, backend, _, outcome = self.run_locator(gate=gate)
        self.assertEqual(len(backend.requests), 1)
        gate.assert_called_once()
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "acceptance_gate_failed")
        self.assertEqual(outcome.candidates, ())

    def test_cancellation_after_response_or_during_gate_discards_valid_candidates(self):
        for stage in ("response", "gate"):
            with self.subTest(stage=stage):
                prepared = self.prepare()
                catalogue = discovery.prepare_discovery(prepared)
                backend = ScriptedBackend(['{"candidates":["D0001"]}'])
                backend.cancel_event = threading.Event()

                def response(_request):
                    backend.cancel_event.set()
                    return '{"candidates":["D0001"]}'

                def cleanup():
                    if stage == "gate":
                        backend.cancel_event.set()
                    return passing_gate()

                if stage == "response":
                    backend.outputs = [response]
                outcome = discovery.run_discovery(prepared, catalogue, backend, model="test-model",
                    reader="qwen35", acceptance_gate=cleanup)
                self.assertEqual(outcome.status, "interrupted")
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.candidates, ())
                self.assertEqual(len(backend.requests), 1)

    def test_callback_failures_still_close_gate_and_never_expose_candidates(self):
        for stage in ("progress", "cleanup"):
            with self.subTest(stage=stage):
                prepared = self.prepare()
                catalogue = discovery.prepare_discovery(prepared)
                backend = ScriptedBackend(['{"candidates":["D0001"]}'])
                gate = Mock(side_effect=RuntimeError("synthetic cleanup error") if stage == "cleanup" else passing_gate)
                progress = Mock(side_effect=KeyboardInterrupt()) if stage == "progress" else None
                outcome = discovery.run_discovery(prepared, catalogue, backend, model="test-model",
                    reader="qwen35", acceptance_gate=gate, progress=progress)
                gate.assert_called_once()
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.candidates, ())
                self.assertEqual(len(backend.requests), 0 if stage == "progress" else 1)

    def test_catalogue_preparation_rejects_prior_source_snapshot_or_ingress_drift(self):
        for target in ("source", "snapshot", "ingress"):
            with self.subTest(target=target):
                (self.source / "app.py").write_text(self.text, encoding="utf-8")
                prepared = self.prepare()
                path = {"source": self.source / "app.py",
                    "snapshot": Path(prepared.snapshot.snapshot_root) / "app.py",
                    "ingress": prepared.ingress_path}[target]
                path.write_text("changed\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    discovery.prepare_discovery(prepared)
                self.assertFalse((prepared.run_root / "discovery-input.json").exists())

    def test_pre_inference_drift_is_rejected_before_backend_and_post_drift_gates_candidates(self):
        for when in ("before", "during"):
            for target in ("source", "snapshot", "ingress", "catalogue"):
                with self.subTest(when=when, target=target):
                    (self.source / "app.py").write_text(self.text, encoding="utf-8")
                    prepared = self.prepare()
                    catalogue = discovery.prepare_discovery(prepared)
                    targets = {"source": self.source / "app.py",
                        "snapshot": Path(prepared.snapshot.snapshot_root) / "app.py",
                        "ingress": prepared.ingress_path,
                        "catalogue": prepared.run_root / "discovery-input.json"}

                    def mutate(_request=None):
                        targets[target].write_text("changed\n", encoding="utf-8")
                        return '{"candidates":["D0001"]}'

                    if when == "before":
                        mutate()
                    backend = ScriptedBackend([mutate if when == "during" else '{"candidates":["D0001"]}'])
                    gate = Mock(side_effect=passing_gate)
                    outcome = discovery.run_discovery(prepared, catalogue, backend, model="test-model",
                        reader="qwen35", acceptance_gate=gate)
                    self.assertEqual(len(backend.requests), 0 if when == "before" else 1)
                    gate.assert_called_once()
                    self.assertFalse(outcome.ok)
                    self.assertEqual(outcome.candidates, ())

    def test_prepared_catalogue_binding_cannot_be_replaced_in_memory(self):
        for field in ("context", "question", "snapshot_sha256", "catalogue_sha256"):
            with self.subTest(field=field):
                prepared = self.prepare()
                original = discovery.prepare_discovery(prepared)
                change = {field: getattr(original, field) + "CHANGED"}
                backend = ScriptedBackend(['{"candidates":["D0001"]}'])
                gate = Mock(side_effect=passing_gate)
                outcome = discovery.run_discovery(prepared, replace(original, **change), backend,
                    model="test-model", reader="qwen35", acceptance_gate=gate)
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.candidates, ())
                self.assertEqual(backend.requests, [])


if __name__ == "__main__":
    unittest.main()
