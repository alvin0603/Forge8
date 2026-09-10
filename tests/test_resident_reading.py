"""Resident reading integration with real snapshots/seals and mocked inference.

No source imports, GPU, native child, or network. The owner fixture supplies a
fake owned supervisor and verified assets; the actual CLI, reading/discovery
engines, cancellation wrapper and resident receipt registration remain in use.
"""

import argparse
import copy
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from forge8 import cli, desk as desk_module, explain, inference, residency
from forge8.operator import AcceptanceGateResult
from forge8.runtime import IntegrityResult
from forge8.server import ServerPreparation
from forge8.trace import verify_trace
from test_explain import ScriptedBackend, passing_gate
from test_structured_reading import answer_document
import test_residency as owner_fixture


def completion(identifier="model-contract"):
    return {"schema_version": 1, "scope": "resident_request", "server_session_id": identifier,
        "request_completed": True, "slot_idle": True, "transport_secret_cleared": True,
        "session_finalization": "pending"}


class ResidentReadingTests(unittest.TestCase):
    # Borrow only fixture methods, not the 26 independent owner test methods.
    start_patch = owner_fixture.ResidentTests.start_patch
    cleanup_owner = owner_fixture.ResidentTests.cleanup_owner

    def setUp(self):
        owner_fixture.ResidentTests.setUp(self)
        self.source = self.root / "source"
        self.source.mkdir()
        self.source_text = 'raise RuntimeError("never import this reading source")\n\ndef answer():\n    return 7\n'
        (self.source / "app.py").write_text(self.source_text, encoding="utf-8")
        self.calls, self.transports, self.outputs = [], [], []
        self.call_hook = None
        self.patch(cli, "_resolve_fix_asset_root", return_value=self.assets)
        self.patch(desk_module, "_resolve_fix_asset_root", return_value=self.assets)
        self.patch(cli, "_fix_runs_parent", return_value=self.runs)
        self.patch(desk_module, "_fix_runs_parent", return_value=self.runs)
        self.patch(cli, "_fix_asset_anchors", return_value=tuple(self.options.values()))
        self.legacy_prepare = self.patch(cli, "prepare_server", side_effect=AssertionError("legacy preparation called"))
        self.legacy_start = self.patch(cli, "LocalServerSupervisor", side_effect=AssertionError("legacy supervisor called"))
        test = self

        def chat(transport, request):
            test.calls.append(request)
            test.transports.append(transport)
            if test.call_hook is not None:
                test.call_hook(transport, request)
            text = test.outputs.pop(0) if test.outputs else json.dumps(answer_document("It returns 7."))
            if isinstance(text, BaseException):
                raise text
            return inference.ChatResponse(text, "stop", {"prompt_tokens": 100, "completion_tokens": 20}, {})

        self.patch(inference.OpenAITransport, "chat", chat)

    def patch(self, target, name, *args, **kwargs):
        value = patch.object(target, name, *args, **kwargs)
        self.addCleanup(value.stop)
        return value.start()

    def run_cli(self, kind="explain", *, question="What does answer return?", cancellation=None, progress=None, **options):
        args = argparse.Namespace(repo=self.source, question=question,
            focus=["app.py:3-4"] if kind == "explain" else [], reader="qwen35", as_json=True)
        results, phases = [], []

        def notify(message):
            phases.append(message)
            if progress is not None:
                progress(message)

        runner = {"explain": cli._run_explain_cli, "locate": cli._run_locate_cli,
            "project": cli._run_project_cli}[kind]
        code = runner(args, resident_model=self.owner, cancel_event=cancellation or threading.Event(),
            on_result=results.append, on_progress=notify, **options)
        self.assertEqual(len(results), 1, results)
        return code, results[0], phases

    def test_direct_request_has_distinct_immutable_manifest_not_legacy_success(self):
        code, result, _ = self.run_cli()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["kind"], "forge8.explain.request")
        self.assertTrue(cli._reading_request_complete(result))
        root = Path(result["run_root"])
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["kind"], "forge8.explanation.request.manifest")
        self.assertIs(manifest["ok"], False)
        self.assertIs(manifest["request_ok"], True)
        self.assertEqual(manifest["server_session_id"], result["request_completion"]["server_session_id"])
        self.assertEqual(manifest["session_finalization"], "pending")
        self.assertTrue(verify_trace(root / "trace.jsonl", root / "trace.seal.json").ok)
        self.assertFalse((root / "server-logs").exists())
        self.assertIn("SESSION CLEANUP PENDING", (root / "ANSWER.txt").read_text(encoding="utf-8"))
        self.assertEqual(cli._explain_gpu_state(result["server"]), "resident")
        self.assertEqual(self.owner.status()["state"], "idle")
        self.assertIsNone(self.supervisors[0].shutdown_result)
        self.assertTrue(all(transport.api_key is None for transport in self.transports))
        self.assertEqual((self.source / "app.py").read_text(encoding="utf-8"), self.source_text)
        self.legacy_prepare.assert_not_called()
        self.legacy_start.assert_not_called()

    def test_existing_one_shot_engine_cannot_accept_a_resident_gate(self):
        prepared = explain.prepare_explanation(self.source, self.runs / "one-shot",
            "Explain answer.", "one-shot", focus=("app.py:3-4",))
        outcome = explain.run_explanation(prepared, ScriptedBackend(["Returns 7. [E1:L4]"]),
            model="scripted", reader="qwen35",
            acceptance_gate=lambda: AcceptanceGateResult(True, None, completion()))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "acceptance_gate_failed")
        self.assertIsNone(outcome.answer)
        manifest = json.loads(Path(outcome.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["kind"], "forge8.explanation.manifest")
        self.assertNotIn("request_ok", manifest)

    def test_failed_presecret_assets_preserve_same_actionable_preparation_as_one_shot(self):
        missing = ServerPreparation("integrity_failed", ("model integrity verification failed",),
            runtime_integrity=IntegrityResult(True, ("runtime",), (), ()),
            model_integrity=IntegrityResult(False, (), ("Missing-Qwen.gguf",), ()))
        self.prepare.return_value = missing
        code, result, _ = self.run_cli()
        self.assertEqual((code, result["status"]), (2, "integrity_failed"))
        self.assertEqual(result["server"], {"preparation": missing.as_dict()})
        self.assertIn("Missing-Qwen.gguf", result["error"])
        self.assertIn("reader qwen35", result["error"])
        self.assertIn("model", result["error"])
        self.assertIn("--manifest", result["error"])
        self.assertEqual(cli._explain_gpu_state(result["server"]), "not_acquired")
        self.assertEqual((self.supervisors, self.calls), ([], []))
        self.assertEqual(self.owner.status()["state"], "unloaded")
        self.assertFalse((self.desk / "model-sessions").exists())
        self.legacy_prepare.assert_not_called()
        self.prepare.assert_called_once()

    def test_failed_presecret_profile_keeps_configuration_reason_without_start(self):
        for status, reason in (("configuration_error", "profile host must be 127.0.0.1"),
                               ("unsupported_platform", "use a pinned native runtime/profile")):
            with self.subTest(status=status):
                self.prepare.return_value = ServerPreparation(status, (reason,))
                code, result, _ = self.run_cli()
                self.assertEqual((code, result["status"], result["error"]), (2, status, reason))
                self.assertFalse(result["server"]["preparation"]["ok"])
                self.assertEqual((self.supervisors, self.calls), ([], []))

    def test_cancellation_wins_over_failed_presecret_preparation(self):
        cancellation = threading.Event()
        def failed(*_args, **_kwargs):
            cancellation.set()
            return ServerPreparation("configuration_error", ("configuration detail",))
        self.prepare.side_effect = failed
        code, result, _ = self.run_cli(cancellation=cancellation)
        self.assertEqual((code, result["status"]), (130, "interrupted"))
        self.assertNotIn("configuration detail", result["error"])
        self.assertEqual((self.supervisors, self.calls), ([], []))

    def test_typed_error_after_generation_exists_is_still_redacted(self):
        original_start = residency.LocalServerSupervisor.start
        def failed_start(supervisor):
            original_start(supervisor)
            raise residency.ResidentPreparationError(ServerPreparation(
                "configuration_error", ("test-private-secret",)))
        with patch.object(residency.LocalServerSupervisor, "start", new=failed_start):
            code, result, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertNotIn("test-private-secret", json.dumps(result))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.supervisors), 1)
        self.assertIsNone(self.supervisors[0].api_key)
        self.assertEqual(self.owner.status()["state"], "unloaded")

    def test_resident_engine_rejects_legacy_gate_and_wrong_session_identity(self):
        for index, gate in enumerate((passing_gate,
                lambda: AcceptanceGateResult(True, None, completion("model-other")))):
            prepared = explain.prepare_explanation(self.source, self.runs / f"resident-gate-{index}",
                "Explain answer.", f"resident-gate-{index}", focus=("app.py:3-4",))
            outcome = explain.run_explanation(prepared, ScriptedBackend(["Returns 7. [E1:L4]"]),
                model="scripted", reader="qwen35", resident_session_id="model-contract", acceptance_gate=gate)
            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.status, "acceptance_gate_failed")
            self.assertIsNone(outcome.request_completion)

    def test_completed_uncited_structured_text_is_not_promoted_or_rescued_as_prose(self):
        self.outputs = [json.dumps(answer_document("It returns the integer seven.", []))]
        code, result, _ = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertEqual(result["status"], "stalled")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["outcome"]["answer"])
        self.assertIsNone(cli._unverified_explain_prose(result))
        self.assertNotIn("unverified_prose", result["outcome"])
        self.assertTrue(cli._reading_request_complete(result))
        self.assertEqual(self.owner.status()["state"], "idle")

    def test_discovery_schema_registers_with_real_owner_without_shutdown(self):
        self.outputs = ['{"candidates":["D0001"]}']
        code, result, _ = self.run_cli("locate")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["kind"], "forge8.locate.request")
        self.assertTrue(cli._reading_request_complete(result))
        artifact = Path(result["run_root"]) / "discovery.json"
        document = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual((document["schema_version"], document["kind"]), (1, "forge8.discovery.request"))
        self.assertEqual(document["server_session_id"], self.owner.status()["id"])
        self.assertEqual(self.owner._generation["requests"][0]["path"], str(artifact))
        self.assertEqual(self.supervisors[0].stop_calls, 0)

    def test_project_discovery_and_answer_reuse_one_owner_and_preserve_both_artifacts(self):
        self.outputs = ['{"candidates":["D0001"]}', json.dumps(answer_document("It returns 7."))]
        code, result, _ = self.run_cli("project")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["project_reading"]["answer_attempted"])
        self.assertEqual(len(self.supervisors), 1)
        self.assertEqual(self.prepare.call_count, 1)
        self.assertEqual(len(self.calls), 2)
        discovery = result["project_reading"]["discovery"]
        self.assertEqual(discovery["request_completion"], result["request_completion"])
        self.assertNotEqual(discovery["run_root"], result["run_root"])
        self.assertEqual(len(self.owner._generation["requests"]), 2)
        self.assertEqual([Path(pin["path"]).name for pin in self.owner._generation["requests"]],
            ["discovery.json", "manifest.json"])

    def test_backend_exception_reclaims_generation_and_never_serializes_secret(self):
        self.outputs = [RuntimeError("test-private-secret")]
        code, result, _ = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["outcome"])
        self.assertNotIn("test-private-secret", json.dumps(result))
        self.assertEqual(self.owner.status()["state"], "unloaded")
        self.assertEqual(cli._explain_gpu_state(result["server"]), "released")
        self.assertIsNone(self.owner._lease)
        self.assertEqual(self.supervisors[0].stop_calls, 1)

    def test_cancel_during_transport_preserves_interrupted_and_observed_release(self):
        cancellation = threading.Event()
        self.call_hook = lambda *_: cancellation.set()
        code, result, _ = self.run_cli(cancellation=cancellation)
        self.assertEqual(code, 130, result)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(cli._explain_gpu_state(result["server"]), "released")
        self.assertIsNone(self.owner._lease)
        self.assertEqual(self.owner.status()["state"], "unloaded")
        self.assertEqual(self.supervisors[0].stop_calls, 1)

    def test_exception_after_acquire_before_transport_is_cleaned_and_redacted(self):
        observed = []

        def progress(message):
            if message == "resident model acquired; preparing the source request":
                observed.append(message)
                raise RuntimeError("test-private-secret")

        code, result, _ = self.run_cli(progress=progress)
        self.assertNotEqual(code, 0)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.owner._lease)
        self.assertEqual(self.owner.status()["state"], "unloaded")
        self.assertEqual(cli._explain_gpu_state(result["server"]), "released")
        self.assertNotIn("test-private-secret", json.dumps(result))

    def test_project_interstage_cancel_closes_exact_completed_generation(self):
        self.outputs = ['{"candidates":["D0001"]}']
        cancellation = threading.Event()

        def progress(message):
            if message == "Project question: preparing complete candidate source.":
                cancellation.set()

        code, result, _ = self.run_cli("project", cancellation=cancellation, progress=progress)
        self.assertEqual(code, 130, result)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.owner.status()["state"], "unloaded")
        self.assertEqual(cli._explain_gpu_state(result["server"]), "released")
        self.assertEqual(len(self.supervisors), 1)

    def test_source_drift_during_completed_request_closes_without_promotion(self):
        self.call_hook = lambda *_: (self.source / "app.py").write_text(self.source_text + "# changed\n", encoding="utf-8")
        code, result, _ = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["outcome"])
        self.assertEqual(self.owner.status()["state"], "unloaded")
        self.assertIsNone(self.owner._lease)

    def test_original_source_guard_refusal_before_and_after_inference(self):
        refused = AcceptanceGateResult(False, "original source changed", {})
        code, result, _ = self.run_cli(source_guard=lambda: refused)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "source_guard_failed")
        self.prepare.assert_not_called()
        guard = Mock(side_effect=[AcceptanceGateResult(True, None, {}), refused])
        code, result, _ = self.run_cli(source_guard=guard)
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(self.owner.status()["state"], "unloaded")
        self.assertEqual(len(self.calls), 1)

    def test_public_completion_must_match_outer_server_and_outcome_identities(self):
        _, result, _ = self.run_cli()
        self.assertTrue(cli._reading_request_complete(result))
        for location in ("server", "outcome", "top"):
            bad = copy.deepcopy(result)
            if location == "server":
                bad["server"]["server_session_id"] = "model-other"
            elif location == "outcome":
                bad["outcome"]["request_completion"]["server_session_id"] = "model-other"
            else:
                bad["request_completion"]["server_session_id"] = "model-other"
            with self.subTest(location=location):
                self.assertFalse(cli._reading_request_complete(bad))

    def make_desk(self):
        desk = desk_module.ReadingDesk(self.source, idle_timeout=600)
        self.addCleanup(desk.close)
        return desk

    def ask_desk(self, desk, *, parent=None, question="What does answer return?"):
        payload = {"question": question, "version": desk.project["version"]}
        if parent:
            payload["parent"] = parent
        else:
            payload["focus"] = [{"file": "0", "start": 3, "end": 4}]
        desk.start(payload, kind="continue" if parent else "explain")
        desk.worker.join(5)
        self.assertFalse(desk.worker.is_alive())
        return desk.status()

    def test_desk_history_and_exact_source_continuation_work_while_model_is_resident(self):
        desk = self.make_desk()
        first = self.ask_desk(desk)
        self.assertEqual(first["status"], "answered", first)
        self.assertEqual(first["gpu"], "resident")
        self.assertTrue(desk._job_complete(first))
        self.assertEqual(first["reading_scope"]["focus"], ["app.py:3-4"])
        second = self.ask_desk(desk, parent=first["id"], question="Which literal is returned?")
        self.assertEqual(second["status"], "answered", second)
        self.assertEqual(second["request_completion"], first["request_completion"])
        self.assertEqual(second["continuation"]["parent_id"], first["id"])
        self.assertEqual(len(self.supervisors), 1)
        self.assertEqual(len(desk.history()["entries"]), 2)
        self.assertNotIn(first["question"], self.calls[-1].messages[1].content)
        self.assertTrue(self.calls[-1].messages[1].content.endswith("Which literal is returned?"))
        desk.release_model({"session_id": desk.model.status()["id"]})
        self.assertTrue(desk._job_complete(desk.history()["entries"][0]))
        self.assertEqual(desk.model.status()["state"], "unloaded")

    def test_desk_rejects_mismatched_request_evidence_without_scope_or_history_answer(self):
        _, result, _ = self.run_cli()
        bad = copy.deepcopy(result)
        bad["outcome"]["request_completion"]["server_session_id"] = "model-other"
        desk = self.make_desk()

        def wrong_result(_args, **hooks):
            hooks["on_result"](bad)

        with patch.object(desk_module, "_run_explain_cli", side_effect=wrong_result):
            job = self.ask_desk(desk)
        self.assertEqual(job["status"], "incomplete")
        self.assertFalse(job["result"]["ok"])
        self.assertIsNone(job["result"]["outcome"]["answer"])
        self.assertNotIn("reading_scope", job)
        self.assertNotIn("result", desk.history()["entries"][0])

    def test_desk_cancel_after_result_before_worker_publication_closes_idle_generation(self):
        desk = self.make_desk()
        result_ready, allow_return = threading.Event(), threading.Event()
        real_runner = desk_module._run_explain_cli

        def pause_after_result(args, **hooks):
            code = real_runner(args, **hooks)
            result_ready.set()  # CLI result exists, but the desk has not published it.
            if not allow_return.wait(3):
                raise RuntimeError("test did not allow runner return")
            return code

        payload = {"question": "What does answer return?", "version": desk.project["version"],
            "focus": [{"file": "0", "start": 3, "end": 4}]}
        with patch.object(desk_module, "_run_explain_cli", side_effect=pause_after_result):
            identifier = desk.start(payload)["id"]
            try:
                self.assertTrue(result_ready.wait(3))
                self.assertNotIn("result", desk.status())
                self.assertEqual(desk.model.status()["state"], "idle")
                self.assertEqual(self.supervisors[0].stop_calls, 0)
                self.assertEqual(desk.cancel(identifier)["status"], "cancelling")
            finally:
                allow_return.set()
                desk.worker.join(5)
        self.assertFalse(desk.worker.is_alive())
        job = desk.status()
        self.assertEqual(job["status"], "cancelled", job)
        self.assertFalse(job.get("result", {}).get("ok", False))
        outcome = job.get("result", {}).get("outcome")
        if isinstance(outcome, dict):
            self.assertIsNone(outcome.get("answer"))
        self.assertNotIn("reading_scope", job)
        self.assertNotIn("preview", job)
        self.assertEqual(desk.model.status()["state"], "unloaded")
        self.assertEqual(self.supervisors[0].stop_calls, 1)
        self.assertTrue(desk.model.status()["receipts"][-1]["ok"])
        self.assertNotIn("result", desk.history()["entries"][-1])

    def test_desk_release_reservation_blocks_new_job_before_owner_release_begins(self):
        desk = self.make_desk()
        first = self.ask_desk(desk)
        self.assertEqual(first["status"], "answered", first)
        identifier = desk.model.status()["id"]
        release_entered, allow_release = threading.Event(), threading.Event()
        real_release = desk.model.release
        failures = []

        def delayed_release(expected_id):
            release_entered.set()  # Desk flag is reserved; manager is still idle.
            if not allow_release.wait(3):
                raise RuntimeError("test did not allow model release")
            return real_release(expected_id)

        def release():
            try:
                desk.release_model({"session_id": identifier})
            except BaseException as exc:
                failures.append(type(exc).__name__)

        payload = {"question": "Which literal is returned?", "version": desk.project["version"],
            "focus": [{"file": "0", "start": 3, "end": 4}]}
        with patch.object(desk.model, "release", side_effect=delayed_release):
            worker = threading.Thread(target=release)
            worker.start()
            try:
                self.assertTrue(release_entered.wait(3))
                self.assertTrue(desk.model_release_pending)
                self.assertEqual(desk.model.status()["state"], "idle")
                with self.assertRaises(ValueError):
                    desk.start(payload)
                with self.assertRaises(ValueError):
                    desk.refresh()
                self.assertEqual(desk.status()["id"], first["id"])
                self.assertEqual(len(self.calls), 1)
            finally:
                allow_release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertFalse(desk.model_release_pending)
        self.assertEqual(desk.model.status()["state"], "unloaded")
        self.assertEqual(self.supervisors[0].stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
