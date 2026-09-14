"""ReadingDesk experiment admission/lifecycle; only mocked native execution."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
from unittest.mock import patch

from forge8 import comparison, desk as desk_module, experiments
from forge8.checks import CheckCleanupError
from forge8.git_source import GitBaseline
import test_desk as fixtures


class DeskExperimentTests(fixtures.DeskFixture):
    def setUp(self):
        super().setUp()
        self.desk.close()
        self.code = (
            '# Owned static fixture; never execute.\r\nTAG = "x\u200by"\r\n'
            "raise RuntimeError('must not execute this fixture')\r\n"
            "def entry(value):\r\n    return {'value': value, 'tag': TAG}\r\n"
        ).encode("utf-8")
        self.live = self.source / "main.py"
        self.live.write_bytes(self.code)
        for mock in (
            patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.root / "state")}),
            patch.object(experiments, "native_platform", return_value="linux"),
            patch("subprocess.Popen", side_effect=AssertionError("no subprocess allowed")),
            patch.object(experiments, "_worker", side_effect=AssertionError("no native worker allowed")),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        self.guards = []
        for target, name in ((experiments, "run_experiment"),
                             (desk_module, "_run_explain_cli"),
                             (desk_module, "_run_locate_cli"),
                             (desk_module, "_run_project_cli")):
            guard = patch.object(target, name, side_effect=AssertionError("unmocked operation"))
            self.guards.append(guard.start())
            self.addCleanup(guard.stop)
        self.runtime = self.assets / f"experiment-{experiments.RUNTIME_NAME}-linux"
        self.runtime.mkdir()
        (self.runtime / "runtime.json").write_bytes(b"{}")  # Availability only; never loaded.
        self.desk = desk_module.ReadingDesk(self.source, allow_experiments=True)
        self.addCleanup(self.desk.close)

    def target(self, *, desk=None, path="main.py", entry="entry"):
        desk = self.desk if desk is None else desk
        item = next(item for item in desk.project["files"] if item["path"] == path)
        return {"file": item["id"], "version": desk.project["version"], "entry": entry}

    def payload(self):
        target = self.desk.prepare_experiment(self.target())
        return {key: target[key] for key in ("file", "version", "entry", "source_sha256")} | {
            "input_text": ' {"args":[9007199254740993],"kwargs":{}}\n',
            "allow_execution": True,
        }

    def report(self, value=9007199254740993):
        result = {"return": value}
        return {"source_unchanged": True, "runtime_unchanged": True,
            "process_status": "passed", "reported_result": result,
            "execution": {"host_status": "exited", "detail": None, "output_limit": False,
                "guest_output": {"stdout": "owned output\nFORGE8_GUEST_RESULT=" + json.dumps(result) + "\n",
                                 "stderr": ""}}}

    def join_experiment(self):
        self.assertIsNotNone(self.desk.experiment_worker)
        self.desk.experiment_worker.join(5)
        self.assertFalse(self.desk.experiment_worker.is_alive(), "mock experiment did not finish")

    def reading_state(self):
        return deepcopy((self.desk.job, self.desk.history(), self.desk.project, self.desk.contents))

    def assert_no_operations(self):
        for guard in self.guards:
            guard.assert_not_called()
        self.assertIsNone(self.desk.experiment_worker)

    def test_watched_locals_bind_input_preserve_a_and_detach_public_snapshots(self):
        payload = {**self.payload(), "trace_lines": True, "watch_names": ["value"]}
        before = self.reading_state()
        baseline = {"id": "retained-a", "version": self.desk.project["version"]}
        self.desk._trial_baseline = json.dumps({"view": baseline}).encode()
        retained = self.desk._trial_baseline
        watch = {"names": ["value"], "truncated": False, "events": [
            {"event": "line", "line": 5, "call_id": 1, "values": {
                "value": {"state": "value", "json": "9007199254740993"}}}]}
        report = self.report()
        report["reported_trace"] = {"line_events": [5], "truncated": False, "hook_intact": True, "watch": watch}
        with patch.object(experiments, "run_experiment", return_value=report) as runner, \
                patch.object(experiments, "retain_trial_result", side_effect=AssertionError("watched report cannot become A")):
            self.desk.start_experiment(payload)
            self.join_experiment()
        job = self.desk.experiment_status()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(runner.call_args.kwargs["watch_names"], ("value",))
        self.assertIs(runner.call_args.kwargs["trace_lines"], True)
        self.assertEqual(runner.call_args.kwargs["expected_input_sha256"],
            hashlib.sha256(payload["input_text"].encode()).hexdigest())
        self.assertEqual(job["watch_names"], ["value"])
        self.assertEqual(job["reported_trace"]["watch"], watch)
        self.assertEqual(job["input_text"], payload["input_text"])
        self.assertEqual(self.reading_state(), before)
        self.assertEqual(self.desk._trial_baseline, retained)
        self.assertEqual(job["input_comparison"]["baseline"], baseline)
        self.assertIs(job["input_comparison"]["can_pin"], False)
        self.assertIsNone(job["input_comparison"]["current_report_sha256"])
        self.assertEqual(job["input_comparison"]["outcome"], "unavailable")
        revision = job["input_comparison"]["revision"]
        with self.assertRaises(ValueError):
            self.desk.set_trial_baseline({"id": job["id"], "version": job["version"], "revision": revision})
        job["watch_names"].append("changed")
        job["reported_trace"]["watch"]["events"][0]["values"]["value"]["json"] = "0"
        unchanged = self.desk.experiment_status()
        self.assertEqual(unchanged["watch_names"], ["value"])
        self.assertEqual(unchanged["reported_trace"]["watch"]["events"][0]["values"]["value"]["json"], "9007199254740993")
        self.desk.set_trial_baseline({"id": baseline["id"], "version": baseline["version"], "revision": revision}, clear=True)
        self.assertIsNone(self.desk._trial_baseline)

    def test_invalid_watch_options_allocate_no_run_or_worker(self):
        payload = {**self.payload(), "trace_lines": True}
        before = set(self.desk.root.iterdir())
        invalid = [{**payload, "watch_names": names} for names in (
            None, False, "value", [], ["value", "value"], ["a", "b", "c", "d"],
            [1], ["not-valid"], ["a" * 65], ["名稱"], ["class"], ("value",))]
        invalid += [{**payload, "watch_names": ["value"], **options} for options in (
            {"trace_lines": False}, {"generator_steps": 2},
            {"modules": [payload["file"]], "module_set_sha256": "a" * 64}, {"mode": "head_current"})]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.desk.start_experiment(value)
        self.assertEqual(set(self.desk.root.iterdir()), before)
        self.assert_no_operations()

    def test_watched_trace_cannot_survive_incomplete_or_cancelled_run(self):
        payload = {**self.payload(), "trace_lines": True, "watch_names": ["value"]}
        for cancelled in (False, True):
            report = self.report()
            report["reported_trace"] = {"line_events": [5], "truncated": False, "hook_intact": True,
                "watch": {"names": ["value"], "events": [], "truncated": False}}
            def run(*args, **kwargs):
                if cancelled:
                    self.desk.experiment_cancel_event.set()
                else:
                    report["source_unchanged"] = False
                return report
            with self.subTest(cancelled=cancelled), patch.object(experiments, "run_experiment", side_effect=run):
                self.desk.start_experiment(payload)
                self.join_experiment()
                job = self.desk.experiment_status()
                self.assertEqual(job["watch_names"], ["value"])
                self.assertIsNone(job.get("reported_trace"))
                self.assertIsNone(job["input_comparison"]["current_report_sha256"])
                self.assertIs(job["input_comparison"]["can_pin"], False)


    def test_default_off_browsing_and_context_never_enable_execution(self):
        desk = desk_module.ReadingDesk(self.source)
        self.addCleanup(desk.close)
        target = self.target(desk=desk)
        self.assertFalse(desk.project["experiments_enabled"])
        self.assertTrue(desk.source_view(target["file"], target["version"])["lines"])
        self.assertTrue(desk.search("TAG")["matches"])
        desk.context({"file": target["file"], "version": target["version"], "start": 4, "end": 5})
        with self.assertRaisesRegex(ValueError, "disabled"):
            desk.prepare_experiment(target)
        payload = {**target, "source_sha256": hashlib.sha256(self.code).hexdigest(),
                   "input_text": '{"args":[],"kwargs":{}}', "allow_execution": True}
        with self.assertRaisesRegex(ValueError, "disabled"):
            desk.start_experiment(payload)
        self.assertEqual(desk.experiment_status(), {"id": None, "status": "idle"})
        self.assert_no_operations()

    def test_generator_steps_bind_original_input_and_preserve_reading_and_baseline(self):
        payload = {**self.payload(), "generator_steps": 3}
        previous = self.reading_state()
        baseline = {"id": "retained-a", "version": self.desk.project["version"]}
        self.desk._trial_baseline = json.dumps({"view": baseline}).encode()
        retained = self.desk._trial_baseline
        result = {"generator": {"limit": 3, "next_calls": 2,
            "yields": [9007199254740993], "iteration": {"status": "exhausted"},
            "serialization": {"status": "ok"}, "close": {"status": "closed"}, "return_value": None}}
        report = self.report()
        report["reported_result"] = result
        with patch.object(experiments, "run_experiment", return_value=report) as runner, \
                patch.object(experiments, "retain_trial_result", side_effect=AssertionError("generator cannot become A")):
            self.desk.start_experiment(payload)
            self.join_experiment()
        job = self.desk.experiment_status()
        self.assertEqual((job["status"], job["generator_steps"]), ("completed", 3))
        self.assertEqual(json.loads(job["result_text"]), result)
        self.assertIn("9007199254740993", job["result_text"])
        self.assertEqual(job["input_text"], payload["input_text"])
        self.assertEqual(runner.call_args.kwargs["generator_steps"], 3)
        self.assertEqual(runner.call_args.kwargs["expected_input_sha256"],
            hashlib.sha256(payload["input_text"].encode()).hexdigest())
        self.assertNotIn("trace_lines", runner.call_args.kwargs)
        self.assertEqual(self.reading_state(), previous)
        self.assertEqual(self.desk._trial_baseline, retained)
        self.assertEqual(job["input_comparison"]["baseline"], baseline)
        self.assertIs(job["input_comparison"]["can_pin"], False)
        self.assertEqual(job["input_comparison"]["outcome"], "unavailable")
        self.assertIsNone(job["input_comparison"]["current_report_sha256"])
        revision = job["input_comparison"]["revision"]
        with self.assertRaises(ValueError):
            self.desk.set_trial_baseline({"id": job["id"], "version": job["version"], "revision": revision})
        self.desk.set_trial_baseline({"id": baseline["id"], "version": baseline["version"], "revision": revision}, clear=True)
        self.assertIsNone(self.desk._trial_baseline)

    def test_invalid_generator_options_allocate_no_run_or_worker(self):
        payload = self.payload()
        before = set(self.desk.root.iterdir())
        invalid = [{**payload, "generator_steps": value} for value in (None, False, True, 0, 13, 1.0, "2", [], {})]
        invalid += [{**payload, "generator_steps": 2, **extra} for extra in (
            {"trace_lines": True}, {"modules": [payload["file"]], "module_set_sha256": "a" * 64},
            {"mode": "head_current"})]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.desk.start_experiment(value)
        self.assertEqual(set(self.desk.root.iterdir()), before)
        self.assert_no_operations()

    def test_cancelled_or_missing_generator_report_cannot_publish_a_result(self):
        payload = {**self.payload(), "generator_steps": 2}
        report = self.report()
        report["reported_result"] = None
        with patch.object(experiments, "run_experiment", return_value=report):
            self.desk.start_experiment(payload)
            self.join_experiment()
        self.assertEqual(self.desk.experiment_status()["status"], "incomplete")
        self.assertNotIn("result_text", self.desk.experiment_status())
        def cancelled(*args, **kwargs):
            self.desk.experiment_cancel_event.set()
            return self.report()
        with patch.object(experiments, "run_experiment", side_effect=cancelled):
            self.desk.start_experiment(payload)
            self.join_experiment()
        job = self.desk.experiment_status()
        self.assertEqual((job["status"], job["generator_steps"]), ("cancelled", 2))
        self.assertEqual(job["input_text"], payload["input_text"])
        self.assertNotIn("result_text", job)

    def test_large_generator_result_keeps_complete_json_within_browser_budget(self):
        report = self.report()
        value = {"generator": {"yields": [[0] * 30000]}}
        report["reported_result"] = value
        self.assertGreater(len(json.dumps(value, indent=2)), 128 * 1024)
        with patch.object(experiments, "run_experiment", return_value=report):
            self.desk.start_experiment({**self.payload(), "generator_steps": 1})
            self.join_experiment()
        result = self.desk.experiment_status()["result_text"]
        self.assertEqual(json.loads(result), value)
        self.assertLessEqual(len(result.encode()), 128 * 1024)

    def test_opt_in_trace_is_separate_from_result_and_bound_to_original_input(self):
        payload = {**self.payload(), "trace_lines": True}
        previous = self.reading_state()
        trace = {"line_events": [5, 5], "truncated": False, "hook_intact": True}
        report = self.report()
        report["reported_trace"] = trace
        report["execution"]["guest_output"]["stdout"] = (
            "ordinary output\nFORGE8_GUEST_TRACE=" + json.dumps(trace) +
            "\nFORGE8_GUEST_RESULT=" + json.dumps(report["reported_result"]) + "\n")
        with patch.object(experiments, "run_experiment", return_value=report) as run:
            self.desk.start_experiment(payload)
            self.join_experiment()
        job = self.desk.experiment_status()
        self.assertEqual(job["status"], "completed")
        self.assertIs(job["trace_lines"], True)
        self.assertEqual(job["reported_trace"], trace)
        self.assertEqual(job["input_text"], payload["input_text"])
        self.assertEqual(job["stdout"], "ordinary output")
        self.assertEqual(json.loads(job["result_text"]), report["reported_result"])
        self.assertEqual(self.reading_state(), previous)
        self.assertIs(run.call_args.kwargs["trace_lines"], True)
        self.assertEqual(run.call_args.kwargs["expected_input_sha256"],
            hashlib.sha256(payload["input_text"].encode("utf-8")).hexdigest())
        trace["line_events"].append(1)
        self.assertEqual(self.desk.experiment_status()["reported_trace"]["line_events"], [5, 5])

    def test_trace_requires_boolean_and_single_file_before_state_or_execution(self):
        payload = self.payload()
        before = set(self.desk.root.iterdir())
        invalid = [{**payload, "trace_lines": value} for value in (None, 0, 1, "true", [], {})]
        invalid += [{**payload, "trace_lines": True, "modules": [payload["file"]],
            "module_set_sha256": "a" * 64}, {**payload, "trace_lines": True, "mode": "head_current"}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.desk.start_experiment(value)
        self.assertEqual(set(self.desk.root.iterdir()), before)
        self.assert_no_operations()

    def test_trace_omitted_or_false_preserves_legacy_worker_and_job_shape(self):
        for extra in ({}, {"trace_lines": False}):
            with self.subTest(extra=extra), patch.object(experiments, "run_experiment", return_value=self.report()) as run:
                self.desk.start_experiment({**self.payload(), **extra})
                self.join_experiment()
                self.assertNotIn("trace_lines", run.call_args.kwargs)
                self.assertNotIn("watch_names", run.call_args.kwargs)
                self.assertNotIn("expected_input_sha256", run.call_args.kwargs)
                job = self.desk.experiment_status()
                self.assertNotIn("trace_lines", job)
                self.assertNotIn("watch_names", job)
                self.assertNotIn("reported_trace", job)

    def test_incomplete_trace_cannot_survive_drift_process_output_or_runtime_failure(self):
        for field, value in (("source_unchanged", False), ("runtime_unchanged", False),
                ("process_status", "timed_out"), ("output_limit", True), ("host_status", "trap")):
            report = self.report()
            report["reported_trace"] = {"line_events": [5], "truncated": False, "hook_intact": True}
            (report["execution"] if field in {"output_limit", "host_status"} else report)[field] = value
            with self.subTest(field=field), patch.object(experiments, "run_experiment", return_value=report):
                self.desk.start_experiment({**self.payload(), "trace_lines": True})
                self.join_experiment()
                job = self.desk.experiment_status()
                self.assertEqual(job["status"], "incomplete")
                self.assertIsNone(job["reported_trace"])

    def test_cancelled_trace_keeps_input_choice_but_no_path_or_result(self):
        payload = {**self.payload(), "trace_lines": True}
        def cancelled(*args, **kwargs):
            self.desk.experiment_cancel_event.set()
            report = self.report()
            report["reported_trace"] = {"line_events": [5], "truncated": False, "hook_intact": True}
            return report
        with patch.object(experiments, "run_experiment", side_effect=cancelled):
            self.desk.start_experiment(payload)
            self.join_experiment()
        job = self.desk.experiment_status()
        self.assertEqual(job["status"], "cancelled")
        self.assertIs(job["trace_lines"], True)
        self.assertEqual(job["input_text"], payload["input_text"])
        for key in ("reported_trace", "result_text", "stdout", "stderr"):
            self.assertNotIn(key, job)

    def test_preparation_hashes_whole_raw_module_not_sanitized_display(self):
        previous = self.reading_state()
        target = self.target()
        view = self.desk.source_view(target["file"], target["version"])
        self.assertIn('TAG = "x\\u200by"', view["lines"])
        self.assertNotEqual("\n".join(view["lines"]).encode("utf-8"), self.code)
        with patch.object(experiments, "prepare_module", wraps=experiments.prepare_module) as prepare:
            result = self.desk.prepare_experiment(target)
        prepare.assert_called_once_with(self.code, "entry")
        self.assertEqual(result, {**target, "path": "main.py",
            "source_sha256": hashlib.sha256(self.code).hexdigest(), "source_bytes": len(self.code)})
        self.assertEqual(self.reading_state(), previous)
        self.assert_no_operations()

    def test_bad_prepare_targets_and_missing_runtime_start_no_operation(self):
        target = self.target()
        for payload in (None, [], {}, {**target, "extra": True}, {**target, "file": "../main.py"},
                        {**target, "version": "stale"}, {**target, "entry": "entry()"},
                        {**target, "entry": "owner.entry"}, {**target, "entry": "missing"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.desk.prepare_experiment(payload)
        (self.runtime / "runtime.json").unlink()
        with self.assertRaisesRegex(ValueError, "runtime is not installed"):
            self.desk.prepare_experiment(target)
        self.assert_no_operations()

    def test_duplicate_conditional_async_or_oversized_modules_cannot_be_prepared(self):
        for code in (
            b"def entry():\n    pass\ndef entry():\n    pass\n",
            b"if True:\n    def entry():\n        pass\n",
            b"async def entry():\n    pass\n",
            b"def entry():\n    pass\n#" + b"x" * experiments.MAX_SOURCE_BYTES,
        ):
            with self.subTest(prefix=code[:40]):
                self.live.write_bytes(code)
                self.desk.refresh()
                with self.assertRaises(ValueError):
                    self.desk.prepare_experiment(self.target())
        self.assert_no_operations()

    def test_exact_consent_version_hash_and_raw_json_are_required_before_worker(self):
        valid = self.payload()
        invalid = [None, [], {}, {**valid, "extra": True},
                   {key: value for key, value in valid.items() if key != "allow_execution"}]
        invalid += [{**valid, "allow_execution": consent} for consent in (False, None, 1, "true")]
        invalid += [{**valid, "version": "stale"}, {**valid, "source_sha256": "0" * 64},
                    {**valid, "input_text": {"args": [], "kwargs": {}}}]
        invalid += [{**valid, "input_text": text} for text in (
            '{"args":[', '{"args":[],"args":[1],"kwargs":{}}',
            '{"args":[1e999],"kwargs":{}}', " " * (experiments.MAX_INPUT_BYTES + 1),
        )]
        before = set(self.desk.root.iterdir())
        for payload in invalid:
            with self.subTest(payload=repr(payload)[:100]), self.assertRaises(ValueError):
                self.desk.start_experiment(payload)
        self.assertEqual(set(self.desk.root.iterdir()), before)
        self.assert_no_operations()

    def test_result_is_lossless_text_from_retained_source_and_preserves_reading_state(self):
        payload = self.payload()
        self.desk.job = {"id": "owned-reading", "status": "answered", "question": "Keep this question",
            "reader": "qwen35", "version": self.desk.project["version"], "gpu": "released",
            "result": {"focus": [{"path": "main.py", "start_line": 4, "end_line": 5}],
                       "answer": "Owned prior result"}}
        self.desk.started, self.desk.finished = 1.0, 2.0
        with self.desk.lock:
            self.desk._remember_finished_job()
        previous = self.reading_state()
        self.live.write_bytes(b"raise RuntimeError('different LIVE source; never execute')\n")

        def run(runtime, source, entry, inputs, destination, **options):
            self.assertEqual(runtime, self.runtime)
            self.assertEqual(source, Path(self.desk.browse_snapshot.snapshot_root) / "main.py")
            self.assertNotEqual(source, self.live)
            self.assertEqual(source.read_bytes(), self.code)
            self.assertEqual(entry, "entry")
            self.assertEqual(inputs.read_bytes(), payload["input_text"].encode("utf-8"))
            self.assertEqual(destination, inputs.parent / "execution")
            self.assertIs(options["allow_execution"], True)
            self.assertEqual(options["expected_source_sha256"], payload["source_sha256"])
            self.assertFalse(options["cancel_requested"]())
            return self.report()

        with patch.object(experiments, "run_experiment", side_effect=run) as runner:
            identifier = self.desk.start_experiment(payload)["id"]
            self.join_experiment()
        runner.assert_called_once()
        result = self.desk.experiment_status()
        self.assertEqual(result["id"], identifier)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["input_text"], payload["input_text"])
        self.assertEqual(json.loads(result["result_text"]), {"return": 9007199254740993})
        self.assertIn("9007199254740993", result["result_text"])
        self.assertNotIn("reported_result", result)
        self.assertEqual(result["stdout"], "owned output")
        self.assertEqual(self.reading_state(), previous)
        result["result_text"] = "caller mutation"
        self.assertIn("9007199254740993", self.desk.experiment_status()["result_text"])
        for guard in self.guards:
            guard.assert_not_called()

    def test_result_text_keeps_readable_unicode_and_escapes_nonprinting_characters_losslessly(self):
        value = {"文字": '實習：中文 "quoted"\nnext 😀', "id": 9007199254740993,
            "hidden": "x\u200b\u202e\u2028\ud800\U000e0001y", "literal": r"\u4e2d"}
        with patch.object(experiments, "run_experiment", return_value=self.report(value)):
            self.desk.start_experiment(self.payload())
            self.join_experiment()
        result = self.desk.experiment_status()
        self.assertEqual(result["status"], "completed")
        text = result["result_text"]
        self.assertEqual(json.loads(text), {"return": value})
        self.assertIn("實習：中文", text)
        self.assertIn("😀", text)
        self.assertIn("9007199254740993", text)
        self.assertIn(r"\u200b\u202e\u2028\ud800\udb40\udc01", text)
        self.assertNotIn("\u202e", text)
        text.encode("utf-8", "strict")

    def test_retained_source_change_after_preparation_refuses_before_execution(self):
        payload = self.payload()
        retained = Path(self.desk.browse_snapshot.snapshot_root) / "main.py"
        retained.chmod(stat.S_IREAD | stat.S_IWRITE)
        retained.write_bytes(self.code.replace(b"TAG", b"NEW"))
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            self.desk.start_experiment(payload)
        self.assertEqual(self.desk.experiment_status(), {"id": None, "status": "idle"})
        self.assert_no_operations()

    def test_active_experiment_blocks_model_refresh_and_second_run_until_cancel_cleanup(self):
        payload = self.payload()
        entered, release = threading.Event(), threading.Event()
        observed = {}

        def run(*_args, **options):
            observed["cancel"] = options["cancel_requested"]
            entered.set()
            self.assertTrue(release.wait(3), "test did not release mock experiment")
            self.assertTrue(options["cancel_requested"]())
            return self.report("late result must not survive cancellation")

        with patch.object(experiments, "run_experiment", side_effect=run) as runner:
            identifier = self.desk.start_experiment(payload)["id"]
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.desk.experiment_status()["status"], "running")
                for action in (lambda: self.desk.start_experiment(payload), self.desk.refresh,
                               lambda: self.desk.start(self.question())):
                    with self.assertRaises(ValueError):
                        action()
                with self.assertRaisesRegex(ValueError, "unknown isolated experiment"):
                    self.desk.cancel_experiment({"id": "wrong"})
                self.assertFalse(observed["cancel"]())
                self.assertEqual(self.desk.cancel_experiment({"id": identifier}), {"status": "cancelling"})
                self.assertTrue(observed["cancel"]())
                with self.assertRaises(ValueError):
                    self.desk.refresh()
            finally:
                release.set()
                self.join_experiment()
        runner.assert_called_once()
        result = self.desk.experiment_status()
        self.assertEqual(result["status"], "cancelled")
        for key in ("result_text", "stdout", "stderr"):
            self.assertNotIn(key, result)
        self.assertFalse(self.desk._busy())
        with patch.object(experiments, "run_experiment", return_value=self.report(4)):
            self.desk.start_experiment(payload)
            self.join_experiment()
        self.assertEqual(json.loads(self.desk.experiment_status()["result_text"]), {"return": 4})
        self.desk.refresh()
        self.assertEqual(self.desk.experiment_status(), {"id": None, "status": "idle"})

    def test_active_model_question_refuses_cpu_execution_without_replacing_model_job(self):
        payload = self.payload()
        entered, release = threading.Event(), threading.Event()

        def model(_args, **_hooks):
            entered.set()
            self.assertTrue(release.wait(3), "test did not release mock model")

        with patch.object(desk_module, "_run_explain_cli", side_effect=model) as runner:
            identifier = self.desk.start(self.question())["id"]
            try:
                self.assertTrue(entered.wait(3))
                before = deepcopy(self.desk.job)
                with self.assertRaisesRegex(ValueError, "only one model question"):
                    self.desk.start_experiment(payload)
                self.assertEqual(self.desk.job, before)
                self.assertEqual(self.desk.job["id"], identifier)
                self.assertIsNone(self.desk.experiment_worker)
            finally:
                release.set()
                self.desk.worker.join(5)
        self.assertFalse(self.desk.worker.is_alive())
        runner.assert_called_once()
        self.guards[0].assert_not_called()

    def test_closing_requests_cpu_cancel_joins_worker_and_rejects_late_requests(self):
        payload = self.payload()
        entered, cancelled = threading.Event(), threading.Event()

        def run(*_args, **options):
            entered.set()
            self.assertTrue(self.desk.experiment_cancel_event.wait(3), "close did not request cancellation")
            self.assertTrue(options["cancel_requested"]())
            cancelled.set()
            return self.report("cancelled result")

        with patch.object(experiments, "run_experiment", side_effect=run):
            self.desk.start_experiment(payload)
            self.assertTrue(entered.wait(3))
            self.desk.close()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.desk._busy())
        self.assertEqual(self.desk.experiment_status()["status"], "cancelled")
        with self.assertRaisesRegex(ValueError, "closing"):
            self.desk.prepare_experiment(self.target())
        with self.assertRaisesRegex(ValueError, "closing"):
            self.desk.start_experiment(payload)

    def test_preparation_and_run_reject_comparison_mode_without_executing_git(self):
        before = self.code.replace(b"value,", b"value + 1,")
        with patch.object(comparison, "read_head", return_value=GitBaseline("a" * 40, {"main.py": before}, ())), \
                patch.object(comparison, "head_identity", return_value="a" * 40):
            self.desk.refresh(mode="changes")
        target = self.target(path="after/main.py")
        with self.assertRaisesRegex(ValueError, "ordinary source mode"):
            self.desk.prepare_experiment(target)
        payload = {**target, "source_sha256": hashlib.sha256(self.code).hexdigest(),
                   "input_text": '{"args":[],"kwargs":{}}', "allow_execution": True}
        with self.assertRaisesRegex(ValueError, "ordinary source mode"):
            self.desk.start_experiment(payload)
        self.assert_no_operations()

    def test_worker_failure_keeps_reading_answer_and_frees_cpu_slot(self):
        payload = self.payload()
        before = self.reading_state()
        with patch.object(experiments, "run_experiment", side_effect=ValueError("owned pre-execution refusal")):
            self.desk.start_experiment(payload)
            self.join_experiment()
        result = self.desk.experiment_status()
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("owned pre-execution refusal", result["error"])
        self.assertNotIn("result_text", result)
        self.assertFalse(self.desk._busy())
        self.assertEqual(self.reading_state(), before)

    def test_uncertain_cleanup_is_not_overwritten_by_cancel_and_blocks_new_work(self):
        payload = self.payload()
        before = self.reading_state()

        def uncertain(*args, **options):
            self.desk.experiment_cancel_event.set()
            raise CheckCleanupError(123)

        with patch.object(experiments, "run_experiment", side_effect=uncertain):
            self.desk.start_experiment(payload)
            self.join_experiment()
        result = self.desk.experiment_status()
        self.assertEqual(result["status"], "incomplete")
        self.assertIs(result["cleanup_unknown"], True)
        self.assertIn("123", result["error"])
        self.assertNotIn("result_text", result)
        self.assertTrue(self.desk._busy())
        for action in (lambda: self.desk.start_experiment(payload), self.desk.refresh,
                       lambda: self.desk.start(self.question())):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(self.reading_state(), before)
