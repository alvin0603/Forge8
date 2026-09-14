"""One explicitly pinned trial; real snapshots, synthesized reports, no guest."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from forge8 import desk as desk_module, experiments
from forge8.checks import CheckCleanupError
import test_desk as fixtures


def encoded(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2).encode("utf-8")


def sha(value):
    return hashlib.sha256(value).hexdigest()


class DeskBaselineTests(fixtures.DeskFixture):
    def setUp(self):
        super().setUp()
        self.desk.close()
        self.code = (b"raise RuntimeError('NEVER EXECUTE THIS OWNED SOURCE')\r\n"
                     b"def entry(value):\r\n    return value\r\n")
        (self.source / "main.py").write_bytes(self.code)
        (self.source / "other.py").write_bytes(self.code)
        for guard in (
                patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.root / "state")}),
                patch.object(experiments, "native_platform", return_value="linux"),
                patch("subprocess.Popen", side_effect=AssertionError("no subprocess")),
                patch.object(experiments, "_worker", side_effect=AssertionError("no guest")),
                patch.object(experiments, "run_experiment", side_effect=AssertionError("unmocked trial")),
                patch.object(desk_module, "_run_explain_cli", side_effect=AssertionError("no inference"))):
            guard.start()
            self.addCleanup(guard.stop)
        self.runtime = self.assets / f"experiment-{experiments.RUNTIME_NAME}-linux"
        self.runtime.mkdir()
        self.cache = {"filename": "python.cwasm", "size_bytes": 123, "sha256": "c" * 64}
        # A synthetic manifest for report identity, never a provisioned runtime.
        self.manifest = {"experiment": deepcopy(experiments._metadata()),
            "install_dir": "installed", "installed_files": [self.cache]}
        (self.runtime / "runtime.json").write_bytes(encoded(self.manifest))
        self.desk = desk_module.ReadingDesk(self.source, allow_experiments=True)
        self.addCleanup(self.desk.close)
        self.emitted = []

    def payload(self, *, input_text=' {"args":[9007199254740993],"kwargs":{}}\n', trace=False):
        item = next(row for row in self.desk.project["files"] if row["path"] == "main.py")
        target = self.desk.prepare_experiment({"file": item["id"],
            "version": self.desk.project["version"], "entry": "entry"})
        value = {key: target[key] for key in ("file", "version", "entry", "source_sha256")}
        value.update(input_text=input_text, allow_execution=True)
        if trace:
            value["trace_lines"] = True
        return value

    def runner(self, result=None, *, mutate=None, disk=None, after=None):
        result = {"return": 9007199254740993} if result is None else result

        def run(runtime, source, entry, inputs, run_root, **options):
            self.assertEqual(runtime, self.runtime)
            self.assertIs(options["allow_execution"], True)
            raw_source, raw_input = source.read_bytes(), inputs.read_bytes()
            identity = {"source_sha256": sha(raw_source), "source_bytes": len(raw_source),
                "input_sha256": sha(raw_input), "input_bytes": len(raw_input), "entry": entry}
            if options.get("trace_lines"):
                identity["trace_lines"] = True
            report = {"schema_version": 1, "kind": "forge8.experiment", "identity": identity,
                "runtime": {**deepcopy(experiments._metadata()), "cache": deepcopy(self.cache)},
                "source_unchanged": True, "runtime_unchanged": True, "postcheck_errors": [],
                "process_status": "passed", "duration_seconds": 0.01, "elapsed_seconds": 0.02,
                "run_root": str(run_root), "reported_result": deepcopy(result),
                "execution": {"host_status": "exited", "detail": None, "output_limit": False,
                    "load_seconds": 0.01, "worker_seconds": 0.01, "guest_output": {
                        "stdout": "PRIVATE LOG\nFORGE8_GUEST_RESULT=" + json.dumps(result) + "\n",
                        "stderr": "PRIVATE STDERR"}}, "notice": "SYNTHETIC TEST REPORT"}
            if options.get("trace_lines"):
                report["reported_trace"] = None
            if mutate is not None:
                mutate(report)
            run_root.mkdir()
            raw_report = encoded(report) if disk is None else disk(report)
            (run_root / "experiment.json").write_bytes(raw_report)
            self.emitted.append((deepcopy(report), raw_report, run_root, dict(options)))
            if after is not None:
                after(source, inputs, run_root)
            return report
        return run

    def join(self):
        self.desk.experiment_worker.join(5)
        self.assertFalse(self.desk.experiment_worker.is_alive(), "owned mock worker did not finish")

    def run_trial(self, result=None, *, payload=None, **options):
        payload = self.payload() if payload is None else payload
        with patch.object(experiments, "run_experiment", side_effect=self.runner(result, **options)) as run:
            self.desk.start_experiment(payload)
            self.join()
        run.assert_called_once()
        return self.desk.experiment_status()

    def comparison(self):
        return self.desk.experiment_status()["input_comparison"]

    def command(self, *, identifier=None):
        value = self.comparison()
        return {"id": value["current_id"] if identifier is None else identifier,
            "version": self.desk.project["version"], "revision": value["revision"]}

    def pin(self):
        return self.desk.set_trial_baseline(self.command())

    def test_completed_capture_pin_is_detached_and_polling_is_historical_only(self):
        self.assertNotIn("input_comparison", self.desk.experiment_status())
        payload = self.payload()
        job = self.run_trial(payload=payload)
        self.assertEqual(job["status"], "completed")
        before = self.comparison()
        self.assertIs(before["can_pin"], True)
        self.assertEqual(before["reason"], "no_baseline")
        pinned = self.pin()
        baseline = pinned["baseline"]
        self.assertEqual(pinned["revision"], before["revision"] + 1)
        self.assertEqual((pinned["outcome"], pinned["reason"]), ("unavailable", "same_run"))
        self.assertIs(pinned["can_pin"], False)
        self.assertEqual(baseline["source_sha256"], sha(self.code))
        self.assertEqual(baseline["input_text"], payload["input_text"])
        self.assertEqual(baseline["input_sha256"], sha(payload["input_text"].encode("utf-8")))
        self.assertEqual(baseline["report_sha256"], sha(self.emitted[-1][1]))
        self.assertEqual(baseline["runtime_sha256"], sha(encoded(self.manifest)))
        self.assertNotIn("PRIVATE", json.dumps(baseline))
        baseline["input_text"] = "caller mutation"
        (self.emitted[-1][2] / "experiment.json").write_bytes(b"changed AFTER capture")
        (self.source / "main.py").write_bytes(b"saved live editor change, not refreshed")
        with patch.object(experiments, "_file", side_effect=AssertionError("status reread disk")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("status reread path")):
            self.assertEqual(self.comparison()["baseline"]["input_text"], payload["input_text"])
            self.assertEqual(self.comparison()["baseline"]["report_sha256"], sha(self.emitted[-1][1]))

    def test_manual_next_inputs_compare_exact_large_integers_without_replacing_a(self):
        self.run_trial()
        baseline = self.pin()["baseline"]
        draft = '{"args":[9007199254740994],"kwargs":{}}'
        same = self.run_trial(payload=self.payload(input_text=draft))["input_comparison"]
        self.assertEqual(same["outcome"], "same")
        self.assertIs(same["can_pin"], True)
        self.assertEqual(same["baseline"], baseline)
        different = self.run_trial({"return": 9007199254740994},
            payload=self.payload(input_text=draft))
        self.assertEqual(different["input_comparison"]["outcome"], "different")
        self.assertEqual(different["input_comparison"]["baseline"], baseline)
        self.assertIn("9007199254740993", baseline["result_text"])
        self.assertIn("9007199254740994", different["result_text"])
        self.assertNotEqual(sha(draft.encode()), baseline["input_sha256"])

    def test_typed_reports_and_ordinary_exceptions_are_not_test_verdicts(self):
        error = {"exception": "ValueError", "message": "owned failure", "phase": "call"}
        cases = [({"return": True}, {"return": 1}, "different"),
            ({"return": 1}, {"return": 1.0}, "different"),
            ({"return": {"b": 2, "a": 1}}, {"return": {"a": 1, "b": 2}}, "same"),
            (error, deepcopy(error), "same"),
            (error, {**error, "phase": "module_initialization"}, "different")]
        for left, right, outcome in cases:
            with self.subTest(left=left, right=right):
                self.run_trial(left)
                self.pin()
                current = self.run_trial(right)
                self.assertEqual(current["status"], "completed")
                self.assertEqual(current["input_comparison"]["outcome"], outcome)
                self.assertEqual(json.loads(current["result_text"]), right)

    def test_revision_rejects_clear_then_repin_aba_and_exact_shape_errors(self):
        self.run_trial()
        self.pin()
        old_clear = self.command()
        self.desk.set_trial_baseline(old_clear, clear=True)
        self.pin()  # Same trial id, newer baseline revision.
        expected = self.comparison()
        with self.assertRaises(ValueError):
            self.desk.set_trial_baseline(old_clear, clear=True)
        valid = self.command()
        invalid = [{**valid, "id": "other"}, {**valid, "version": "stale"},
            {**valid, "revision": True}, {**valid, "revision": 1.0},
            {**valid, "revision": -1}, {**valid, "revision": 2**53},
            {**valid, "result_text": "caller-supplied"}, {"id": valid["id"]}, None]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.desk.set_trial_baseline(value, clear=True)
        self.assertEqual(self.comparison(), expected)

    def test_strict_capture_flags_do_not_relabel_ordinary_completed_result(self):
        mutations = [lambda r: r.update(schema_version=True),
            lambda r: r.update(source_unchanged=1), lambda r: r.update(runtime_unchanged=1),
            lambda r: r.update(postcheck_errors=["reported postcheck failure"]),
            lambda r: r["execution"].update(host_status="guest_exit", detail=False),
            lambda r: r["execution"].update(output_limit=0),
            lambda r: r["identity"].update(input_bytes=float(r["identity"]["input_bytes"])),
            lambda r: r["identity"].update(input_sha256="0" * 64),
            lambda r: r["identity"].update(source_sha256="0" * 64),
            lambda r: r.update(reported_result={"return": True})]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                job = self.run_trial({"return": 1}, mutate=mutate)
                self.assertEqual(job["status"], "completed")
                self.assertIn("result_text", job)
                self.assertIs(job["input_comparison"]["can_pin"], False)
                self.assertIsNone(job["input_comparison"]["current_report_sha256"])
                with self.assertRaises(ValueError):
                    self.pin()

    def test_malformed_changed_oversize_or_missing_report_cannot_be_pinned(self):
        disks = [lambda r: b"not JSON", lambda r: b'{"duplicate":1,"duplicate":2}',
            lambda r: encoded({**r, "reported_result": {"return": True}}),
            lambda r: b" " * (2 * 1024**2 + 1)]
        for index, disk in enumerate(disks):
            with self.subTest(index=index):
                job = self.run_trial({"return": 1}, disk=disk)
                self.assertEqual(job["status"], "completed")
                self.assertEqual(json.loads(job["result_text"]), {"return": 1})
                self.assertIs(job["input_comparison"]["can_pin"], False)
        job = self.run_trial(after=lambda _s, _i, root: (root / "experiment.json").unlink())
        self.assertEqual(job["status"], "completed")
        self.assertIs(job["input_comparison"]["can_pin"], False)

    def test_large_display_is_unavailable_whole_without_changing_ordinary_result(self):
        value = "x" * (128 * 1024)
        job = self.run_trial({"return": value})
        self.assertEqual(job["status"], "completed")
        self.assertEqual(json.loads(job["result_text"]), {"return": value})
        self.assertIs(job["input_comparison"]["can_pin"], False)
        self.assertIsNone(self.desk._trial_candidate)

    def test_capture_boundary_checks_raw_input_source_and_runtime_drift(self):
        original_manifest = encoded(self.manifest)
        for changed in ("input", "source", "runtime"):
            def drift(source, inputs, _root):
                target = {"input": inputs, "source": source,
                    "runtime": self.runtime / "runtime.json"}[changed]
                target.write_bytes(target.read_bytes() + b" ")
            with self.subTest(changed=changed):
                payload = self.payload()
                try:
                    job = self.run_trial(payload=payload, after=drift)
                    self.assertEqual(job["status"], "completed")
                    self.assertIs(job["input_comparison"]["can_pin"], False)
                finally:
                    retained = Path(self.desk.browse_snapshot.snapshot_root) / "main.py"
                    retained.write_bytes(self.code)
                    (self.runtime / "runtime.json").write_bytes(original_manifest)

    def test_runtime_and_trace_policy_mismatch_refuse_comparison_not_execution(self):
        self.run_trial()
        baseline = self.pin()["baseline"]
        (self.runtime / "runtime.json").write_bytes(encoded(self.manifest) + b"\n")
        changed = self.run_trial()["input_comparison"]
        self.assertIs(changed["can_pin"], True)
        self.assertEqual((changed["outcome"], changed["reason"]), ("unavailable", "runtime_changed"))
        (self.runtime / "runtime.json").write_bytes(encoded(self.manifest))
        traced = self.run_trial(payload=self.payload(trace=True))["input_comparison"]
        self.assertEqual((traced["outcome"], traced["reason"]), ("unavailable", "trace_mode_changed"))
        self.assertEqual(traced["baseline"], baseline)

    def test_same_bytes_at_a_different_file_are_not_the_same_trial_target(self):
        self.run_trial()
        baseline = self.pin()["baseline"]
        item = next(row for row in self.desk.project["files"] if row["path"] == "other.py")
        target = self.desk.prepare_experiment({"file": item["id"],
            "version": self.desk.project["version"], "entry": "entry"})
        payload = {**self.payload(), **{key: target[key] for key in
            ("file", "version", "entry", "source_sha256")}}
        value = self.run_trial(payload=payload)["input_comparison"]
        self.assertEqual(value["baseline"], baseline)
        self.assertIs(value["can_pin"], True)
        self.assertEqual((value["outcome"], value["reason"]), ("unavailable", "source_changed"))

    def test_terminal_worker_settling_cannot_pin_until_thread_returns(self):
        entered, release = threading.Event(), threading.Event()
        original_thread = threading.Thread

        class HeldCompletion(original_thread):
            def run(inner_self):
                super().run()
                entered.set()
                release.wait(5)

        payload = self.payload()
        with patch.object(desk_module.threading, "Thread", HeldCompletion), \
                patch.object(experiments, "run_experiment", side_effect=self.runner()):
            self.desk.start_experiment(payload)
            try:
                self.assertTrue(entered.wait(3))
                job = self.desk.experiment_status()
                self.assertEqual(job["status"], "completed")
                self.assertIs(job["input_comparison"]["settling"], True)
                self.assertIs(job["input_comparison"]["can_pin"], False)
                with self.assertRaises(ValueError):
                    self.pin()
            finally:
                release.set()
                self.join()
        self.assertIs(self.comparison()["settling"], False)
        self.assertIs(self.comparison()["can_pin"], True)

    def test_cancelled_next_worker_keeps_baseline_without_any_comparison(self):
        self.run_trial()
        baseline = self.pin()["baseline"]
        entered, release = threading.Event(), threading.Event()

        def hold(_source, _inputs, _root):
            entered.set()
            self.assertTrue(release.wait(5))

        with patch.object(experiments, "run_experiment", side_effect=self.runner(after=hold)):
            identifier = self.desk.start_experiment(self.payload())["id"]
            try:
                self.assertTrue(entered.wait(3))
                self.assertIs(self.comparison()["can_pin"], False)
                with self.assertRaises(ValueError):
                    self.desk.set_trial_baseline(self.command(identifier=baseline["id"]), clear=True)
                self.desk.cancel_experiment({"id": identifier})
            finally:
                release.set()
                self.join()
        job = self.desk.experiment_status()
        self.assertEqual(job["status"], "cancelled")
        self.assertNotIn("result_text", job)
        self.assertEqual(job["input_comparison"]["baseline"], baseline)
        self.assertEqual(job["input_comparison"]["outcome"], "unavailable")
        self.assertIsNone(job["input_comparison"]["current_report_sha256"])

    def test_failed_next_run_and_unknown_cleanup_never_replace_the_baseline(self):
        self.run_trial()
        baseline = self.pin()["baseline"]
        incomplete = self.run_trial(mutate=lambda r: r.update(process_status="timed_out"))
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertEqual(incomplete["input_comparison"]["baseline"], baseline)
        self.assertEqual(incomplete["input_comparison"]["outcome"], "unavailable")
        self.assertIs(incomplete["input_comparison"]["can_pin"], False)
        with patch.object(experiments, "run_experiment", side_effect=CheckCleanupError(123)):
            self.desk.start_experiment(self.payload())
            self.join()
        job = self.desk.experiment_status()
        self.assertIs(job["cleanup_unknown"], True)
        self.assertEqual(job["input_comparison"]["baseline"], baseline)
        self.assertEqual(job["input_comparison"]["outcome"], "unavailable")
        self.assertIs(job["input_comparison"]["can_pin"], False)
        with self.assertRaises(ValueError):
            self.desk.set_trial_baseline(self.command(identifier=baseline["id"]), clear=True)

    def test_failed_refresh_preserves_baseline_success_same_hash_refresh_clears(self):
        self.run_trial()
        self.pin()
        before = self.desk.experiment_status()
        old_command = self.command()
        with patch.object(desk_module, "prepare_repository_snapshot", side_effect=ValueError("owned refusal")):
            with self.assertRaises(ValueError):
                self.desk.refresh()
        self.assertEqual(self.desk.experiment_status(), before)
        version = self.desk.project["version"]
        self.assertEqual(self.desk.refresh()["version"], version)
        self.assertEqual(self.desk.experiment_status(), {"id": None, "status": "idle"})
        self.assertIsNone(self.desk._trial_baseline)
        self.assertIsNone(self.desk._trial_candidate)
        with self.assertRaises(ValueError):
            self.desk.set_trial_baseline(old_command)

    def test_module_paired_disabled_browse_and_closed_baseline_actions_refuse(self):
        payload = self.payload()
        target = self.desk.prepare_experiment({key: payload[key] for key in ("file", "version", "entry")} |
            {"modules": [payload["file"]]})
        module_job = self.run_trial(payload={**payload, "modules": [payload["file"]],
            "module_set_sha256": target["module_set"]["sha256"]})
        self.assertEqual(module_job["status"], "completed")
        self.assertNotIn("input_comparison", module_job)
        with self.assertRaises(ValueError):
            self.desk.set_trial_baseline({"id": module_job["id"], "version": module_job["version"],
                "revision": self.desk._trial_revision})
        self.run_trial()
        command = self.command()
        with patch.object(self.desk, "comparison", object()):
            self.assertNotIn("input_comparison", self.desk.experiment_status())
            with self.assertRaises(ValueError):
                self.desk.set_trial_baseline(command)
        for options in ({}, {"browse_only": True, "state_directory": self.root / "browse-state"}):
            other = desk_module.ReadingDesk(self.source, **options)
            try:
                for clear in (False, True):
                    with self.assertRaises(ValueError):
                        other.set_trial_baseline(command, clear=clear)
                self.assertIsNone(other.experiment_worker)
            finally:
                other.close()
        self.desk.close()
        with self.assertRaises(ValueError):
            self.desk.set_trial_baseline(command)

    request = fixtures.ReadingHTTPTests.request

    def test_http_pin_clear_use_exact_shapes_and_never_execute(self):
        self.run_trial()
        self.server, url = desk_module.make_server(self.desk)
        self.origin = url.split("/#", 1)[0]
        self.token = parse_qs(urlsplit(url).fragment)["token"][0]
        server_thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        server_thread.start()
        try:
            original = self.command()
            status, _, _ = self.request("/api/experiment/baseline", method="POST",
                payload=original, auth=False, headers_only=True)
            self.assertEqual(status, 401)
            with patch.object(experiments, "_file", side_effect=AssertionError("HTTP pin read disk")):
                status, _, body = self.request("/api/experiment/baseline", method="POST", payload=original)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["baseline"]["id"], original["id"])
                for path in ("/api/experiment/baseline", "/api/experiment/baseline/clear"):
                    status, _, _ = self.request(path, method="POST", payload={**self.command(), "extra": True})
                    self.assertEqual(status, 400)
                status, _, body = self.request("/api/experiment/current")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["input_comparison"], self.comparison())
                status, _, body = self.request("/api/experiment/baseline/clear", method="POST", payload=self.command())
                self.assertEqual(status, 200)
                self.assertIsNone(json.loads(body)["baseline"])
            self.assertEqual(len(self.emitted), 1)
        finally:
            self.server.shutdown()
            self.server.server_close()
            server_thread.join(3)
            self.assertFalse(server_thread.is_alive())
