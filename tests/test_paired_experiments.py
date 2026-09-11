"""Paired HEAD/current trials: owned inert source and mocked execution only."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from forge8 import comparison, desk as desk_module, experiments
from forge8.checks import CheckCleanupError
from forge8.git_source import GitBaseline
from forge8.operator import AcceptanceGateResult
from test_desk import DeskFixture


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ReportedComparisonTests(unittest.TestCase):
    def test_canonical_json_preserves_types_integer_precision_and_array_order(self):
        large = 10**80 + 9007199254740993
        pairs = [
            ({"return": {"z": [large, "中文"], "a": None}},
             {"return": {"a": None, "z": [large, "中文"]}}, "same"),
            ({"return": large}, {"return": large + 1}, "different"),
            ({"return": True}, {"return": 1}, "different"),
            ({"return": 1}, {"return": 1.0}, "different"),
            ({"return": [1, 2]}, {"return": [2, 1]}, "different"),
            ({"return": None}, {"return": None}, "same"),
        ]
        for left, right, expected in pairs:
            with self.subTest(left=left, right=right):
                before = deepcopy((left, right))
                self.assertEqual(experiments.compare_reported_results(left, right), expected)
                self.assertEqual((left, right), before)

    def test_exception_phase_and_message_are_compared_not_promoted_to_success(self):
        original = {"exception": "ValueError", "message": "owned failure", "phase": "call"}
        self.assertEqual(experiments.compare_reported_results(original, dict(reversed(list(original.items())))), "same")
        for changed in ({**original, "phase": "module_initialization"},
                        {**original, "phase": "serialization"},
                        {**original, "message": "different failure"}, {"return": None}):
            with self.subTest(changed=changed):
                self.assertEqual(experiments.compare_reported_results(original, changed), "different")

    def test_unavailable_envelopes_non_json_and_bounded_depth_are_not_equal(self):
        nested = 0
        for _ in range(65):
            nested = [nested]
        cycle = []
        cycle.append(cycle)
        invalid = [None, {}, [], {"return": 1, "extra": True},
            {"exception": "ValueError", "message": "x", "phase": "unknown"},
            {"exception": "ValueError", "message": 1, "phase": "call"},
            {"return": (1, 2)}, {"return": {1: "not a JSON key"}},
            {"return": float("nan")}, {"return": float("inf")},
            {"return": nested}, {"return": cycle},
            {"return": "x" * (6 * experiments.OUTPUT_BYTES)}]
        for index, value in enumerate(invalid):
            with self.subTest(index=index):
                self.assertEqual(experiments.compare_reported_results(value, value), "unavailable")
                self.assertEqual(experiments.compare_reported_results({"return": None}, value), "unavailable")

    def test_input_parser_recursion_becomes_admission_value_error(self):
        raw = b'{"args":[' + b"[" * 4000 + b"0" + b"]" * 4000 + b'],"kwargs":{}}'
        self.assertLess(len(raw), experiments.MAX_INPUT_BYTES)
        with self.assertRaises(ValueError):
            experiments.prepare_inputs(raw)
        with patch.object(experiments, "_json", side_effect=RecursionError("owned parser limit")):
            with self.assertRaisesRegex(ValueError, "nesting"):
                experiments.prepare_inputs(b'{"args":[],"kwargs":{}}')

    def test_explicit_input_pin_is_checked_before_runtime_or_worker(self):
        with patch.object(experiments, "prepare_request", return_value=({}, {
                "source_sha256": "a" * 64, "input_sha256": "b" * 64})), \
                patch.object(experiments, "verify_experiment_runtime") as verify, \
                patch.object(experiments, "_worker") as worker:
            with self.assertRaisesRegex(ValueError, "input changed"):
                experiments.run_experiment(Path("runtime"), Path("source.py"), "entry",
                    Path("arguments.json"), Path("run"), allow_execution=True,
                    expected_source_sha256="a" * 64, expected_input_sha256="c" * 64)
            verify.assert_not_called()
            worker.assert_not_called()


class PairedExperimentTests(DeskFixture):
    def setUp(self):
        super().setUp()
        self.desk.close()
        self.code = (
            '# Owned inert fixture; never execute.\r\nTAG = "x\u200by"\r\n'
            "raise RuntimeError('must not execute this fixture')\r\n"
            "def entry(value):\r\n    return {'value': value, 'tag': TAG}\r\n"
        ).encode("utf-8")
        self.before = self.code.replace(b"'value': value,", b"'value': value + 1,")
        self.live = self.source / "main.py"
        self.live.write_bytes(self.code)
        self.head = "a" * 40
        self.operations = []
        patches = [
            patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.root / "state")}),
            patch.object(experiments, "native_platform", return_value="linux"),
            patch.object(comparison, "read_head", return_value=GitBaseline(self.head, {"main.py": self.before}, ())),
            patch.object(comparison, "head_identity", return_value=self.head),
        ]
        for pending in patches:
            started = pending.start()
            self.addCleanup(pending.stop)
            self.operations.append(started)
        self.git_read, self.git_head = self.operations[-2:]
        self.guards = []
        for owner, name in ((experiments, "run_experiment"), (experiments, "_worker"),
                            (desk_module, "_run_explain_cli"), (desk_module, "_run_locate_cli"),
                            (desk_module, "_run_project_cli")):
            pending = patch.object(owner, name, side_effect=AssertionError("unmocked execution"))
            self.guards.append(pending.start())
            self.addCleanup(pending.stop)
        process = patch("subprocess.Popen", side_effect=AssertionError("no subprocess allowed"))
        self.process_guard = process.start()
        self.addCleanup(process.stop)
        self.runtime = self.assets / f"experiment-{experiments.RUNTIME_NAME}-linux"
        self.runtime.mkdir()
        (self.runtime / "runtime.json").write_bytes(b"{}")  # Availability only.
        self.desk = desk_module.ReadingDesk(self.source, allow_experiments=True)
        self.addCleanup(self.desk.close)
        self.desk.refresh(mode="changes")
        self.runtime_identity = {"protocol": 1, "platform": "linux", "python": "3.14.7",
            "wasmtime": "48.0.0", "cache": {"sha256": "c" * 64, "size_bytes": 17}}
        self.reports = {}

    def target(self, path="after/main.py"):
        item = next(item for item in self.desk.project["files"] if item["path"] == path)
        return {"file": item["id"], "version": self.desk.project["version"],
            "entry": "entry", "mode": "head_current"}

    def payload(self):
        target = self.desk.prepare_experiment(self.target())
        return {key: target[key] for key in ("file", "version", "entry", "source_sha256", "mode", "head")} | {
            "before_sha256": target["before"]["source_sha256"],
            "input_text": ' {"args":[9007199254740993],"kwargs":{"label":"中文"}}\n',
            "allow_execution": True,
        }

    def write_report(self, source, entry, inputs, destination, result, **changes):
        raw_source, raw_input = source.read_bytes(), inputs.read_bytes()
        report = {"schema_version": 1, "kind": "forge8.experiment",
            "identity": {"source_sha256": sha(raw_source), "input_sha256": sha(raw_input),
                "entry": entry, "source_bytes": len(raw_source), "input_bytes": len(raw_input)},
            "runtime": deepcopy(self.runtime_identity), "source_unchanged": True,
            "runtime_unchanged": True, "process_status": "passed", "reported_result": result,
            "execution": {"host_status": "exited", "detail": None, "output_limit": False,
                "load_seconds": 0.1, "worker_seconds": 0.2,
                "guest_output": {"stdout": "owned output\nFORGE8_GUEST_RESULT=" + json.dumps(result) + "\n",
                                 "stderr": "owned diagnostic"}},
            "run_root": str(destination), "elapsed_seconds": 0.3, "postcheck_errors": []}
        report.update(changes)
        destination.mkdir()
        (destination / "experiment.json").write_bytes(json.dumps(report, ensure_ascii=True, indent=2).encode("utf-8"))
        self.reports[destination.name] = (destination / "experiment.json", deepcopy(report))
        return report

    def run_pair(self, runner, payload=None):
        payload = self.payload() if payload is None else payload
        with patch.object(experiments, "run_experiment", side_effect=runner) as mocked:
            identifier = self.desk.start_experiment(payload)["id"]
            self.join_experiment()
        return identifier, self.desk.experiment_status(), mocked.call_count

    def join_experiment(self):
        self.assertIsNotNone(self.desk.experiment_worker)
        self.desk.experiment_worker.join(5)
        self.assertFalse(self.desk.experiment_worker.is_alive(), "mock paired trial did not finish")

    def assert_no_execution(self):
        for guard in self.guards:
            guard.assert_not_called()
        self.process_guard.assert_not_called()
        self.assertIsNone(self.desk.experiment_worker)

    def search_payload(self, raw=' {"args":[9],"kwargs":{}}\n'):
        preview = self.desk.prepare_experiment({**self.target(), "search": "nearby-v1", "input_text": raw})
        payload = self.payload()
        payload.update(search="nearby-v1", input_text=raw, search_plan_sha256=preview["search_plan"]["sha256"])
        return payload, preview["search_plan"]

    def test_search_preview_is_exact_static_and_does_not_change_trial_state(self):
        original = deepcopy(self.desk.experiment_status())
        paths = set(self.desk.root.iterdir())
        raw = ' {"args":[9007199254740993,"中文"],"kwargs":{}}\n'
        payload, plan = self.search_payload(raw)
        self.assertEqual(plan, experiments.prepare_input_search(raw.encode("utf-8")))
        self.assertEqual(plan["inputs"][0]["input_text"], raw)
        self.assertEqual(plan["seed_input_text"], raw)
        self.assertEqual(plan["max_initializations"], 2 * len(plan["inputs"]))
        self.assertLessEqual(len(plan["inputs"]), 12)
        self.assertEqual(plan["max_seconds"], 120)
        self.assertNotIn("search_plan", self.desk.prepare_experiment(self.target()))
        self.assertEqual(self.desk.experiment_status(), original)
        self.assertEqual(set(self.desk.root.iterdir()), paths)
        self.assertEqual(payload["search_plan_sha256"], plan["sha256"])
        self.assert_no_execution()

    def test_search_rechecks_plan_and_explicit_consent_before_creating_worker(self):
        payload, plan = self.search_payload()
        invalid = [{**payload, "search_plan_sha256": "0" * 64},
            {**payload, "search_plan_sha256": True}, {**payload, "search": True},
            {**payload, "search": "unknown"}, {**payload, "input_text": '{"args":[8],"kwargs":{}}'},
            {**payload, "inputs": plan["inputs"]}, {**payload, "allow_execution": 1},
            {key: value for key, value in payload.items() if key != "search_plan_sha256"},
            {key: value for key, value in payload.items() if key != "search"}]
        paths = set(self.desk.root.iterdir())
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.desk.start_experiment(request)
        with patch.object(experiments, "prepare_input_search", return_value={**plan, "sha256": "a" * 64}):
            with self.assertRaisesRegex(ValueError, "plan changed"):
                self.desk.start_experiment(payload)
        self.assertEqual(set(self.desk.root.iterdir()), paths)
        self.assert_no_execution()

    def test_search_stops_at_first_difference_and_binds_prior_pairs(self):
        payload, plan = self.search_payload()
        saved = deepcopy((self.desk.job, self.desk.history(), self.desk.project))
        seen = []

        def run(_runtime, source, entry, inputs, destination, **options):
            index = int(destination.parent.name[-2:])
            seen.append((index, destination.name))
            self.assertEqual(inputs.read_bytes(), plan["inputs"][index - 1]["input_text"].encode("utf-8"))
            self.assertEqual(self.desk.experiment_status()["input_text"], payload["input_text"])
            self.assertEqual(self.desk.experiment_status()["search"]["case_index"], index)
            self.assertFalse(options["cancel_requested"]())
            value = index == 2 and destination.name == "after"
            return self.write_report(source, entry, inputs, destination, {"return": value})

        identifier, state, count = self.run_pair(run, payload)
        self.assertEqual(count, 4)
        self.assertEqual(seen, [(1, "before"), (1, "after"), (2, "before"), (2, "after")])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["comparison_result"], "different")
        self.assertEqual(state["search"], {"strategy": "nearby-v1", "plan_sha256": plan["sha256"],
            "total": len(plan["inputs"]), "completed": 2, "case_index": 2,
            "input_text": plan["inputs"][1]["input_text"], "stop_reason": "different"})
        self.assertEqual(state["input_text"], payload["input_text"])
        self.assertEqual(json.loads(state["observations"]["before"]["result_text"]), {"return": False})
        self.assertEqual(json.loads(state["observations"]["after"]["result_text"]), {"return": True})
        directory = self.desk.root / identifier
        receipt = json.loads((directory / "comparison-experiment.json").read_bytes())
        self.assertEqual(receipt["kind"], "forge8.paired_input_search")
        self.assertEqual(receipt["plan"], plan)
        self.assertEqual(receipt["result"], state)
        self.assertEqual(len(receipt["cases"]), 2)
        self.assertEqual(len(receipt["retained_sha256"]), 9)
        for name, digest in receipt["retained_sha256"].items():
            self.assertEqual(sha((directory / name).read_bytes()), digest)
        for row in receipt["cases"]:
            child = directory / f"case-{row['index']:02}" / "comparison-experiment.json"
            self.assertEqual(sha(child.read_bytes()), row["receipt_sha256"])
            pair = json.loads(child.read_bytes())
            self.assertEqual(pair["kind"], "forge8.paired_experiment")
            self.assertEqual(pair["input_sha256"], row["input_sha256"])
            self.assertEqual(pair["child_reports"], row["child_reports"])
            self.assertNotIn("search", pair["result"])
        state["search"]["input_text"] = "caller mutation"
        self.assertEqual(self.desk.experiment_status()["search"]["input_text"], plan["inputs"][1]["input_text"])
        self.assertEqual((self.desk.job, self.desk.history(), self.desk.project), saved)

    def test_search_exhaustion_and_seed_only_plan_count_only_completed_pairs(self):
        for raw in ('{"args":[false],"kwargs":{}}', '{"args":[],"kwargs":{}}'):
            with self.subTest(raw=raw):
                payload, plan = self.search_payload(raw)

                def run(_runtime, source, entry, inputs, destination, **_options):
                    return self.write_report(source, entry, inputs, destination, {"return": 9007199254740993})

                identifier, state, count = self.run_pair(run, payload)
                self.assertEqual(count, 2 * len(plan["inputs"]))
                self.assertEqual(state["search"]["completed"], len(plan["inputs"]))
                self.assertEqual(state["search"]["stop_reason"], "exhausted")
                self.assertEqual(state["comparison_result"], "same")
                receipt = json.loads((self.desk.root / identifier / "comparison-experiment.json").read_bytes())
                self.assertIn("no equivalence", receipt["notice"])
                self.assertEqual(receipt["result"], state)

    def test_search_between_case_cancellation_keeps_one_busy_lease_and_no_next_guest(self):
        payload, _ = self.search_payload()
        original_write = experiments._write_json

        def run(_runtime, source, entry, inputs, destination, **_options):
            return self.write_report(source, entry, inputs, destination, {"return": 7})

        def write(path, record):
            original_write(path, record)
            if path.parent.name == "case-01":
                for action in (lambda: self.desk.start_experiment(payload), self.desk.refresh):
                    with self.assertRaises(ValueError):
                        action()
                self.assertEqual(self.desk.cancel_experiment({"id": self.desk.experiment["id"]})["status"], "cancelling")

        with patch.object(experiments, "_write_json", side_effect=write):
            _, state, count = self.run_pair(run, payload)
        self.assertEqual(count, 2)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["search"]["completed"], 1)
        self.assertEqual(state["search"]["stop_reason"], "cancelled")
        self.assertEqual(state["comparison_result"], "unavailable")
        self.assertFalse(self.desk._busy())

    def test_search_budget_during_first_guest_is_not_user_cancellation(self):
        payload, _ = self.search_payload()
        clock = [1.0]

        def run(_runtime, source, entry, inputs, destination, **options):
            report = self.write_report(source, entry, inputs, destination, {"return": 7})
            clock[0] = 122.0
            self.assertTrue(options["cancel_requested"]())
            return report

        with patch.object(desk_module.time, "monotonic", side_effect=lambda: clock[0]):
            _, state, count = self.run_pair(run, payload)
        self.assertEqual(count, 1)
        self.assertEqual(state["status"], "incomplete")
        self.assertEqual(state["search"]["stop_reason"], "budget")
        self.assertEqual(state["search"]["completed"], 0)
        self.assertEqual(state["comparison_result"], "unavailable")
        self.assertFalse(self.desk.experiment_cancel_event.is_set())

    def test_search_cancel_or_budget_before_final_publication_suppresses_found_difference(self):
        for mode in ("cancel", "budget"):
            with self.subTest(mode=mode):
                payload, _ = self.search_payload()
                clock = [1.0]
                original_file = experiments._file

                def run(_runtime, source, entry, inputs, destination, **_options):
                    return self.write_report(source, entry, inputs, destination, {"return": destination.name})

                def read(path, *args, **kwargs):
                    raw = original_file(path, *args, **kwargs)
                    if path == self.desk.root / self.desk.experiment["id"] / "arguments.json":
                        if mode == "budget":
                            clock[0] = 122.0
                        else:
                            self.desk.cancel_experiment({"id": self.desk.experiment["id"]})
                    return raw

                with patch.object(experiments, "_file", side_effect=read), \
                        patch.object(desk_module.time, "monotonic", side_effect=lambda: clock[0]):
                    _, state, count = self.run_pair(run, payload)
                self.assertEqual(count, 2)
                self.assertEqual(state["search"]["completed"], 1)
                self.assertEqual(state["search"]["stop_reason"], "cancelled" if mode == "cancel" else "budget")
                self.assertEqual(state["status"], "cancelled" if mode == "cancel" else "incomplete")
                self.assertEqual(state["comparison_result"], "unavailable")
                self.assertFalse(state["comparison_unchanged"])

    def test_search_deadline_status_and_reason_use_one_publication_sample(self):
        payload, _ = self.search_payload()
        original_file = experiments._file
        receipt_reads, final_samples = [], []

        def clock():
            if len(receipt_reads) < 2:
                return 1.0
            # The first finalization sample is just before the 121.0 deadline;
            # elapsed-time formatting crosses it. The verdict must stay coherent.
            final_samples.append(True)
            return 120.5 if len(final_samples) == 1 else 121.5

        def read(path, *args, **kwargs):
            value = original_file(path, *args, **kwargs)
            if path.parent.name == "case-01" and path.name == "comparison-experiment.json":
                receipt_reads.append(path)
            return value

        def run(_runtime, source, entry, inputs, destination, **_options):
            return self.write_report(source, entry, inputs, destination, {"return": destination.name})

        with patch.object(experiments, "_file", side_effect=read), \
                patch.object(desk_module.time, "monotonic", side_effect=clock):
            identifier, state, count = self.run_pair(run, payload)
        self.assertEqual(count, 2)
        self.assertGreaterEqual(len(final_samples), 2)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["search"]["stop_reason"], "different")
        receipt = json.loads((self.desk.root / identifier / "comparison-experiment.json").read_bytes())
        self.assertEqual(receipt["result"], state)

    def test_search_rechecks_earlier_input_report_and_receipt_after_later_pair(self):
        for relative in ("arguments.json", "before/experiment.json", "comparison-experiment.json"):
            with self.subTest(relative=relative):
                payload, _ = self.search_payload()

                def run(_runtime, source, entry, inputs, destination, **_options):
                    value = destination.parent.name == "case-02" and destination.name == "after"
                    report = self.write_report(source, entry, inputs, destination, {"return": value})
                    if value:
                        path = destination.parent.parent / "case-01" / relative
                        path.write_bytes(path.read_bytes() + b" ")
                    return report

                _, state, count = self.run_pair(run, payload)
                self.assertEqual(count, 4)
                self.assertEqual(state["status"], "incomplete")
                self.assertEqual(state["search"]["stop_reason"], "incomplete")
                self.assertEqual(state["comparison_result"], "unavailable")

    def test_search_source_or_runtime_drift_between_cases_stops_before_next_guest(self):
        original_write = experiments._write_json
        for defect in ("source", "runtime"):
            with self.subTest(defect=defect):
                self.live.write_bytes(self.code)
                (self.runtime / "runtime.json").write_bytes(b"{}")
                self.desk.refresh(mode="changes")
                payload, _ = self.search_payload()

                def run(_runtime, source, entry, inputs, destination, **_options):
                    return self.write_report(source, entry, inputs, destination, {"return": 7})

                def write(path, record):
                    original_write(path, record)
                    if path.parent.name == "case-01":
                        changed = self.live if defect == "source" else self.runtime / "runtime.json"
                        changed.write_bytes(changed.read_bytes() + b" ")

                with patch.object(experiments, "_write_json", side_effect=write):
                    _, state, count = self.run_pair(run, payload)
                self.assertEqual(count, 2)
                self.assertEqual(state["status"], "incomplete")
                self.assertEqual(state["search"]["stop_reason"], "incomplete")
                self.assertEqual(state["comparison_result"], "unavailable")

    def test_search_cross_case_reported_runtime_change_cannot_become_a_difference(self):
        payload, _ = self.search_payload()

        def run(_runtime, source, entry, inputs, destination, **_options):
            later = destination.parent.name == "case-02"
            identity = {**self.runtime_identity, "python": "other"} if later else self.runtime_identity
            return self.write_report(source, entry, inputs, destination,
                {"return": later and destination.name == "after"}, runtime=identity)

        _, state, count = self.run_pair(run, payload)
        self.assertEqual(count, 4)
        self.assertEqual(state["search"]["stop_reason"], "incomplete")
        self.assertEqual(state["comparison_result"], "unavailable")

    def test_search_unavailable_report_stops_instead_of_counting_a_same_pair(self):
        payload, _ = self.search_payload()

        def run(_runtime, source, entry, inputs, destination, **_options):
            return self.write_report(source, entry, inputs, destination, None)

        _, state, count = self.run_pair(run, payload)
        self.assertEqual(count, 1)
        self.assertEqual(state["status"], "incomplete")
        self.assertEqual(state["search"]["stop_reason"], "incomplete")
        self.assertEqual(state["search"]["completed"], 0)

    def test_search_cleanup_unknown_latches_and_does_not_advance(self):
        payload, _ = self.search_payload()

        def run(*_args, **_options):
            self.desk.experiment_cancel_event.set()
            raise CheckCleanupError(123)

        _, state, count = self.run_pair(run, payload)
        self.assertEqual(count, 1)
        self.assertTrue(state["cleanup_unknown"])
        self.assertTrue(self.desk._busy())
        self.assertEqual(state["search"]["stop_reason"], "incomplete")
        self.assertEqual(state["comparison_result"], "unavailable")
        with self.assertRaises(ValueError):
            self.desk.start_experiment(payload)

    def test_search_pair_receipt_failure_never_starts_another_case(self):
        payload, _ = self.search_payload()
        original_write = experiments._write_json

        def run(_runtime, source, entry, inputs, destination, **_options):
            return self.write_report(source, entry, inputs, destination, {"return": 7})

        def write(path, record):
            if path.parent.name == "case-01":
                raise OSError("owned search receipt failure")
            original_write(path, record)

        with patch.object(experiments, "_write_json", side_effect=write):
            _, state, count = self.run_pair(run, payload)
        self.assertEqual(count, 2)
        self.assertEqual(state["search"]["completed"], 0)
        self.assertEqual(state["search"]["stop_reason"], "incomplete")
        self.assertEqual(state["comparison_result"], "unavailable")

    def test_prepare_binds_both_complete_raw_modules_without_execution(self):
        request = self.target()
        saved = deepcopy((self.desk.project, self.desk.contents, self.desk.job))
        with patch.object(experiments, "prepare_module", wraps=experiments.prepare_module) as prepare:
            target = self.desk.prepare_experiment(request)
        prepare.assert_any_call(self.code, "entry")
        prepare.assert_any_call(self.before, "entry")
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(target, {**request, "path": "after/main.py", "source_sha256": sha(self.code),
            "source_bytes": len(self.code), "head": self.head,
            "current_snapshot_sha256": self.desk.comparison.original_snapshot_sha256,
            "before": {"file": self.target("before/main.py")["file"], "path": "before/main.py",
                "source_sha256": sha(self.before), "source_bytes": len(self.before)}})
        target["before"]["source_sha256"] = "0" * 64
        self.assertEqual(self.desk.prepare_experiment(request)["before"]["source_sha256"], sha(self.before))
        self.assertEqual((self.desk.project, self.desk.contents, self.desk.job), saved)
        self.assertEqual(self.desk.experiment_status(), {"id": None, "status": "idle"})
        self.assert_no_execution()

    def test_paired_preparation_requires_after_side_matching_entry_and_explicit_mode(self):
        valid = self.target()
        for request in (self.target("before/main.py"), {**valid, "mode": True},
                        {**valid, "mode": "changes"}, {**valid, "modules": [valid["file"]]},
                        {**valid, "version": "stale"}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.desk.prepare_experiment(request)
        for files in ({}, {"main.py": b"def different():\n    return 1\n"},
                      {"main.py": b"async def entry(value):\n    return value\n"}):
            with self.subTest(before_files=files):
                self.git_read.return_value = GitBaseline(self.head, files, ())
                self.desk.refresh(mode="changes")
                with self.assertRaises(ValueError):
                    self.desk.prepare_experiment(self.target())
        self.desk.refresh(mode="source")
        with self.assertRaises(ValueError):
            self.desk.prepare_experiment(self.target("main.py"))
        self.assert_no_execution()

    def test_consent_both_hashes_head_and_raw_input_are_required_before_worker(self):
        valid = self.payload()
        invalid = [{**valid, "allow_execution": item} for item in (False, None, 1, "true")]
        invalid += [{key: value for key, value in valid.items() if key != field}
                    for field in ("mode", "head", "before_sha256", "allow_execution")]
        invalid += [{**valid, "head": "b" * 40}, {**valid, "before_sha256": "0" * 64},
            {**valid, "source_sha256": "0" * 64}, {**valid, "version": "stale"},
            {**valid, "modules": [valid["file"]], "module_set_sha256": "0" * 64},
            {**valid, "input_text": {"args": [], "kwargs": {}}},
            {**valid, "input_text": '{"args":[],"args":[],"kwargs":{}}'},
            {**valid, "input_text": '{"args":[1e999],"kwargs":{}}'}]
        existing = set(self.desk.root.iterdir())
        for index, request in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.desk.start_experiment(request)
        self.assertEqual(set(self.desk.root.iterdir()), existing)
        self.assert_no_execution()

    def test_two_sequential_sources_share_exact_input_and_seal_independent_observations(self):
        payload = self.payload()
        previous = deepcopy((self.desk.job, self.desk.history(), self.desk.project, self.desk.contents))
        seen = []

        def run(runtime, source, entry, inputs, destination, **options):
            side = ("before", "after")[len(seen)]
            seen.append((side, inputs, self.desk.experiment_status()["phase"]))
            self.assertEqual(runtime, self.runtime)
            self.assertEqual(source, Path(self.desk.browse_snapshot.snapshot_root) / side / "main.py")
            self.assertNotEqual(source, self.live)
            self.assertEqual(source.read_bytes(), self.before if side == "before" else self.code)
            self.assertEqual(inputs.read_bytes(), payload["input_text"].encode("utf-8"))
            self.assertEqual(destination, inputs.parent / side)
            self.assertEqual(entry, "entry")
            self.assertEqual(options["expected_source_sha256"], sha(source.read_bytes()))
            self.assertEqual(options["expected_input_sha256"], sha(inputs.read_bytes()))
            self.assertIs(options["allow_execution"], True)
            self.assertFalse(options["cancel_requested"]())
            self.assertFalse(set(options) & {"module_root", "module_files", "expected_module_set_sha256"})
            value = {"label": "中文\u202e", "id": 9007199254740993}
            if side == "after":
                value = dict(reversed(list(value.items())))
            return self.write_report(source, entry, inputs, destination, {"return": value})

        identifier, state, count = self.run_pair(run, payload)
        self.assertEqual(count, 2)
        self.assertEqual([side for side, _, _ in seen], ["before", "after"])
        self.assertEqual(seen[0][1], seen[1][1])
        self.assertNotEqual(seen[0][2], seen[1][2])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["comparison_result"], "same")
        self.assertEqual(state["comparison_rule"], "canonical-json-v1")
        self.assertIs(state["comparison_unchanged"], True)
        self.assertEqual(state["input_text"], payload["input_text"])
        self.assertEqual(state["head"], self.head)
        self.assertEqual(state["source_sha256"], sha(self.code))
        self.assertEqual(state["before"]["source_sha256"], sha(self.before))
        self.assertEqual(set(state["observations"]), {"before", "after"})
        for side, observation in state["observations"].items():
            self.assertIn("9007199254740993", observation["result_text"])
            self.assertIn("中文", observation["result_text"])
            self.assertIn(r"\u202e", observation["result_text"])
            self.assertNotIn("\u202e", observation["result_text"])
            self.assertNotIn("reported_result", observation)
            self.assertEqual(observation["stdout"], "owned output")
            self.assertEqual(observation["stderr"], "owned diagnostic")
            self.assertEqual(observation["report_sha256"], sha(self.reports[side][0].read_bytes()))
        receipt = json.loads((self.desk.root / identifier / "comparison-experiment.json").read_bytes())
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["kind"], "forge8.paired_experiment")
        self.assertEqual(receipt["result"]["comparison_result"], "same")
        self.assertEqual(receipt["result"]["observations"], state["observations"])
        self.assertEqual(receipt["input_sha256"], sha(payload["input_text"].encode("utf-8")))
        self.assertEqual(receipt["runtime_manifest_sha256"], sha(b"{}"))
        self.assertEqual(receipt["child_reports"], {
            side: sha(path.read_bytes()) for side, (path, _) in self.reports.items()})
        state["observations"]["before"]["result_text"] = "caller mutation"
        state["before"]["source_sha256"] = "caller mutation"
        fresh = self.desk.experiment_status()
        self.assertIn("9007199254740993", fresh["observations"]["before"]["result_text"])
        self.assertEqual(fresh["before"]["source_sha256"], sha(self.before))
        self.assertEqual((self.desk.job, self.desk.history(), self.desk.project, self.desk.contents), previous)

    def test_completed_guest_exception_allows_second_observation_and_typed_difference(self):
        def run(_runtime, source, entry, inputs, destination, **_options):
            value = ({"exception": "ValueError", "message": "owned exception", "phase": "call"}
                     if destination.name == "before" else {"return": None})
            return self.write_report(source, entry, inputs, destination, value)

        _, state, count = self.run_pair(run)
        self.assertEqual(count, 2)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["comparison_result"], "different")
        self.assertEqual(json.loads(state["observations"]["before"]["result_text"])["phase"], "call")

    def test_first_infrastructure_failure_prevents_second_guest(self):
        for kind in ("process", "host", "boolean_exit", "output", "source", "runtime", "bad_output"):
            with self.subTest(kind=kind):
                def run(_runtime, source, entry, inputs, destination, **_options):
                    report = self.write_report(source, entry, inputs, destination, {"return": 7})
                    if kind == "process": report["process_status"] = "timed_out"
                    if kind == "host": report["execution"].update(host_status="trap", detail="owned trap")
                    if kind == "boolean_exit": report["execution"].update(host_status="guest_exit", detail=False)
                    if kind == "output": report["execution"]["output_limit"] = True
                    if kind == "source": report["source_unchanged"] = False
                    if kind == "runtime": report["runtime_unchanged"] = False
                    if kind == "bad_output": report["execution"]["guest_output"]["stdout"] = 5
                    (destination / "experiment.json").write_bytes(json.dumps(report).encode("utf-8"))
                    return report

                _, state, count = self.run_pair(run)
                self.assertEqual(count, 1)
                self.assertEqual(state["status"], "incomplete")
                self.assertEqual(state["comparison_result"], "unavailable")
                self.assertNotIn("after", state["observations"])
                self.assertFalse(self.desk._busy())

    def test_input_or_runtime_manifest_drift_prevents_second_launch(self):
        for defect in ("input", "runtime"):
            with self.subTest(defect=defect):
                (self.runtime / "runtime.json").write_bytes(b"{}")

                def run(_runtime, source, entry, inputs, destination, **_options):
                    report = self.write_report(source, entry, inputs, destination, {"return": 7})
                    changed = inputs if defect == "input" else self.runtime / "runtime.json"
                    changed.write_bytes(b'{"args":[8],"kwargs":{}}' if defect == "input" else b'{"changed":true}')
                    return report

                _, state, count = self.run_pair(run)
                self.assertEqual(count, 1)
                self.assertEqual(state["status"], "incomplete")
                self.assertEqual(state["comparison_result"], "unavailable")
                self.assertNotIn("after", state["observations"])

    def test_unprojectable_result_preserves_raw_diagnostic_without_comparison(self):
        nested = 0
        for _ in range(65):
            nested = [nested]

        def run(_runtime, source, entry, inputs, destination, **_options):
            return self.write_report(source, entry, inputs, destination, {"return": nested})

        _, state, count = self.run_pair(run)
        self.assertEqual(count, 2)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["comparison_result"], "unavailable")
        for observation in state["observations"].values():
            self.assertNotIn("result_text", observation)
            self.assertIn("FORGE8_GUEST_RESULT=", observation["stdout"])

    def test_worker_start_failure_is_terminal_without_a_cleanup_claim(self):
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("owned thread failure")):
            identifier = self.desk.start_experiment(self.payload())["id"]
        state = self.desk.experiment_status()
        self.assertEqual(state["id"], identifier)
        self.assertEqual(state["status"], "incomplete")
        self.assertEqual(state["comparison_result"], "unavailable")
        self.assertNotIn("cleanup_unknown", state)
        self.assertIsNone(self.desk.experiment_worker)
        self.assertFalse(self.desk._busy())
        self.assert_no_execution()

    def test_cancellation_after_first_guest_prevents_second_and_preserves_diagnostics(self):
        entered, release = threading.Event(), threading.Event()
        payload = self.payload()

        def run(_runtime, source, entry, inputs, destination, **options):
            report = self.write_report(source, entry, inputs, destination, {"return": 7})
            entered.set()
            self.assertTrue(release.wait(3), "test did not release first mock guest")
            self.assertTrue(options["cancel_requested"]())
            return report

        with patch.object(experiments, "run_experiment", side_effect=run) as runner:
            identifier = self.desk.start_experiment(payload)["id"]
            try:
                self.assertTrue(entered.wait(3))
                for action in (lambda: self.desk.start_experiment(payload), self.desk.refresh,
                               lambda: self.desk.start(self.question())):
                    with self.assertRaises(ValueError):
                        action()
                self.assertEqual(self.desk.cancel_experiment({"id": identifier})["status"], "cancelling")
            finally:
                release.set()
                self.join_experiment()
        runner.assert_called_once()
        state = self.desk.experiment_status()
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["comparison_result"], "unavailable")
        self.assertEqual(set(state["observations"]), {"before"})
        self.assertEqual(json.loads(state["observations"]["before"]["result_text"]), {"return": 7})
        self.assertEqual(state["input_text"], payload["input_text"])
        self.assertEqual(state["head"], self.head)
        self.assertFalse(self.desk._busy())

    def test_cleanup_unknown_overrides_cancellation_and_blocks_new_work(self):
        payload = self.payload()

        def run(*_args, **_options):
            self.desk.experiment_cancel_event.set()
            raise CheckCleanupError(123)

        _, state, count = self.run_pair(run, payload)
        self.assertEqual(count, 1)
        self.assertEqual(state["status"], "incomplete")
        self.assertIs(state["cleanup_unknown"], True)
        self.assertEqual(state["comparison_result"], "unavailable")
        self.assertTrue(self.desk._busy())
        for action in (lambda: self.desk.start_experiment(payload), self.desk.refresh,
                       lambda: self.desk.start(self.question())):
            with self.assertRaises(ValueError):
                action()

    def test_all_three_comparison_guards_run_unlocked_and_failure_stops_publication(self):
        for failure_at in (None, 1, 2, 3):
            with self.subTest(failure_at=failure_at):
                payload = self.payload()
                captured, original = self.desk.comparison, self.desk.comparison.guard
                events = []

                def guard(owner):
                    self.assertIs(owner, captured)
                    acquired = []

                    def inspect_lock():
                        # start_experiment may still be returning under its own
                        # short lock; only a guard-held lock must block this.
                        held = self.desk.lock.acquire(timeout=0.5)
                        acquired.append(held)
                        if held:
                            self.desk.lock.release()

                    observer = threading.Thread(target=inspect_lock)
                    observer.start()
                    observer.join(1)
                    self.assertEqual(acquired, [True], "comparison guard ran under desk lock")
                    events.append("guard")
                    if events.count("guard") == failure_at:
                        return AcceptanceGateResult(False, "owned comparison drift", {})
                    return original()

                def run(_runtime, source, entry, inputs, destination, **_options):
                    events.append(destination.name)
                    return self.write_report(source, entry, inputs, destination, {"return": 7})

                with patch.object(comparison.Comparison, "guard", new=guard):
                    _, state, count = self.run_pair(run, payload)
                expected = ["guard", "before", "guard", "after", "guard"]
                if failure_at is not None:
                    expected = expected[:(failure_at - 1) * 2 + 1]
                self.assertEqual(events, expected)
                self.assertEqual(count, 2 if failure_at in (None, 3) else failure_at - 1)
                self.assertEqual(state["comparison_result"], "same" if failure_at is None else "unavailable")
                self.assertEqual(state["comparison_unchanged"], failure_at is None)
                if failure_at is not None:
                    self.assertEqual(state["status"], "incomplete")

    def test_report_identity_bytes_and_cross_side_runtime_must_stay_bound(self):
        for defect in ("source_sha256", "input_sha256", "entry", "kind", "schema_version",
                       "written_bytes", "runtime_pair", "prior_report_drift"):
            with self.subTest(defect=defect):
                def run(_runtime, source, entry, inputs, destination, **_options):
                    report = self.write_report(source, entry, inputs, destination, {"return": 7})
                    if destination.name == "before":
                        if defect in {"source_sha256", "input_sha256", "entry"}:
                            report["identity"][defect] = "wrong" if defect == "entry" else "0" * 64
                        elif defect == "kind": report["kind"] = "not.an.experiment"
                        elif defect == "schema_version": report["schema_version"] = True
                        elif defect == "written_bytes": report["reported_result"] = {"return": "not the written report"}
                    elif defect == "runtime_pair":
                        report["runtime"]["cache"]["sha256"] = "d" * 64
                    elif defect == "prior_report_drift":
                        path, previous = self.reports["before"]
                        path.write_bytes(json.dumps({**previous, "reported_result": {"return": 99}}).encode("utf-8"))
                    if defect != "written_bytes":
                        (destination / "experiment.json").write_bytes(json.dumps(report).encode("utf-8"))
                    return report

                _, state, count = self.run_pair(run)
                self.assertEqual(count, 2 if defect in {"runtime_pair", "prior_report_drift"} else 1)
                self.assertEqual(state["status"], "incomplete")
                self.assertEqual(state["comparison_result"], "unavailable")

    def test_live_head_or_source_drift_between_sides_prevents_second_guest(self):
        for defect in ("head", "source"):
            with self.subTest(defect=defect):
                self.live.write_bytes(self.code)
                self.git_head.return_value = self.head
                self.desk.refresh(mode="changes")

                def run(_runtime, source, entry, inputs, destination, **_options):
                    report = self.write_report(source, entry, inputs, destination, {"return": 7})
                    if defect == "head":
                        self.git_head.return_value = "b" * 40
                    else:
                        self.live.write_bytes(self.code + b"# Changed live source; never execute.\n")
                    return report

                _, state, count = self.run_pair(run)
                self.assertEqual(count, 1)
                self.assertEqual(state["status"], "incomplete")
                self.assertEqual(state["comparison_result"], "unavailable")
                self.assertIs(state["comparison_unchanged"], False)

    def test_duplicate_keys_in_actual_child_report_cannot_be_hidden_by_json_comparison(self):
        def run(_runtime, source, entry, inputs, destination, **_options):
            report = self.write_report(source, entry, inputs, destination, {"return": 7})
            path = destination / "experiment.json"
            raw = path.read_bytes()
            changed = raw.replace(b'"schema_version": 1',
                b'"schema_version": 1, "schema_version": 1', 1)
            self.assertNotEqual(changed, raw)
            path.write_bytes(changed)
            return report

        _, state, count = self.run_pair(run)
        self.assertEqual(count, 1)
        self.assertEqual(state["status"], "incomplete")
        self.assertEqual(state["comparison_result"], "unavailable")
        self.assertNotIn("after", state["observations"])

    def test_terminal_result_is_not_published_until_receipt_write_succeeds(self):
        original_write = experiments._write_json
        for after_value, write_fails in ((7, False), (8, False), (7, True)):
            with self.subTest(after_value=after_value, write_fails=write_fails):
                during_write = []

                def run(_runtime, source, entry, inputs, destination, **_options):
                    value = 7 if destination.name == "before" else after_value
                    return self.write_report(source, entry, inputs, destination, {"return": value})

                def write(path, receipt):
                    self.assertEqual(path.name, "comparison-experiment.json")
                    during_write.append(deepcopy(self.desk.experiment))
                    self.assertNotEqual(self.desk.experiment["status"], "completed")
                    self.assertEqual(self.desk.experiment["comparison_result"], "unavailable")
                    self.assertEqual(receipt["result"]["status"], "completed")
                    if write_fails:
                        raise OSError("owned receipt write failure")
                    original_write(path, receipt)

                with patch.object(experiments, "_write_json", side_effect=write) as writer:
                    _, state, count = self.run_pair(run)
                self.assertEqual(count, 2)
                writer.assert_called_once()
                self.assertEqual(len(during_write), 1)
                self.assertEqual(state["status"], "incomplete" if write_fails else "completed")
                expected = "unavailable" if write_fails else "same" if after_value == 7 else "different"
                self.assertEqual(state["comparison_result"], expected)
                self.assertEqual(state["comparison_unchanged"], not write_fails)
                if write_fails:
                    self.assertIn("receipt", state["error"].lower())

    def test_late_cancel_after_terminal_publication_does_not_rewrite_completed_result(self):
        def run(_runtime, source, entry, inputs, destination, **_options):
            return self.write_report(source, entry, inputs, destination, {"return": 7})

        identifier, state, count = self.run_pair(run)
        self.assertEqual(count, 2)
        self.assertEqual(state["status"], "completed")
        receipt = self.desk.root / identifier / "comparison-experiment.json"
        retained = receipt.read_bytes()
        self.assertFalse(self.desk.experiment_cancel_event.is_set())
        # Simulate the narrow post-publication, pre-thread-exit interval.
        with patch.object(self.desk.experiment_worker, "is_alive", return_value=True):
            self.assertEqual(self.desk.cancel_experiment({"id": identifier}), {"status": "completed"})
        self.assertFalse(self.desk.experiment_cancel_event.is_set())
        self.assertEqual(self.desk.experiment_status(), state)
        self.assertEqual(receipt.read_bytes(), retained)
