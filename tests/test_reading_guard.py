"""Owned source fixtures and fake inference exercise the real publication path."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge8 import cli, desk
from forge8.inference import ChatResponse
from forge8.operator import AcceptanceGateResult
from forge8.server import ServerPreparation
from test_structured_reading import answer_document, citation


class ReadingGuardTests(unittest.TestCase):
    def run_reading(self, guard=None, *, shutdown_ok=True, expected_snapshot=None,
                    source_files=None, focus=("app.py:3-4",), question="What does answer return?",
                    reply=None):
        reply = json.dumps(answer_document("It returns 7.")) if reply is None else reply
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        source, assets, state = root / "source", root / "assets", root / "state"
        source.mkdir()
        assets.mkdir()
        # This file is snapshotted and parsed only, never imported or executed.
        originals = source_files if source_files is not None else {"app.py":
            'raise RuntimeError("never execute this source")\n\ndef answer():\n    return 7\n'}
        for path, content in originals.items():
            target = source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        calls, supervisors, transports, results, requests = [], [], [], [], []

        class Supervisor:
            def __init__(self, plan, **kwargs):
                self.api_key = None
                self.start_result = self.shutdown_result = None
                self.exited = False
                supervisors.append(self)

            def __enter__(self):
                self.api_key = "owned-fake-key"
                self.start_result = SimpleNamespace(ok=True, status="ready", error=None,
                    health_last_error=None, as_dict=lambda: {"ok": True, "status": "ready"})
                calls.append("start")
                return self

            def stop(self):
                calls.append("stop")
                self.api_key = None
                if self.shutdown_result is None:
                    payload = {"ok": shutdown_ok,
                        "status": "terminated" if shutdown_ok else "shutdown_error",
                        "return_code": 0 if shutdown_ok else None}
                    self.shutdown_result = SimpleNamespace(**payload, as_dict=lambda: dict(payload))
                return self.shutdown_result

            def __exit__(self, *_args):
                self.exited = True
                self.stop()

        class Transport:
            def __init__(self, endpoint, *, api_key, **kwargs):
                self.api_key = api_key
                transports.append(self)

            def chat(self, request):
                calls.append("chat")
                requests.append(request)
                return ChatResponse(reply, "stop", {}, {})

        def captured_guard():
            calls.append("guard")
            return guard()

        plan = SimpleNamespace(endpoint="http://127.0.0.1:18080", model_manifest_path=assets / "model.json",
            as_dict=lambda: {"endpoint": "http://127.0.0.1:18080"})
        args = cli.build_parser().parse_args(["explain", str(source), "--question", question,
            *[value for selector in focus for value in ("--focus", selector)], "--reader", "qwen35", "--json"])
        with (patch.object(cli, "_load_deployment", return_value=None),
              patch.dict(os.environ, {"FORGE8_STATE_HOME": str(state)}),
              patch.object(cli, "_resolve_fix_asset_root", return_value=assets),
              patch.object(cli, "_fix_model_id", return_value="owned-fake-model"),
              patch.object(cli, "prepare_server", return_value=ServerPreparation("ready", (), plan=plan)) as prepare,
              patch.object(cli, "LocalServerSupervisor", Supervisor),
              patch.object(cli, "OpenAITransport", Transport)):
            code = cli._run_explain_cli(args, on_result=results.append, on_progress=lambda _: None,
                expected_snapshot=expected_snapshot,
                **({"source_guard": captured_guard} if guard is not None else {}))
        for path, content in originals.items():
            self.assertEqual((source / path).read_text(encoding="utf-8"), content)
        self.assertEqual(len(results), 1)
        return SimpleNamespace(code=code, result=results[0], calls=calls, supervisors=supervisors,
            transports=transports, requests=requests, prepare_calls=prepare.call_count, root=root)

    def assert_no_accepted_publication(self, run):
        self.assertEqual(run.code, 2)
        self.assertFalse(run.result["ok"])
        outcome = run.result["outcome"]
        self.assertEqual(outcome["status"], "acceptance_gate_failed")
        self.assertIsNone(outcome["answer"])
        self.assertNotIn("unverified_prose", outcome)
        root = Path(run.result["run_root"])
        sealed = json.loads((root / "explanation.json").read_text(encoding="utf-8"))
        self.assertFalse(sealed["answered"])
        self.assertIsNone(sealed["answer"])
        self.assertNotIn("unverified_prose", sealed)
        text = (root / "ANSWER.txt").read_text(encoding="utf-8")
        self.assertIn("INCOMPLETE", text)
        self.assertNotIn("It returns 7.", text)
        self.assertTrue(run.supervisors[0].exited)
        self.assertIsNone(run.supervisors[0].api_key)
        self.assertIsNone(run.transports[0].api_key)

    def test_default_explain_wrapper_omits_guard_keyword(self):
        args = SimpleNamespace(repo=Path("unused"))
        with patch.object(cli, "_run_reading_cli", return_value=17) as shared:
            self.assertEqual(cli._run_explain_cli(args), 17)
        self.assertNotIn("source_guard", shared.call_args.kwargs)

    def test_explain_wrapper_forwards_same_callback_without_calling_it(self):
        guard = Mock()
        with patch.object(cli, "_run_reading_cli", return_value=17) as shared:
            cli._run_explain_cli(SimpleNamespace(repo=Path("unused")), source_guard=guard)
        self.assertIs(shared.call_args.kwargs["source_guard"], guard)
        guard.assert_not_called()

    def test_guard_rejected_for_non_explain_before_source_or_assets(self):
        for kind in ("locate", "project"):
            with self.subTest(kind=kind):
                guard, results = Mock(), []
                with (patch.object(cli, "_resolve_fix_asset_root") as assets,
                      patch.object(cli, "prepare_explanation") as snapshot,
                      patch.object(cli, "prepare_server") as prepare):
                    code = cli._run_reading_cli(SimpleNamespace(repo=Path("unused"), question="Why?"),
                        kind=kind, source_guard=guard, on_result=results.append)
                self.assertEqual(code, 2)
                guard.assert_not_called()
                assets.assert_not_called()
                snapshot.assert_not_called()
                prepare.assert_not_called()

    def test_early_rejection_precedes_hashing_and_loading(self):
        guard = Mock(return_value=AcceptanceGateResult(False, "owned original drift", {"unchanged": False}))
        run = self.run_reading(guard)
        self.assertEqual((run.code, run.result["status"]), (2, "source_guard_failed"))
        self.assertEqual(run.prepare_calls, 0)
        self.assertEqual(run.supervisors, [])
        self.assertEqual(run.transports, [])
        self.assertEqual(run.calls, ["guard"])
        self.assertIsNone(run.result["outcome"])

    def test_early_exception_and_malformed_results_fail_without_private_text(self):
        invalid = AcceptanceGateResult(True, None, {})
        object.__setattr__(invalid, "ok", 1)  # Deliberately broken owned callback result.
        cases = (Mock(side_effect=RuntimeError("PRIVATE-GUARD-DETAIL")), Mock(return_value=None),
            Mock(return_value={"ok": True}), Mock(return_value=invalid))
        for guard in cases:
            with self.subTest(guard=guard):
                run = self.run_reading(guard)
                self.assertEqual(run.result["status"], "source_guard_failed")
                self.assertEqual(run.prepare_calls, 0)
                self.assertNotIn("PRIVATE-GUARD-DETAIL", json.dumps(run.result))

    def test_snapshot_mismatch_precedes_guard(self):
        guard = Mock(return_value=AcceptanceGateResult(True, None, {}))
        run = self.run_reading(guard, expected_snapshot="0" * 64)
        self.assertEqual(run.code, 2)
        self.assertEqual(run.prepare_calls, 0)
        guard.assert_not_called()

    def test_final_rejection_is_sealed_before_any_accepted_answer(self):
        guard = Mock(side_effect=[AcceptanceGateResult(True, None, {"stage": "before"}),
            AcceptanceGateResult(False, "original source changed", {"stage": "after", "unchanged": False})])
        run = self.run_reading(guard)
        self.assert_no_accepted_publication(run)
        self.assertEqual(run.calls[:5], ["guard", "start", "chat", "stop", "guard"])
        self.assertEqual(run.prepare_calls, 1)
        self.assertEqual(guard.call_count, 2)
        gate = run.result["outcome"]["acceptance"]
        self.assertFalse(gate["evidence"]["original_source"]["ok"])
        self.assertTrue(gate["evidence"]["lifecycle"]["supervisor_secret_cleared"])
        self.assertEqual(gate["evidence"]["server_logs"], [])

    def test_success_preserves_lifecycle_and_original_source_evidence(self):
        guard = Mock(return_value=AcceptanceGateResult(True, None, {"head": "owned-head", "unchanged": True}))
        run = self.run_reading(guard)
        self.assertEqual(run.code, 0, run.result)
        self.assertTrue(run.result["ok"])
        gate = run.result["outcome"]["acceptance"]
        self.assertEqual(gate["evidence"]["original_source"], {"ok": True, "reason": None,
            "evidence": {"head": "owned-head", "unchanged": True}})
        self.assertTrue(gate["evidence"]["lifecycle"]["transport_secret_cleared"])
        self.assertTrue(gate["evidence"]["return_code_observed"])
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(run.calls.count("chat"), 1)

    def test_final_guard_exception_cannot_bypass_cleanup_or_leak(self):
        for error in (RuntimeError("PRIVATE-GUARD-DETAIL"), KeyboardInterrupt("PRIVATE-GUARD-DETAIL")):
            with self.subTest(error=type(error).__name__):
                guard = Mock(side_effect=[AcceptanceGateResult(True, None, {}), error])
                run = self.run_reading(guard)
                self.assert_no_accepted_publication(run)
                gate = run.result["outcome"]["acceptance"]
                self.assertEqual(gate["evidence"]["original_source"], {"ok": False,
                    "reason": "original source verification failed", "evidence": {}})
                for file in Path(run.result["run_root"]).rglob("*"):
                    if file.is_file():
                        self.assertNotIn(b"PRIVATE-GUARD-DETAIL", file.read_bytes())
                self.assertNotIn("PRIVATE-GUARD-DETAIL", json.dumps(run.result))

    def test_passing_original_guard_cannot_override_failed_lifecycle(self):
        guard = Mock(return_value=AcceptanceGateResult(True, None, {"unchanged": True}))
        run = self.run_reading(guard, shutdown_ok=False)
        self.assert_no_accepted_publication(run)
        gate = run.result["outcome"]["acceptance"]
        self.assertFalse(gate["ok"])
        self.assertTrue(gate["evidence"]["original_source"]["ok"])
        self.assertFalse(gate["evidence"]["return_code_observed"])

    def test_final_malformed_or_nonbool_guard_result_cannot_publish(self):
        invalid = AcceptanceGateResult(True, None, {})
        object.__setattr__(invalid, "ok", 1)
        for result in (None, {"ok": True}, invalid):
            with self.subTest(result=result):
                guard = Mock(side_effect=[AcceptanceGateResult(True, None, {}), result])
                run = self.run_reading(guard)
                self.assert_no_accepted_publication(run)
                self.assertEqual(run.result["outcome"]["acceptance"]["evidence"]["original_source"],
                    {"ok": False, "reason": "original source verification failed", "evidence": {}})

    def test_default_execution_does_not_add_guard_evidence(self):
        run = self.run_reading()
        self.assertEqual(run.code, 0, run.result)
        self.assertNotIn("original_source", run.result["outcome"]["acceptance"]["evidence"])
        self.assertNotIn("guard", run.calls)

    def test_contained_caller_role_survives_normal_evidence_reuse(self):
        before = 'raise RuntimeError("never execute this source")\n\ndef answer():\n    return 6\n\ndef show():\n    return str(answer())\n'
        after = before.replace("return 6", "return 7")
        selectors = ["before/app.py:1-7", "after/app.py:1-7", "after/app.py:6-7"]
        question = desk._comparison_question("What changes for this fixed caller?", selectors)
        run = self.run_reading(Mock(return_value=AcceptanceGateResult(True, None, {})),
            source_files={"before/app.py": before, "after/app.py": after}, focus=selectors,
            question=question, reply=json.dumps(answer_document("The selected caller invokes answer.",
                [citation("E2", 6, 7)])))
        self.assertEqual(run.code, 0, run.result)
        self.assertEqual(len(run.requests), 1)
        user = [message.content for message in run.requests[0].messages if message.role == "user"]
        self.assertEqual(len(user), 1)
        context = user[0]
        self.assertIn("[E1] before/app.py L1-L7", context)
        self.assertIn("[E2] after/app.py L1-L7", context)
        self.assertNotIn("[E3]", context)
        self.assertIn("BEFORE: before/app.py:1-7", context)
        self.assertIn("AFTER: after/app.py:1-7", context)
        self.assertIn("FIXED CALLER: after/app.py:6-7", context)
        self.assertTrue(context.endswith(question))
        self.assertEqual(run.result["outcome"]["evidence_count"], 2)


if __name__ == "__main__":
    unittest.main()
