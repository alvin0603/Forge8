"""Exact-source continuation, with owned source and mocked inference only."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from forge8 import cli, comparison, desk as desk_module
from forge8.git_source import GitBaseline
import test_desk as fixtures
import test_desk_changes as change_fixtures


class _ContinuationFixture(fixtures.DeskFixture):
    def setUp(self):
        super().setUp()
        self.code = "".join(f"VALUE_{index} = {index}\n" for index in range(1, 301))
        (self.source / "main.py").write_bytes(self.code.encode())
        self.desk.refresh()
        spawn = patch("subprocess.Popen", side_effect=AssertionError("no native process allowed"))
        spawn.start()
        self.addCleanup(spawn.stop)

    def question(self, text="Original self-contained question", ranges=((3, 9), (6, 12))):
        return {"question": text, "version": self.desk.project["version"],
            "focus": [{"file": "0", "start": first, "end": last} for first, last in ranges]}

    def followup(self, parent, question="New self-contained question"):
        return {"parent": parent, "question": question, "version": self.desk.project["version"]}

    def result(self, *, unverified=False):
        value = fixtures.ReadingDeskTests.rejected_result(self)
        value["kind"] = "forge8.explain"
        if unverified:
            value["outcome"]["unverified_prose"] = "Complete ordinary text; not an accepted answer."
        else:
            value.update(ok=True, status="answered")
            value["outcome"].update(ok=True, status="answered", answer={"claims": [{"text": "OLD PROSE NEVER SEND"}]})
        # This merged view must never reconstruct the original overlapping windows.
        value["outcome"]["coverage"] = {"observed": {"ranges": [{"path": "main.py",
            "ranges": [{"start_line": 3, "end_line": 12}]}]}}
        return value

    def project_result(self, count=6):
        value = self.result()
        focus = [{"path": "main.py", "start_line": first, "end_line": first + 4}
            for first in range(1, count * 20, 20)]
        value["project_reading"] = {"answer_attempted": True, "focus": focus,
            "context": {"added": [{"path": "main.py", "start_line": 2, "end_line": 3}], "skipped": {}},
            "discovery": {"ok": True, "status": "located", "snapshot_sha256": self.desk.project["version"],
                "source_unchanged": True, "snapshot_unchanged": True, "ingress_unchanged": True,
                "acceptance": {"ok": True}, "candidates": []}}
        return value

    def join(self):
        self.desk.worker.join(5)
        self.assertFalse(self.desk.worker.is_alive(), "mock reading job did not finish")

    def finish(self, payload=None, *, kind="explain", value=None, cancel=False, error=False, preview=None):
        captured = {}
        result = self.result() if value is None else value

        def run(args, **hooks):
            captured["args"] = deepcopy(vars(args))
            captured["origin"] = hooks.get("focus_origin", "user_focus")
            captured["supplements"] = hooks.get("context_supplements", ())
            captured["version"] = hooks["expected_snapshot"]
            if preview is not None:
                hooks["on_text"](preview)
            hooks["on_result"](result)
            if cancel:
                hooks["cancel_event"].set()
            if error:
                raise RuntimeError("owned failure")

        runner = "_run_project_cli" if kind == "project" else "_run_locate_cli" if kind == "locate" else "_run_explain_cli"
        with patch.object(desk_module, runner, side_effect=run) as model:
            self.desk.start(self.question() if payload is None else payload, kind=kind)
            self.join()
        model.assert_called_once()
        return self.desk.status(), captured


class ContinuationDeskTests(_ContinuationFixture):
    def test_manual_original_windows_and_new_question_only_survive_into_history(self):
        parent, _ = self.finish()
        expected = {"focus": ["main.py:3-9", "main.py:6-12"], "origin": "user_focus", "supplements": []}
        self.assertEqual(parent["reading_scope"], expected)
        self.assertEqual(self.desk.history()["entries"][0]["reading_scope"], expected)
        text = "  新問題：VALUE_6 在原碼如何定義？\n\t不要帶入舊題。  "
        with patch.object(desk_module, "_run_project_cli") as project, patch.object(desk_module, "_run_locate_cli") as locate:
            child, captured = self.finish(self.followup(parent["id"], text), kind="continue")
        project.assert_not_called(); locate.assert_not_called()
        self.assertEqual(captured["args"]["question"], text)
        self.assertEqual(captured["args"]["focus"], expected["focus"])
        self.assertEqual((captured["origin"], captured["supplements"]), ("user_focus", ()))
        self.assertEqual(captured["version"], parent["version"])
        self.assertEqual(set(captured["args"]), {"repo", "question", "focus", "reader", "as_json"})
        self.assertNotIn(parent["question"], str(captured))
        self.assertNotIn("OLD PROSE NEVER SEND", str(captured))
        self.assertEqual(child["kind"], "continue")
        self.assertEqual(child["continuation"], {"parent_id": parent["id"], "parent_question": parent["question"]})
        self.assertEqual(child["reading_scope"], expected)
        saved = self.desk.history()["entries"][-1]
        self.assertEqual(saved["continuation"], child["continuation"])
        self.assertEqual(saved["reading_scope"], expected)
        child["reading_scope"]["focus"].clear()
        self.assertEqual(self.desk.history()["entries"][-1]["reading_scope"], expected)

    def test_five_and_six_project_windows_keep_original_supplements_without_discovery(self):
        for count in (5, 6):
            with self.subTest(count=count):
                value = self.project_result(count)
                parent, _ = self.finish({"question": "Locate the owned definitions", "version": self.desk.project["version"]},
                    kind="project", value=value)
                expected = {"focus": [f"main.py:{first}-{first + 4}" for first in range(1, count * 20, 20)],
                    "origin": "project_candidates", "supplements": ["main.py:2-3"]}
                self.assertEqual(parent["reading_scope"], expected)
                with patch.object(desk_module, "_run_project_cli") as project, patch.object(desk_module, "_run_locate_cli") as locate:
                    child, captured = self.finish(self.followup(parent["id"]), kind="continue")
                project.assert_not_called(); locate.assert_not_called()
                self.assertEqual(captured["args"]["focus"], expected["focus"])
                self.assertEqual((captured["origin"], captured["supplements"]), ("project_candidates", ("main.py:2-3",)))
                self.assertEqual(child["reading_scope"], expected)
                self.assertNotIn("project_reading", child["result"])

    def test_viewing_a_while_b_runs_does_not_change_b_cancel_or_a_continuation_scope(self):
        parent, _ = self.finish()
        entered, release = threading.Event(), threading.Event()

        def running(_args, **hooks):
            hooks["on_result"](self.result(unverified=True))
            entered.set()
            self.assertTrue(release.wait(3))

        with patch.object(desk_module, "_run_explain_cli", side_effect=running):
            current = self.desk.start(self.question("Question B", ((60, 65),)))
            try:
                self.assertTrue(entered.wait(3))
                self.assertNotIn("reading_scope", self.desk.status())
                saved_a = self.desk.history()["entries"][0]
                self.desk.source_view("0", saved_a["version"])
                self.assertEqual(self.desk.status()["id"], current["id"])
                with self.assertRaisesRegex(ValueError, "only one"):
                    self.desk.start(self.followup(parent["id"]), kind="continue")
                self.desk.cancel(current["id"])
            finally:
                release.set(); self.join()
        self.assertEqual(self.desk.status()["status"], "cancelled")
        self.assertNotIn("reading_scope", self.desk.status())
        child, captured = self.finish(self.followup(parent["id"]), kind="continue")
        self.assertEqual(captured["args"]["focus"], parent["reading_scope"]["focus"])
        self.assertNotIn("main.py:60-65", captured["args"]["focus"])
        self.assertEqual(child["continuation"]["parent_id"], parent["id"])

    def test_continuation_chain_is_flat_and_does_not_require_an_evicted_ancestor(self):
        first, _ = self.finish()
        second, _ = self.finish(self.followup(first["id"], "Independent second question"), kind="continue")
        for index in range(4):
            self.finish(self.question(f"Unrelated question {index}", ((40, 42),)))
        ids = [entry["id"] for entry in self.desk.history()["entries"]]
        self.assertNotIn(first["id"], ids)
        self.assertIn(second["id"], ids)
        with self.assertRaisesRegex(ValueError, "evicted"):
            self.desk.start(self.followup(first["id"]), kind="continue")
        third, captured = self.finish(self.followup(second["id"], "Independent third question"), kind="continue")
        self.assertEqual(third["reading_scope"], first["reading_scope"])
        self.assertEqual(third["continuation"], {"parent_id": second["id"], "parent_question": second["question"]})
        self.assertEqual(captured["args"]["question"], "Independent third question")
        self.assertNotIn(first["id"], str(third["continuation"]))
        self.assertEqual(len(self.desk.history()["entries"]), 5)
        self.assertGreaterEqual(self.desk.history()["evicted"], 2)

    def test_only_complete_gated_answers_or_normal_unverified_prose_receive_scope(self):
        parent, _ = self.finish(value=self.result(unverified=True))
        self.assertEqual(parent["status"], "incomplete")
        self.assertIn("reading_scope", parent)
        child, _ = self.finish(self.followup(parent["id"]), kind="continue")
        self.assertIn("reading_scope", child)
        for fault in ("source", "snapshot", "acceptance", "release", "exception", "cancel", "error", "preview", "project"):
            with self.subTest(fault=fault):
                value = self.project_result() if fault == "project" else self.result()
                if fault in ("source", "snapshot"): value["outcome"][fault + "_unchanged"] = False
                if fault == "acceptance": value["outcome"]["acceptance"]["ok"] = False
                if fault == "release": value["server"]["shutdown"]["return_code"] = None
                if fault == "error": value["error"] = "owned final error"
                if fault == "preview": value = fixtures.ReadingDeskTests.rejected_result(self)
                if fault == "project": value["project_reading"]["discovery"]["ingress_unchanged"] = False
                payload = {"question": "No reusable scope", "version": self.desk.project["version"]} if fault == "project" else self.question()
                failed, _ = self.finish(payload, value=value, kind="project" if fault == "project" else "explain",
                    cancel=fault == "cancel", error=fault == "exception", preview="partial visible draft" if fault == "preview" else None)
                self.assertNotIn("reading_scope", failed)
                self.assertNotIn("reading_scope", self.desk.history()["entries"][-1])
                with self.assertRaises(ValueError):
                    self.desk.start(self.followup(failed["id"]), kind="continue")

    def test_malformed_or_oversized_server_scope_is_revalidated_before_dispatch(self):
        parent, _ = self.finish()
        saved = deepcopy(self.desk.history()["entries"][-1])
        valid = saved["reading_scope"]
        invalid = [None, {}, {**valid, "extra": "not allowed"}, {**valid, "focus": []},
            {**valid, "origin": "unknown"}, {**valid, "focus": ["main.py:1-81"]},
            {**valid, "focus": ["main.py:299-301"]}, {**valid, "focus": ["missing.py:1-2"]},
            {**valid, "focus": ["../main.py:1-2"]}, {**valid, "focus": ["main.py:1-2"] * 2},
            {**valid, "focus": [f"main.py:{line}-{line}" for line in range(1, 5)]},
            {**valid, "supplements": ["main.py:3-4"]}, {**valid, "supplements": "main.py:3-4"},
            {"origin": "project_candidates", "focus": [f"main.py:{line}-{line + 59}" for line in range(1, 301, 60)], "supplements": []},
            {"origin": "project_candidates", "focus": ["main.py:1-60"], "supplements": ["main.py:1-41"]},
            {"origin": "project_candidates", "focus": ["main.py:1-60"], "supplements": ["main.py:61-62"]},
            {"origin": "project_candidates", "focus": ["main.py:1-60"], "supplements": [f"main.py:{line}-{line}" for line in range(1, 6)]}]
        before = self.desk.status()
        with patch.object(desk_module, "_run_explain_cli") as runner:
            for scope in invalid:
                self.desk._history[-1] = json.dumps({**saved, "reading_scope": scope}).encode()
                with self.subTest(scope=scope), self.assertRaises(ValueError):
                    self.desk.start(self.followup(parent["id"]), kind="continue")
        runner.assert_not_called()
        self.assertEqual(self.desk.status(), before)

    def test_exact_payload_current_version_and_closed_desk_are_required(self):
        parent, _ = self.finish()
        valid = self.followup(parent["id"])
        invalid = [None, [], {}, {**valid, "focus": ["main.py:1-2"]}, {**valid, "parent": None},
            {**valid, "parent": "missing"}, {**valid, "question": " "}, {**valid, "question": "x" * 2001},
            {**valid, "version": "stale"}]
        before = self.desk.status()
        with patch.object(desk_module, "_run_explain_cli") as runner:
            for payload in invalid:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    self.desk.start(payload, kind="continue")
            self.assertEqual(self.desk.status(), before)
            self.desk.refresh()  # Same bytes/version, but the bounded session history is gone.
            with self.assertRaisesRegex(ValueError, "evicted"):
                self.desk.start(valid, kind="continue")
            self.desk.close()
            with self.assertRaisesRegex(ValueError, "closing"):
                self.desk.start(valid, kind="continue")
        runner.assert_not_called()

    def test_live_source_drift_uses_existing_pre_gpu_snapshot_gate(self):
        parent, _ = self.finish()
        (self.source / "main.py").write_bytes(self.code.replace("VALUE_1 = 1", "VALUE_1 = 9").encode())
        with patch.object(cli, "prepare_server") as server, patch.object(desk_module, "_run_project_cli") as project:
            self.desk.start(self.followup(parent["id"]), kind="continue")
            self.join()
        server.assert_not_called(); project.assert_not_called()
        job = self.desk.status()
        self.assertEqual(job["status"], "incomplete")
        self.assertIn("source changed since browsing", job["result"]["error"])
        self.assertNotIn("reading_scope", job)

    def test_locate_only_result_cannot_be_reused_as_answer_source(self):
        value = self.result()
        value.update(kind="forge8.locate", status="located")
        value["outcome"].update(status="located", ingress_unchanged=True, candidates=[])
        parent, _ = self.finish({"question": "Find the source", "version": self.desk.project["version"]},
            kind="locate", value=value)
        self.assertEqual(parent["status"], "located")
        self.assertNotIn("reading_scope", parent)
        with patch.object(desk_module, "_run_explain_cli") as runner, self.assertRaises(ValueError):
            self.desk.start(self.followup(parent["id"]), kind="continue")
        runner.assert_not_called()


class ComparisonContinuationTests(fixtures.DeskFixture):
    question = change_fixtures.ChangeDeskTests.question
    result = change_fixtures.ChangeDeskTests.result

    def setUp(self):
        super().setUp()
        state = patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.root / "state")})
        state.start()
        self.addCleanup(state.stop)
        self.desk.close()
        before = {"main.py": b"def answer():\n    return 41\n",
            "caller.py": b"from main import answer\n\ndef show():\n    return str(answer())\n"}
        for name, data in before.items():
            (self.source / name).write_bytes(data)
        (self.source / "main.py").write_bytes(b"def answer():\n    return 42\n")
        self.head = "a" * 40
        self.desk = desk_module.ReadingDesk(self.source)
        self.addCleanup(self.desk.close)
        with patch.object(comparison, "read_head", return_value=GitBaseline(self.head, before, ())), \
                patch.object(comparison, "head_identity", return_value=self.head):
            self.desk.refresh(mode="changes")

    def test_answered_comparison_has_no_continuation_and_cannot_start_one(self):
        with patch.object(desk_module, "_run_explain_cli", side_effect=lambda _args, **hooks: hooks["on_result"](self.result())) as runner:
            self.desk.start(self.question())
            self.desk.worker.join(5)
            self.assertFalse(self.desk.worker.is_alive())
            parent = self.desk.status()
            self.assertEqual(parent["status"], "answered")
            self.assertNotIn("reading_scope", parent)
            with self.assertRaisesRegex(ValueError, "comparison"):
                self.desk.start({"parent": parent["id"], "question": "New question", "version": parent["version"]}, kind="continue")
        runner.assert_called_once()


class ContinuationHTTPTests(_ContinuationFixture):
    request = fixtures.ReadingHTTPTests.request
    close_server = fixtures.ReadingHTTPTests.close_server

    def setUp(self):
        super().setUp()
        self.parent, _ = self.finish()
        self.server, url = desk_module.make_server(self.desk)
        self.origin = url.split("/#", 1)[0]
        self.token = parse_qs(urlsplit(url).fragment)["token"][0]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.close_server)

    def test_authenticated_route_resolves_parent_and_dispatches_one_direct_question(self):
        payload = self.followup(self.parent["id"])
        calls = []

        def run(args, **hooks):
            calls.append((args.question, args.focus))
            hooks["on_result"](self.result())

        with patch.object(desk_module, "_run_explain_cli", side_effect=run) as runner, \
                patch.object(desk_module, "_run_project_cli") as project, patch.object(desk_module, "_run_locate_cli") as locate:
            for headers, auth, expected in (({}, False, 401), ({"Host": "evil.invalid"}, True, 403),
                    ({"Origin": "https://evil.invalid"}, True, 403)):
                self.assertEqual(self.request("/api/continue-question", method="POST", payload=payload, headers=headers, auth=auth)[0], expected)
            for bad in ({**payload, "focus": []}, {**payload, "version": "stale"}, {**payload, "question": "x" * 17_000}):
                self.assertEqual(self.request("/api/continue-question", method="POST", payload=bad)[0], 400)
            status, headers, body = self.request("/api/continue-question", method="POST", payload=payload)
            self.assertEqual(status, 202)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.join()
            self.assertEqual(json.loads(body)["id"], self.desk.status()["id"])
        runner.assert_called_once(); project.assert_not_called(); locate.assert_not_called()
        self.assertEqual(calls, [(payload["question"], self.parent["reading_scope"]["focus"])])
