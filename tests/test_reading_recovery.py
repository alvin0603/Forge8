"""Clean abstention recovery; owned fixtures and mocked inference only."""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from forge8 import cli, desk as desk_module
import test_cli as cli_fixture
import test_desk_continuation as continuation_fixture
import test_resident_reading as resident_fixture
from test_structured_reading import answer_document


_REASON = "需要確認 helper 的實作。這是模型判斷，不代表來源確實不足。"
_INSUFFICIENT_WIRE = json.dumps({"kind": "insufficient", "reason": _REASON})


def insufficient(value):
    value = deepcopy(value)
    value.update(ok=False, status="insufficient_evidence")
    value["outcome"].update(ok=False, status="insufficient_evidence", answer=None,
        failure_reason=_REASON)
    value["outcome"].pop("unverified_prose", None)
    return value


class RecoveryProjectCLITests(unittest.TestCase):
    # Reuse the fixture helper, not its independent test methods.
    run_question = cli_fixture.ProjectQuestionCLITests.run_question

    def test_clean_project_abstention_keeps_actual_second_stage_source_and_exit_failure(self):
        run = self.run_question(answer=_INSUFFICIENT_WIRE)
        result, = run.results
        self.assertEqual(run.code, 1)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["outcome"]["answer"])
        self.assertEqual(cli._insufficient_explain_reason(result), _REASON)
        self.assertIsNone(cli._unverified_explain_prose(result))
        self.assertEqual([len(transport.requests) for transport in run.transports], [1, 1])
        self.assertEqual(cli._explain_gpu_state(result["server"]), "released")
        project = result["project_reading"]
        self.assertIs(project["answer_attempted"], True)
        self.assertEqual(project["focus"], [{"path": "app.py", "start_line": 3, "end_line": 4}])
        ingress = json.loads((Path(result["run_root"]) / "input/ingress.json").read_bytes())
        self.assertEqual(ingress["focus"], ["app.py:3-4"])
        self.assertEqual(ingress["focus_origin"], "project_candidates")
        self.assertEqual((run.source / "app.py").read_text(encoding="utf-8"), run.content)
        self.assertTrue(all(transport.api_key is None for transport in run.transports))

    def test_truncated_or_malformed_abstention_never_becomes_clean_recovery(self):
        invalid = [(_INSUFFICIENT_WIRE, "length"), (_INSUFFICIENT_WIRE[:-1], "stop")]
        invalid.extend((json.dumps({"kind": "insufficient", "reason": reason}), "stop")
            for reason in ("", "x" * 1001, "hidden\u202econtrol"))
        for text, finish in invalid:
            with self.subTest(finish=finish, text=text[:40]):
                run = self.run_question(answer=text, finish=finish)
                result, = run.results
                self.assertNotEqual(result["status"], "insufficient_evidence")
                self.assertIsNone(cli._insufficient_explain_reason(result))
                self.assertFalse(result["ok"])
                self.assertIsNone(result["outcome"]["answer"])

    def test_project_cancellation_drift_and_cleanup_do_not_gain_recovery_metadata(self):
        phase = "Project question: answering from the automatically selected source."
        for fault in ("cancel", "source", "cleanup"):
            with self.subTest(fault=fault):
                run = self.run_question(answer=_INSUFFICIENT_WIRE,
                    fault=fault, phase=phase if fault != "cleanup" else None)
                result, = run.results
                self.assertIsNone(cli._insufficient_explain_reason(result))
                self.assertNotIn("project_reading", result)


class RecoveryDeskTests(continuation_fixture._ContinuationFixture):
    def recovery_result(self):
        return insufficient(self.result())

    def test_manual_abstention_keeps_exact_windows_and_continues_with_new_question_only(self):
        parent, _ = self.finish(value=self.recovery_result())
        expected = {"focus": ["main.py:3-9", "main.py:6-12"],
            "origin": "user_focus", "supplements": []}
        self.assertEqual(parent["status"], "incomplete")
        self.assertEqual(parent["reading_scope"], expected)
        self.assertIsNone(parent["result"]["outcome"]["answer"])
        saved, = self.desk.history()["entries"]
        self.assertEqual(saved["reading_scope"], expected)
        self.assertEqual(saved["result"]["outcome"]["failure_reason"], _REASON)
        question = "New self-contained question about VALUE_6."
        with patch.object(desk_module, "_run_project_cli") as project, \
                patch.object(desk_module, "_run_locate_cli") as locate:
            child, captured = self.finish(self.followup(parent["id"], question), kind="continue")
        project.assert_not_called()
        locate.assert_not_called()
        self.assertEqual(captured["args"]["focus"], expected["focus"])
        self.assertEqual(captured["args"]["question"], question)
        self.assertNotIn(parent["question"], str(captured))
        self.assertNotIn(_REASON, str(captured))
        self.assertEqual(child["reading_scope"], expected)
        self.assertEqual(child["continuation"]["parent_id"], parent["id"])

    def test_project_abstention_keeps_six_actual_windows_and_existing_supplements(self):
        value = insufficient(self.project_result())
        project = value["project_reading"]
        value["outcome"]["coverage"]["observed"]["ranges"] = [{"path": "main.py",
            "ranges": [{"start_line": span["start_line"], "end_line": span["end_line"]}
                for span in project["focus"]]}]
        parent, _ = self.finish({"question": "Original project question", "version": self.desk.project["version"]},
            kind="project", value=value)
        self.assertEqual(parent["result"]["project_reading"], project)
        expected = {"origin": "project_candidates", "supplements": ["main.py:2-3"],
            "focus": [f"main.py:{span['start_line']}-{span['end_line']}" for span in project["focus"]]}
        self.assertEqual(parent["reading_scope"], expected)
        child, captured = self.finish(self.followup(parent["id"]), kind="continue")
        self.assertEqual(captured["args"]["focus"], expected["focus"])
        self.assertEqual((captured["origin"], captured["supplements"]),
            ("project_candidates", ("main.py:2-3",)))
        self.assertEqual(child["reading_scope"], expected)
        self.assertNotIn(_REASON, str(captured))

    def test_reason_is_bounded_ordinary_text_not_evidence_of_a_missing_definition(self):
        for reason, allowed in ((_REASON, True), ("  未提供某個設定\n\t請確認。  ", True),
                ("<script>not executable</script>", True), ("x" * 1000, True),
                (None, False), (False, False), (" ", False), ("x" * 1001, False),
                ("bad\x00control", False), ("bad\u202econtrol", False), ("bad\ud800", False)):
            with self.subTest(reason=repr(reason)[:50]):
                value = self.recovery_result()
                value["outcome"]["failure_reason"] = reason
                self.assertEqual(cli._insufficient_explain_reason(value), reason if allowed else None)
                job, _ = self.finish(value=value)
                self.assertEqual("reading_scope" in job, allowed)
                self.assertFalse(job["result"]["ok"])
                self.assertIsNone(job["result"]["outcome"]["answer"])

    def test_status_null_answer_and_strict_postchecks_cannot_be_spoofed(self):
        changes = [("status", "stalled"), ("ok", True), ("error", "owned error"),
            ("kind", "forge8.locate"), ("outcome.status", "source_drift"),
            ("outcome.ok", 0), ("outcome.answer", {}),
            ("outcome.source_unchanged", 1), ("outcome.snapshot_unchanged", False),
            ("outcome.acceptance.ok", False), ("server.shutdown.return_code", None),
            ("server.supervisor_secret_cleared", False), ("server.transport_secret_cleared", False)]
        for field, replacement in changes:
            with self.subTest(field=field):
                value = self.recovery_result()
                container = value
                *parents, name = field.split(".")
                for part in parents:
                    container = container[part]
                container[name] = replacement
                self.assertIsNone(cli._insufficient_explain_reason(value))
                job, _ = self.finish(value=value)
                self.assertNotIn("reading_scope", job)
                self.assertNotIn("reading_scope", self.desk.history()["entries"][-1])
                with self.assertRaises(ValueError):
                    self.desk.start(self.followup(job["id"]), kind="continue")
        value = self.recovery_result()
        value["outcome"].pop("answer")
        self.assertIsNone(cli._insufficient_explain_reason(value))

    def test_invalid_project_abstention_cannot_keep_candidate_recovery(self):
        for fault in ("reason", "status", "discovery", "version"):
            with self.subTest(fault=fault):
                value = insufficient(self.project_result())
                if fault == "reason": value["outcome"]["failure_reason"] = " "
                if fault == "status": value["outcome"]["status"] = "stalled"
                if fault == "discovery": value["project_reading"]["discovery"]["ingress_unchanged"] = False
                if fault == "version": value["project_reading"]["discovery"]["snapshot_sha256"] = "different"
                job, _ = self.finish({"question": "Project question", "version": self.desk.project["version"]},
                    kind="project", value=value)
                self.assertNotIn("project_reading", job["result"])
                self.assertNotIn("reading_scope", job)

    def test_cancellation_or_runner_exception_after_result_drops_recovery_and_history_payload(self):
        for cancel, error in ((True, False), (False, True)):
            with self.subTest(cancel=cancel):
                job, _ = self.finish(value=self.recovery_result(), cancel=cancel, error=error,
                    preview="INSUFFICIENT: unfinished streamed excerpt")
                self.assertNotIn("reading_scope", job)
                self.assertNotIn("preview", job)
                self.assertNotIn("rejected_preview", job)
                saved = self.desk.history()["entries"][-1]
                self.assertNotIn("result", saved)
                self.assertNotIn("reading_scope", saved)
                with self.assertRaises(ValueError):
                    self.desk.start(self.followup(job["id"]), kind="continue")

    def test_continuation_still_refuses_comparison_mode_and_checks_live_source_before_gpu(self):
        parent, _ = self.finish(value=self.recovery_result())
        with patch.object(self.desk, "comparison", object()), \
                patch.object(desk_module, "_run_explain_cli") as runner:
            with self.assertRaisesRegex(ValueError, "comparison"):
                self.desk.start(self.followup(parent["id"]), kind="continue")
            runner.assert_not_called()
        (self.source / "main.py").write_bytes(self.code.replace("VALUE_1 = 1", "VALUE_1 = 9").encode())
        with patch.object(cli, "prepare_server") as server:
            self.desk.start(self.followup(parent["id"]), kind="continue")
            self.join()
        server.assert_not_called()
        self.assertIn("source changed since browsing", self.desk.status()["result"]["error"])
        self.assertNotIn("reading_scope", self.desk.status())

    def test_recovery_history_is_immutable_and_refresh_does_not_replay_it(self):
        value = self.recovery_result()
        parent, _ = self.finish(value=value)
        saved = deepcopy(self.desk.history())
        value["outcome"]["failure_reason"] = "changed live result"
        parent["reading_scope"]["focus"].clear()
        self.assertEqual(self.desk.history(), saved)
        self.desk.refresh()
        self.assertEqual(self.desk.history()["entries"], [])
        with patch.object(desk_module, "_run_explain_cli") as runner, self.assertRaises(ValueError):
            self.desk.start(self.followup(parent["id"]), kind="continue")
        runner.assert_not_called()


class RecoveryResidentTests(unittest.TestCase):
    setUp = resident_fixture.ResidentReadingTests.setUp
    patch = resident_fixture.ResidentReadingTests.patch
    start_patch = resident_fixture.ResidentReadingTests.start_patch
    cleanup_owner = resident_fixture.ResidentReadingTests.cleanup_owner
    run_cli = resident_fixture.ResidentReadingTests.run_cli
    make_desk = resident_fixture.ResidentReadingTests.make_desk
    ask_desk = resident_fixture.ResidentReadingTests.ask_desk

    def test_resident_project_abstention_registers_both_artifacts_without_fake_shutdown(self):
        self.outputs = ['{"candidates":["D0001"]}', _INSUFFICIENT_WIRE]
        code, result, _ = self.run_cli("project")
        self.assertEqual(code, 1)
        self.assertEqual(cli._insufficient_explain_reason(result), _REASON)
        self.assertTrue(result["project_reading"]["answer_attempted"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.supervisors), 1)
        self.assertEqual(len(self.owner._generation["requests"]), 2)
        self.assertEqual(self.owner.status()["state"], "idle")
        self.assertIsNone(self.supervisors[0].shutdown_result)
        manifest = json.loads((Path(result["run_root"]) / "manifest.json").read_bytes())
        self.assertIs(manifest["ok"], False)
        self.assertIs(manifest["request_ok"], False)
        self.assertEqual(manifest["session_finalization"], "pending")

    def test_resident_manual_recovery_continues_same_generation_without_prior_reason(self):
        self.outputs = [_INSUFFICIENT_WIRE, json.dumps(answer_document("It returns 7."))]
        desk = self.make_desk()
        parent = self.ask_desk(desk)
        self.assertEqual((parent["status"], parent["gpu"]), ("incomplete", "resident"))
        self.assertTrue(desk._job_complete(parent))
        self.assertEqual(parent["reading_scope"]["focus"], ["app.py:3-4"])
        child = self.ask_desk(desk, parent=parent["id"], question="Which literal is returned?")
        self.assertEqual(child["status"], "answered")
        self.assertEqual(len(self.supervisors), 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(child["request_completion"], parent["request_completion"])
        self.assertNotIn(_REASON, self.calls[-1].messages[1].content)
        self.assertNotIn(parent["question"], self.calls[-1].messages[1].content)
        self.assertEqual(desk.history()["entries"][0]["reading_scope"], parent["reading_scope"])

    def test_mismatched_resident_completion_cannot_recover(self):
        self.outputs = [_INSUFFICIENT_WIRE]
        _, original, _ = self.run_cli()
        result = deepcopy(original)
        result["outcome"]["request_completion"]["server_session_id"] = "model-other"
        self.assertIsNone(cli._insufficient_explain_reason(result))
        desk = self.make_desk()
        with patch.object(desk_module, "_run_explain_cli", side_effect=lambda _args, **hooks: hooks["on_result"](result)):
            job = self.ask_desk(desk)
        self.assertNotIn("reading_scope", job)
        self.assertNotIn("result", desk.history()["entries"][-1])
        with self.assertRaises(ValueError):
            desk.start({"parent": job["id"], "question": "New question", "version": job["version"]}, kind="continue")

    def test_resident_cancel_after_abstention_result_reclaims_owned_generation_before_publication(self):
        self.outputs = [_INSUFFICIENT_WIRE]
        desk = self.make_desk()
        runner = desk_module._run_explain_cli
        def cancel_after_result(args, **hooks):
            code = runner(args, **hooks)
            self.assertEqual(desk.model.status()["state"], "idle")
            hooks["cancel_event"].set()
            return code
        with patch.object(desk_module, "_run_explain_cli", side_effect=cancel_after_result):
            job = self.ask_desk(desk)
        self.assertEqual((job["status"], job["gpu"]), ("cancelled", "released"))
        self.assertEqual(desk.model.status()["state"], "unloaded")
        self.assertTrue(desk.model.status()["receipts"][-1]["ok"])
        self.assertEqual(self.supervisors[0].stop_calls, 1)
        self.assertNotIn("reading_scope", job)
        self.assertNotIn("result", desk.history()["entries"][-1])


if __name__ == "__main__":
    unittest.main()
