from __future__ import annotations

import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from forge8 import cli
from forge8 import desk as desk_module


class DeskFixture(unittest.TestCase):
    def setUp(self):
        deployment = patch.object(cli, "_load_deployment", return_value=None)
        deployment.start()
        self.addCleanup(deployment.stop)
        temporary = tempfile.TemporaryDirectory(prefix="forge8-desk-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source, self.assets = self.root / "project", self.root / "assets"
        self.source.mkdir()
        self.assets.mkdir()
        (self.source / "main.py").write_text('# <script>never execute</script>\nvalue = 7\n', encoding="utf-8")
        for target in (desk_module, cli):
            mock = patch.object(target, "_resolve_fix_asset_root", return_value=self.assets)
            mock.start()
            self.addCleanup(mock.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.desk = desk_module.ReadingDesk(self.source)
        self.addCleanup(self.desk.close)

    def question(self):
        return {"question": "Explain value.", "version": self.desk.project["version"],
            "focus": [{"file": "0", "start": 1, "end": 2}]}


class ReadingDeskTests(DeskFixture):
    def project_result(self, *, attempted=True):
        value = self.rejected_result()
        version = self.desk.project["version"]
        value.update(kind="forge8.explain", status="stalled" if attempted else "selection_required")
        value["project_reading"] = {"answer_attempted": attempted,
            "focus": [{"path": "main.py", "start_line": 1, "end_line": 2}] if attempted else [],
            "discovery": {"ok": True, "status": "located", "snapshot_sha256": version,
                "source_unchanged": True, "snapshot_unchanged": True, "ingress_unchanged": True,
                "acceptance": {"ok": True}, "candidates": [{"id": "D0001", "file": "0",
                    "path": "main.py", "name": "read", "start_line": 1, "end_line": 2}]}}
        if not attempted:
            value["outcome"] = None
        return value

    def test_project_question_keeps_one_worker_and_holds_final_scope_until_cleanup(self):
        question = "  從專案找答案\n\t保留原樣。  "
        payload = {"question": question, "version": self.desk.project["version"]}
        for cancel, accepted in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(cancel=cancel, accepted=accepted):
                entered, finished = threading.Event(), threading.Event()
                value = self.project_result()
                if accepted:
                    value.update(ok=True, status="answered")
                    value["outcome"].update(ok=True, status="answered", answer={"claims": [{"text": "interpreted"}]})
                else:
                    value["outcome"]["unverified_prose"] = "complete ordinary text"

                def run(args, **hooks):
                    self.assertEqual(args.question, question)
                    self.assertEqual(args.focus, [])
                    self.assertEqual(hooks["expected_snapshot"], payload["version"])
                    hooks["on_progress"]("Project question: preparing complete candidate source.")
                    hooks["on_text"]("ordinary draft")
                    hooks["on_result"](value)
                    entered.set()
                    self.assertTrue(finished.wait(3))

                with patch.object(desk_module, "_run_project_cli", side_effect=run, create=True) as runner, \
                        patch.object(desk_module, "_run_explain_cli") as manual:
                    identifier = self.desk.start(payload, kind="project")["id"]
                    try:
                        self.assertTrue(entered.wait(3))
                        self.assertEqual(self.desk.status()["kind"], "project")
                        self.assertNotIn("result", self.desk.status())
                        with self.assertRaises(ValueError): self.desk.refresh()
                        with self.assertRaises(ValueError): self.desk.start(payload, kind="project")
                        if cancel: self.desk.cancel(identifier)
                    finally:
                        finished.set(); self.desk.worker.join(5)
                runner.assert_called_once(); manual.assert_not_called()
                job = self.desk.status()
                self.assertEqual(job["question"], question)
                self.assertEqual(job["status"], "cancelled" if cancel else "answered" if accepted else "incomplete")
                self.assertEqual("project_reading" in job["result"], not cancel)
                self.assertEqual("unverified_prose" in job["result"]["outcome"], not cancel and not accepted)
                if cancel: self.assertIsNone(job["result"]["outcome"]["answer"])
                self.assertFalse(self.desk.worker.is_alive())

    def test_project_scope_fallback_and_invalid_final_gates(self):
        payload = {"question": "Find and explain.", "version": self.desk.project["version"]}
        for fault in (None, "cancel", "error", "release", "source", "version", "ingress", "answer_source"):
            with self.subTest(fault=fault):
                value = self.project_result(attempted=fault == "answer_source")
                if fault in ("source", "ingress"): value["project_reading"]["discovery"][fault + "_unchanged"] = False
                if fault == "version": value["project_reading"]["discovery"]["snapshot_sha256"] = "stale"
                if fault == "release": value["server"]["shutdown"]["return_code"] = None
                if fault == "answer_source": value["outcome"]["source_unchanged"] = False
                def run(_args, **hooks):
                    hooks["on_result"](value)
                    if fault == "cancel": hooks["cancel_event"].set()
                    if fault == "error": raise RuntimeError("private")
                with patch.object(desk_module, "_run_project_cli", side_effect=run, create=True):
                    self.desk.start(payload, kind="project"); self.desk.worker.join(5)
                job = self.desk.status()
                self.assertEqual("project_reading" in job["result"], fault is None)
                self.assertFalse(job["result"]["ok"])
                self.assertNotIn("preview", job)
        with patch.object(desk_module, "_run_project_cli", create=True) as runner:
            for invalid in ({**payload, "focus": []}, {**payload, "reader": "gemma12b"}, {**payload, "version": "old"}):
                with self.assertRaises(ValueError): self.desk.start(invalid, kind="project")
            runner.assert_not_called()

    def test_locate_rejects_focus_instead_of_silently_starting_an_explanation(self):
        with self.assertRaisesRegex(ValueError, "expected question"):
            self.desk.start(self.question(), kind="locate")

    def test_project_scope_allows_six_windows_but_keeps_actual_union_and_manual_limits(self):
        (self.source / "main.py").write_text("value = 7\n" * 250, encoding="utf-8")
        self.desk.refresh()
        payload = {"question": "Find and explain.", "version": self.desk.project["version"]}
        cases = [([(line, line) for line in range(1, count + 1)], count <= 6) for count in (4, 6, 7)]
        cases += [([(1, 80), (81, 160), (161, 239), (240, 240)], True),
                  ([(1, 80), (81, 160), (161, 240), (241, 241)], False),
                  ([(1, 80), (61, 140), (121, 200), (161, 240)], True)]
        for bounds, accepted in cases:
            with self.subTest(bounds=bounds, accepted=accepted):
                value = self.project_result()
                value["project_reading"]["focus"] = [{"path": "main.py", "start_line": start, "end_line": end} for start, end in bounds]
                def run(_args, **hooks): hooks["on_result"](value)
                with patch.object(desk_module, "_run_project_cli", side_effect=run):
                    self.desk.start(payload, kind="project"); self.desk.worker.join(5)
                self.assertFalse(self.desk.worker.is_alive())
                self.assertEqual("project_reading" in self.desk.status()["result"], accepted)
        with patch.object(desk_module, "_run_explain_cli") as manual:
            with self.assertRaises(ValueError):
                self.desk.start({**payload, "focus": [{"file": "0", "start": line, "end": line} for line in range(1, 5)]})
            manual.assert_not_called()

    def test_locate_is_one_nonstream_worker_without_automatic_explanation(self):
        payload = {"question": "Where should I begin?", "version": self.desk.project["version"]}
        candidates = [{"id": "D0001", "file": "0", "path": "main.py", "name": "read",
            "kind": "function", "start_line": 1, "definition_line": 1, "end_line": 2, "stub": False}]

        def locate(args, *, on_progress, on_result, cancel_event, expected_snapshot):
            self.assertEqual(args.question, payload["question"])
            self.assertEqual(args.reader, self.desk.reader)
            self.assertEqual(expected_snapshot, payload["version"])
            self.assertFalse(cancel_event.is_set())
            on_progress("choosing source candidates")
            on_result({"kind": "forge8.locate", "ok": True, "status": "located",
                "outcome": {"ok": True, "status": "located", "candidates": candidates,
                    "source_unchanged": True, "snapshot_unchanged": True, "ingress_unchanged": True,
                    "acceptance": {"ok": True}},
                "server": {"start": {"ok": True, "pid": 123},
                    "shutdown": {"ok": True, "status": "terminated", "return_code": 0},
                    "supervisor_secret_cleared": True, "transport_secret_cleared": True}})

        with patch.object(desk_module, "_run_locate_cli", side_effect=locate) as run, \
                patch.object(desk_module, "_run_explain_cli") as explain:
            self.desk.start(payload, kind="locate")
            self.desk.worker.join(5)
            self.assertFalse(self.desk.worker.is_alive())
        run.assert_called_once()
        explain.assert_not_called()
        job = self.desk.status()
        self.assertEqual((job["kind"], job["status"]), ("locate", "located"))
        self.assertEqual(job["result"]["outcome"]["candidates"], candidates)
        self.assertNotIn("preview", job)
        self.assertNotIn("rejected_preview", job)
        self.assertEqual(self.desk.project["version"], payload["version"])

    def test_locate_preflight_rejects_live_drift_and_unsupported_catalogue_before_server(self):
        for content, expected in (("def read(): return 8\n", "source changed since browsing"),
                ("def unfinished(\n", "catalogue unavailable")):
            with self.subTest(content=content):
                (self.source / "main.py").write_text(content, encoding="utf-8")
                if "unfinished" in content:
                    self.desk.refresh()
                with patch.object(cli, "prepare_server") as server:
                    self.desk.start({"question": "Where to read?", "version": self.desk.project["version"]}, kind="locate")
                    self.desk.worker.join(5)
                    self.assertFalse(self.desk.worker.is_alive())
                server.assert_not_called()
                self.assertEqual(self.desk.status()["status"], "incomplete")
                self.assertIn(expected, self.desk.status()["result"]["error"])

    def test_reader_is_fixed_by_the_host_and_survives_refresh_and_question(self):
        for reader in ("qwen35", "gemma12b"):
            with self.subTest(reader=reader), patch.object(desk_module, "_resolve_fix_asset_root", return_value=self.assets) as assets:
                desk = desk_module.ReadingDesk(self.source, reader=reader)
                try:
                    assets.assert_called_once_with(reader)
                    self.assertEqual(desk.project["reader"], reader)
                    self.assertEqual(desk.status()["reader"], reader)
                    self.assertEqual(desk.refresh()["reader"], reader)
                    payload = {**self.question(), "version": desk.project["version"]}
                    with patch.object(desk_module, "_run_explain_cli") as run:
                        with self.assertRaisesRegex(ValueError, "expected question"):
                            desk.start({**payload, "reader": "qwen35"})
                        run.assert_not_called()
                        desk.start(payload); desk.worker.join(5)
                        self.assertFalse(desk.worker.is_alive())
                        self.assertEqual(run.call_args.args[0].reader, reader)
                        self.assertEqual(desk.status()["reader"], reader)
                finally:
                    desk.close()
        with patch.object(desk_module, "_fix_runs_parent") as runs, self.assertRaisesRegex(ValueError, "reader"):
            desk_module.ReadingDesk(self.source, reader="unknown")
        runs.assert_not_called()

    def rejected_result(self):
        return {"ok": False, "status": "stalled", "outcome": {
            "ok": False, "status": "stalled", "answer": None,
            "acceptance": {"ok": True}, "source_unchanged": True, "snapshot_unchanged": True,
        }, "server": {"start": {"ok": True, "pid": 123},
            "shutdown": {"ok": True, "status": "terminated", "return_code": 0},
            "supervisor_secret_cleared": True, "transport_secret_cleared": True}}

    def run_preview_result(self, value, text, *, cancel=False, error=False):
        def run(_args, **hooks):
            hooks["on_text"](text)
            if cancel:
                hooks["cancel_event"].set()
            hooks["on_result"](value)
            if error:
                raise RuntimeError("private failure")
        with patch.object(desk_module, "_run_explain_cli", side_effect=run):
            self.desk.start(self.question())
            self.desk.worker.join(5)
            self.assertFalse(self.desk.worker.is_alive())
        return self.desk.status()

    def test_browsing_and_literal_search_are_snapshot_only_without_model(self):
        with patch.object(desk_module, "_run_explain_cli") as inference:
            version = self.desk.project["version"]
            (self.source / "main.py").write_text("changed live source", encoding="utf-8")
            view = self.desk.source_view("0", version)
            self.assertEqual(view["lines"][1], "value = 7")
            self.assertIn("<script>", view["lines"][0])
            hits = self.desk.search("value")
            self.assertEqual(hits["matches"][0]["line"], 2)
            self.assertFalse(hits["truncated"])
            with self.assertRaises(ValueError):
                self.desk.source_view("../private", version)
            with self.assertRaises(ValueError):
                self.desk.source_view("0", "unknown version")
        inference.assert_not_called()

    def test_stale_browsed_source_is_rejected_before_model_preparation(self):
        (self.source / "main.py").write_text("value = 8\n", encoding="utf-8")
        payload = self.question()
        payload["focus"][0]["end"] = 1
        with patch.object(cli, "prepare_server") as server:
            self.desk.start(payload)
            self.desk.worker.join(5)
            self.assertFalse(self.desk.worker.is_alive())
        server.assert_not_called()
        state = self.desk.status()
        self.assertEqual(state["status"], "incomplete")
        self.assertIn("source changed since browsing", state["result"]["error"])

    def test_refresh_invalidates_old_version_and_resets_visible_job(self):
        old = self.desk.project["version"]
        (self.source / "main.py").write_text("new value\n", encoding="utf-8")
        project = self.desk.refresh()
        self.assertNotEqual(old, project["version"])
        self.assertEqual(self.desk.status()["status"], "idle")
        with self.assertRaises(ValueError):
            self.desk.source_view("0", old)

    def test_one_worker_cancellation_and_cleanup_are_observed_before_cancelled(self):
        entered, cleanup_allowed = threading.Event(), threading.Event()

        def run(_args, *, on_progress, on_result, on_text, cancel_event, expected_snapshot):
            self.assertEqual(expected_snapshot, self.desk.project["version"])
            on_progress("generating")
            on_text("草稿 <script>不執行</script>")
            entered.set()
            self.assertTrue(cancel_event.wait(3))
            on_text("late draft must not reappear after cancellation")
            self.assertTrue(cleanup_allowed.wait(3))
            on_result({"ok": False, "status": "interrupted"})

        with patch.object(desk_module, "_run_explain_cli", side_effect=run):
            identifier = self.desk.start(self.question())["id"]
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.desk.status()["preview"], "草稿 <script>不執行</script>")
            with self.assertRaises(ValueError):
                self.desk.start(self.question())
            with self.assertRaises(ValueError):
                self.desk.refresh()
            self.desk.cancel(identifier)
            self.assertEqual(self.desk.status()["status"], "cancelling")
            self.assertNotIn("preview", self.desk.status())
            cleanup_allowed.set()
            self.desk.worker.join(5)
            self.assertFalse(self.desk.worker.is_alive())
        self.assertEqual(self.desk.status()["status"], "cancelled")
        self.assertNotIn("preview", self.desk.status())

    def test_cancel_during_artifact_hash_stops_reading_without_starting_model(self):
        from forge8.runtime import sha256_file
        entered, release_read = threading.Event(), threading.Event()
        reads = []
        test = self

        class OneBlockedRead(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                data = super().read(size)
                entered.set()
                test.assertTrue(release_read.wait(3))
                return data

        stream = OneBlockedRead(b"first chunk and unread remaining bytes")
        artifact = Mock()
        artifact.open.return_value = stream

        def preparing(_root, **options):
            # Real hashing and real CLI/desk cancellation; only the small owned
            # artifact stream is controlled. No model/runtime process is used.
            sha256_file(artifact, chunk_size=4, cancel_requested=options["cancel_requested"])
            self.fail("cancelled verification must not return even a partial digest")

        with (patch.object(cli, "prepare_server", side_effect=preparing),
              patch.object(cli, "LocalServerSupervisor") as supervisor,
              patch.object(cli, "CancellableTransport") as transport):
            try:
                identifier = self.desk.start(self.question())["id"]
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.desk.status()["phase"], "hashing the pinned local runtime and model")
                self.desk.cancel(identifier)
                self.assertEqual(self.desk.status()["status"], "cancelling")
            finally:
                release_read.set()
                self.desk.worker.join(5)
            self.assertFalse(self.desk.worker.is_alive())
        supervisor.assert_not_called()
        transport.assert_not_called()
        self.assertEqual(reads, [4])
        self.assertTrue(stream.closed)
        state = self.desk.status()
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["gpu"], "not_acquired")
        self.assertEqual(state["result"]["status"], "interrupted")
        self.assertIsNone(state["result"]["outcome"])
        self.assertNotIn("preview", state)

    def test_preview_is_bounded_and_cleared_for_every_final_result(self):
        for ok in (True, False):
            def run(_args, **hooks):
                hooks["on_text"]("字" * 11_999)
                hooks["on_text"]("尾巴")
                self.assertEqual(self.desk.status()["preview"], "字" * 11_999 + "尾")
                hooks["on_result"]({"ok": ok, "status": "answered" if ok else "backend_error"})
            with self.subTest(ok=ok), patch.object(desk_module, "_run_explain_cli", side_effect=run):
                self.desk.start(self.question())
                self.desk.worker.join(5)
                self.assertFalse(self.desk.worker.is_alive())
                self.assertEqual(self.desk.status()["status"], "answered" if ok else "incomplete")
                self.assertNotIn("preview", self.desk.status())
                self.assertNotIn("rejected_preview", self.desk.status())

    def test_complete_unverified_text_waits_for_worker_end_and_late_cancel_clears_it(self):
        text = "  完整但未驗證\r\n\t<script>literal</script> [E1-L3]  "
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                entered, finish = threading.Event(), threading.Event()
                value = self.rejected_result()
                value["outcome"]["unverified_prose"] = text

                def run(_args, **hooks):
                    hooks["on_text"]("partial stream")
                    hooks["on_result"](value)
                    entered.set()
                    self.assertTrue(finish.wait(3))

                with patch.object(desk_module, "_run_explain_cli", side_effect=run):
                    identifier = self.desk.start(self.question())["id"]
                    try:
                        self.assertTrue(entered.wait(3))
                        self.assertNotIn("result", self.desk.status())
                        self.assertEqual(self.desk.status()["preview"], "partial stream")
                        if cancelled:
                            self.desk.cancel(identifier)
                            self.assertEqual(self.desk.status()["status"], "cancelling")
                    finally:
                        finish.set()
                        self.desk.worker.join(5)
                self.assertFalse(self.desk.worker.is_alive())
                job = self.desk.status()
                self.assertEqual(job["status"], "cancelled" if cancelled else "incomplete")
                self.assertFalse(job["result"]["ok"])
                self.assertIsNone(job["result"]["outcome"]["answer"])
                if cancelled:
                    self.assertNotIn("unverified_prose", job["result"]["outcome"])
                else:
                    self.assertEqual(job["result"]["outcome"]["unverified_prose"], text)
                self.assertNotIn("preview", job)
                self.assertNotIn("rejected_preview", job)
                self.assertEqual(value["outcome"]["unverified_prose"], text)

    def test_unverified_publication_rejects_late_error_unknown_cleanup_and_bad_source(self):
        for fault in ("source", "snapshot", "acceptance", "release", "answer", "error", "unsafe", "limit"):
            with self.subTest(fault=fault):
                value = self.rejected_result()
                value["outcome"]["unverified_prose"] = "complete ordinary model text"
                if fault in ("source", "snapshot"): value["outcome"][fault + "_unchanged"] = False
                if fault == "acceptance": value["outcome"]["acceptance"]["ok"] = False
                if fault == "release": value["server"]["shutdown"]["return_code"] = None
                if fault == "answer": value["outcome"]["answer"] = {"claims": []}
                if fault == "unsafe": value["outcome"]["unverified_prose"] = "bad\u202etext"
                if fault == "limit": value["outcome"]["unverified_prose"] = "x" * 12001
                job = self.run_preview_result(value, "partial ordinary stream", error=fault == "error")
                self.assertNotIn("unverified_prose", job["result"]["outcome"])
                self.assertEqual(job["status"], "incomplete")
                self.assertIn("unverified_prose", value["outcome"])

    def test_rejected_preview_preserves_exact_bounded_text_in_ram_without_accepting_answer(self):
        value = self.rejected_result()
        original = json.loads(json.dumps(value))
        before = {path.relative_to(self.desk.root): path.read_bytes()
            for path in self.desk.root.rglob("*") if path.is_file()}
        for text in ("  未採用\r\n\t<script>literal</script> [E1-L3]\r尾端 \n", "字" * 11_999 + "尾巴"):
            with self.subTest(length=len(text)), \
                    patch.object(Path, "read_bytes", side_effect=AssertionError("trace/source read")), \
                    patch.object(Path, "read_text", side_effect=AssertionError("trace/source read")):
                state = self.run_preview_result(value, text)
            self.assertEqual(state["rejected_preview"], text[:12_000])
            self.assertEqual(state["status"], "incomplete")
            self.assertEqual(state["gpu"], "released")
            self.assertNotIn("preview", state)
            self.assertEqual(state["result"], original)
            self.assertIsNone(state["result"]["outcome"]["answer"])
        self.assertEqual({path.relative_to(self.desk.root): path.read_bytes()
            for path in self.desk.root.rglob("*") if path.is_file()}, before)

    def test_rejected_preview_requires_coherent_failure_source_flags_and_released_gpu(self):
        missing = object()
        cases = (
            (("ok",), missing), (("ok",), 0), (("ok",), True),
            (("status",), missing), (("status",), "backend_error"), (("status",), "interrupted"),
            (("outcome",), None), (("outcome",), "not an outcome"),
            (("outcome", "ok"), missing), (("outcome", "ok"), 0), (("outcome", "ok"), True),
            (("outcome", "status"), missing), (("outcome", "status"), "integrity_failed"),
            (("outcome", "answer"), missing), (("outcome", "answer"), {"claims": []}),
            (("outcome", "acceptance"), missing), (("outcome", "acceptance"), None),
            (("outcome", "acceptance", "ok"), missing), (("outcome", "acceptance", "ok"), False),
            (("outcome", "acceptance", "ok"), 1),
            (("outcome", "source_unchanged"), missing), (("outcome", "source_unchanged"), False),
            (("outcome", "source_unchanged"), 1),
            (("outcome", "snapshot_unchanged"), missing), (("outcome", "snapshot_unchanged"), False),
            (("outcome", "snapshot_unchanged"), 1),
            (("server",), None), (("server", "shutdown", "ok"), False),
            (("server", "shutdown", "return_code"), None),
            (("server", "supervisor_secret_cleared"), False),
            (("server", "transport_secret_cleared"), missing),
        )
        for keys, replacement in cases:
            value = self.rejected_result()
            target = value
            for key in keys[:-1]:
                target = target[key]
            if replacement is missing:
                target.pop(keys[-1])
            else:
                target[keys[-1]] = replacement
            with self.subTest(keys=keys, replacement=replacement):
                state = self.run_preview_result(value, "unaccepted text")
                self.assertNotIn("preview", state)
                self.assertNotIn("rejected_preview", state)

    def test_rejected_preview_discards_cancelled_failed_blank_or_unsafe_text_without_sanitizing(self):
        for options in ({"cancel": True}, {"error": True}):
            with self.subTest(options=options):
                state = self.run_preview_result(self.rejected_result(), "unaccepted text", **options)
                self.assertNotIn("rejected_preview", state)
                self.assertNotIn("preview", state)
        for text in ("", " \r\n\t", "valid\x00tail", "valid\x1btail", "valid\vtail",
                "valid\u202etail", "valid\u2028tail", "valid\u2029tail", "valid\ud800tail"):
            with self.subTest(text=repr(text)):
                state = self.run_preview_result(self.rejected_result(), text)
                self.assertNotIn("rejected_preview", state)
                self.assertNotIn("preview", state)

    def test_new_job_and_refresh_remove_rejected_preview(self):
        state = self.run_preview_result(self.rejected_result(), "first unaccepted text")
        self.assertIn("rejected_preview", state)
        entered, finish = threading.Event(), threading.Event()
        def run(_args, **hooks):
            entered.set()
            finish.wait(3)
            hooks["on_result"](self.rejected_result())
        with patch.object(desk_module, "_run_explain_cli", side_effect=run):
            self.desk.start(self.question())
            try:
                self.assertTrue(entered.wait(3))
                self.assertNotIn("rejected_preview", self.desk.status())
            finally:
                finish.set()
                self.desk.worker.join(5)
            self.assertFalse(self.desk.worker.is_alive())
        self.assertNotIn("rejected_preview", self.desk.status())
        self.run_preview_result(self.rejected_result(), "another unaccepted text")
        self.desk.refresh()
        self.assertNotIn("rejected_preview", self.desk.status())
        self.assertEqual(self.desk.status()["status"], "idle")

    def test_worker_result_and_source_identity_are_retained_without_stdout_capture(self):
        question = " \tExplain value.\r\n```python\nif ready:\r\n\tvalue = 7\n```\rWhat type?\t \n"
        def run(_args, **hooks):
            self.assertEqual(_args.question, question)
            hooks["on_result"]({"ok": True, "status": "answered", "question": _args.question,
                "outcome": {"question": _args.question, "answer": {"claims": []}}})
        with patch.object(desk_module, "_run_explain_cli", side_effect=run):
            payload = self.question()
            payload["question"] = question
            self.desk.start(payload)
            payload["question"] = "a later browser draft"
            self.desk.worker.join(5)
        state = self.desk.status()
        self.assertEqual(state["status"], "answered")
        self.assertEqual(state["question"], question)
        self.assertEqual(state["result"]["question"], question)
        self.assertEqual(state["result"]["outcome"]["question"], question)
        self.assertEqual(state["version"], self.desk.project["version"])
        self.assertEqual(state["files"], self.desk.project["files"])

    def test_invalid_questions_are_rejected_before_starting_a_worker(self):
        for question in ("\r\n\t ", "x" * 2001, "text\x00", "text\u202e", "text\u2028", "text\ud800"):
            with self.subTest(question=repr(question[:20])), self.assertRaises(ValueError):
                self.desk.start({**self.question(), "question": question})
        self.assertIsNone(self.desk.worker)

    def test_invalid_ranges_cannot_become_worker_arguments(self):
        for span in ({"file": "../secret", "start": 1, "end": 1},
                     {"file": "0", "start": True, "end": 2},
                     {"file": "0", "start": 1, "end": 81},
                     {"file": "0", "start": 2, "end": 1}):
            payload = self.question()
            payload["focus"] = [span]
            with self.subTest(span=span), self.assertRaises(ValueError):
                self.desk.start(payload)
        self.assertIsNone(self.desk.worker)

    def test_interruption_without_proven_cleanup_is_not_labelled_cancelled(self):
        def run(_args, **hooks):
            hooks["on_result"]({"ok": False, "status": "interrupted", "server": {
                "start": {"ok": True, "pid": 123}, "shutdown": {"ok": False, "status": "shutdown_error"},
            }})
        with patch.object(desk_module, "_run_explain_cli", side_effect=run):
            self.desk.start(self.question())
            self.desk.worker.join(5)
        self.assertEqual(self.desk.status()["status"], "incomplete")
        self.assertEqual(self.desk.status()["gpu"], "unknown")

    def test_closed_desk_cannot_start_a_late_request(self):
        self.desk.close()
        with self.assertRaises(ValueError):
            self.desk.start(self.question())
        with self.assertRaises(ValueError):
            self.desk.refresh()


class DefinitionLookupTests(DeskFixture):
    def setUp(self):
        super().setUp()
        (self.source / "main.py").write_text(
            "class Context:\n"
            "    @overload\n"
            "    def invoke(self, value: int): ...\n"
            "    @overload\n"
            "    def invoke(self, value: str): ...\n"
            "    def invoke(self, value):\n"
            "        return value\n"
            "class Command:\n"
            "    def invoke(self): pass\n"
            "class Outer:\n"
            "    class Context:\n"
            "        async def invoke(self): pass\n"
            "def Invoke(): pass\n"
            "class 類別:\n"
            "    def 讀取(self): pass\n", encoding="utf-8")
        (self.source / "other.py").write_text("def invoke(): pass\n", encoding="utf-8")
        self.version = self.desk.refresh()["version"]

    def test_terminal_name_keeps_ambiguous_duplicates_stubs_and_coordinates(self):
        result = self.desk.definitions("invoke", self.version)
        self.assertEqual(set(result), {"version", "matches", "truncated", "unavailable_files"})
        self.assertEqual(result["version"], self.version)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["unavailable_files"], 0)
        self.assertEqual([(item["path"], item["name"], item["stub"]) for item in result["matches"]], [
            ("main.py", "Context.invoke", True), ("main.py", "Context.invoke", True),
            ("main.py", "Context.invoke", False), ("main.py", "Command.invoke", False),
            ("main.py", "Outer.Context.invoke", False), ("other.py", "invoke", False),
        ])
        first = result["matches"][0]
        self.assertEqual(first, {"file": "0", "path": "main.py", "name": "Context.invoke",
            "kind": "function", "start_line": 2, "definition_line": 3, "end_line": 3, "stub": True})
        self.assertEqual(result["matches"][4]["kind"], "async function")
        first["name"] = "caller mutation"
        self.assertEqual(self.desk.definitions("invoke", self.version)["matches"][0]["name"], "Context.invoke")

    def test_qualified_suffix_is_exact_case_sensitive_and_never_resolves_receivers(self):
        result = self.desk.definitions("Context.invoke", self.version)
        self.assertEqual([item["name"] for item in result["matches"]],
            ["Context.invoke", "Context.invoke", "Context.invoke", "Outer.Context.invoke"])
        self.assertEqual(len(self.desk.definitions("Outer.Context.invoke", self.version)["matches"]), 1)
        self.assertEqual([item["name"] for item in self.desk.definitions("Invoke", self.version)["matches"]], ["Invoke"])
        for query in ("ctx.invoke", "context.invoke", "Context.inv", "INVOKE", "text.invoke", "missing"):
            with self.subTest(query=query):
                self.assertEqual(self.desk.definitions(query, self.version)["matches"], [])
        self.assertEqual([item["name"] for item in self.desk.definitions("類別.讀取", self.version)["matches"]], ["類別.讀取"])
        self.assertEqual(self.desk.definitions("Context", self.version)["matches"][0]["kind"], "class")

    def test_invalid_names_and_stale_versions_are_rejected(self):
        for query in (None, True, 42, "", " ", "a" * 129, " invoke", "invoke ", "foo..bar",
                ".foo", "foo.", "foo/bar", "foo()", "3foo", "foo bar", "foo\nbar", "foo\u202ebar", "\ud800"):
            with self.subTest(query=repr(query)), self.assertRaises(ValueError):
                self.desk.definitions(query, self.version)
        self.assertEqual(self.desk.definitions("a" * 128, self.version)["matches"], [])
        for version in (None, "", "unknown"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "stale"):
                self.desk.definitions("invoke", version)
        (self.source / "main.py").write_text("def replacement(): pass\n", encoding="utf-8")
        self.desk.refresh()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.desk.definitions("invoke", self.version)

    def test_lookup_uses_only_admitted_metadata_after_live_source_changes(self):
        expected = self.desk.definitions("invoke", self.version)
        (self.source / "main.py").write_text("def replaced_live(): pass\n", encoding="utf-8")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("live read")), \
                patch.object(Path, "read_text", side_effect=AssertionError("live read")), \
                patch.object(desk_module, "_python_outline", side_effect=AssertionError("reparse")), \
                patch.object(desk_module, "prepare_repository_snapshot", side_effect=AssertionError("resnapshot")), \
                patch.object(desk_module, "_run_explain_cli") as inference:
            self.assertEqual(self.desk.definitions("invoke", self.version), expected)
            self.assertEqual(self.desk.definitions("replaced_live", self.version)["matches"], [])
        inference.assert_not_called()
        self.assertIsNone(self.desk.worker)

    def test_match_cap_preserves_first_forty_in_file_and_outline_order(self):
        for count in (40, 41):
            (self.source / "main.py").write_text("def repeated(): pass\n" * count, encoding="utf-8")
            version = self.desk.refresh()["version"]
            result = self.desk.definitions("repeated", version)
            self.assertEqual(len(result["matches"]), 40)
            self.assertEqual([item["definition_line"] for item in result["matches"]], list(range(1, 41)))
            self.assertEqual(result["truncated"], count > 40)
            self.assertEqual(self.desk.definitions("repeated", version), result)

    def test_unavailable_count_includes_limited_python_files_even_after_match_cap(self):
        (self.source / "main.py").write_text("def repeated(): pass\n" * 41, encoding="utf-8")
        (self.source / "z_broken.py").write_text("def broken(:\n", encoding="utf-8")
        (self.source / "z_limited.pyi").write_text("def stub(): ...\n" * 301, encoding="utf-8")
        (self.source / "README.md").write_text("No Python outline.\n", encoding="utf-8")
        version = self.desk.refresh()["version"]
        result = self.desk.definitions("repeated", version)
        self.assertEqual(result["unavailable_files"], 2)
        self.assertTrue(result["truncated"])
        readme = next(item["id"] for item in self.desk.project["files"] if item["path"] == "README.md")
        self.assertEqual(self.desk.outlines[readme]["status"], "unsupported")
        # Aggregate metadata exhaustion may also mark non-Python files limited;
        # it must not imply that a Python definition lookup missed those files.
        with patch.dict(self.desk.outlines, {readme: desk_module._outline_status("limited", "metadata_limit")}):
            self.assertEqual(self.desk.definitions("repeated", version), result)


class ReadingHTTPTests(DeskFixture):
    def setUp(self):
        super().setUp()
        self.server, url = desk_module.make_server(self.desk)
        self.origin = url.split("/#", 1)[0]
        self.token = parse_qs(urlsplit(url).fragment)["token"][0]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path, *, method="GET", payload=None, headers=None, auth=True):
        fields = {"Authorization": "Bearer " + self.token} if auth else {}
        body = None
        if method == "POST":
            body = json.dumps(payload or {}).encode()
            fields.update({"Origin": self.origin, "Content-Type": "application/json"})
        fields.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body, fields)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_private_source_requires_token_correct_host_and_origin(self):
        for fields, auth, expected in (({}, False, 401), ({"Host": "evil.invalid"}, True, 403),
                ({"Origin": "https://evil.invalid"}, True, 403), ({"Authorization": "Bearer é"}, True, 401)):
            with self.subTest(fields=fields):
                status, _, body = self.request("/api/project", headers=fields, auth=auth)
                self.assertEqual(status, expected)
                self.assertNotIn(b"main.py", body)
        status, headers, body = self.request("/api/project")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["files"][0]["path"], "main.py")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_reading_note_is_only_a_same_origin_static_asset(self):
        status, headers, body = self.request("/reading-note.js", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(body, (Path(desk_module.__file__).parent / "web/reading-note.js").read_bytes())
        html = self.request("/", auth=False)[2]
        self.assertLess(html.index(b'src="/reading-note.js"'), html.index(b'src="/app.js"'))
        for fields in ({"Host": "evil.invalid"}, {"Origin": "https://evil.invalid"}):
            self.assertEqual(self.request("/reading-note.js", headers=fields, auth=False)[0], 403)
        self.assertEqual(self.request("/api/reading-note")[0], 404)
        self.assertEqual(self.request("/reading-note.js", method="POST", payload={})[0], 404)

    def test_locate_http_auth_exact_payload_and_shared_cancellation(self):
        payload = {"question": "Where does the workflow start?", "version": self.desk.project["version"]}
        with patch.object(desk_module, "_run_locate_cli") as run:
            for fields, auth, expected in (({}, False, 401), ({"Host": "evil.invalid"}, True, 403),
                    ({"Origin": "https://evil.invalid"}, True, 403)):
                with self.subTest(fields=fields):
                    self.assertEqual(self.request("/api/locate", method="POST", payload=payload,
                        headers=fields, auth=auth)[0], expected)
            for invalid in ({**payload, "focus": []}, {**payload, "reader": "gemma12b"},
                    {**payload, "version": "stale"}, {"question": "Where?"}):
                self.assertEqual(self.request("/api/locate", method="POST", payload=invalid)[0], 400)
            run.assert_not_called()
        entered, cleaned = threading.Event(), threading.Event()

        def locate(_args, *, on_progress, on_result, cancel_event, expected_snapshot):
            self.assertEqual(expected_snapshot, payload["version"])
            on_result({"ok": True, "status": "located", "outcome": {"ok": True,
                    "status": "located", "candidates": [{"id": "D0001"}],
                    "source_unchanged": True, "snapshot_unchanged": True, "ingress_unchanged": True,
                    "acceptance": {"ok": True}},
                "server": {"start": {"ok": True, "pid": 123},
                    "shutdown": {"ok": True, "status": "terminated", "return_code": 0},
                    "supervisor_secret_cleared": True, "transport_secret_cleared": True}})
            entered.set()
            self.assertTrue(cancel_event.wait(3))
            self.assertTrue(cleaned.wait(3))

        with patch.object(desk_module, "_run_locate_cli", side_effect=locate) as run, \
                patch.object(desk_module, "_run_explain_cli") as explain:
            try:
                status, _, body = self.request("/api/locate", method="POST", payload=payload)
                self.assertEqual(status, 202)
                identifier = json.loads(body)["id"]
                self.assertTrue(entered.wait(3))
                self.assertNotIn("result", self.desk.status(), "do not expose candidates while worker is active")
                self.assertEqual(self.request("/api/jobs", method="POST", payload=self.question())[0], 400)
                self.assertEqual(self.request("/api/refresh", method="POST")[0], 400)
                self.assertEqual(self.request(f"/api/jobs/{identifier}/cancel", method="POST")[0], 202)
                self.assertEqual(self.desk.status()["status"], "cancelling")
            finally:
                cleaned.set()
                self.desk.cancel_event.set()
                self.desk.worker.join(5)
        self.assertFalse(self.desk.worker.is_alive())
        run.assert_called_once(); explain.assert_not_called()
        self.assertEqual(self.desk.status()["status"], "cancelled")
        self.assertEqual(self.desk.status()["result"]["outcome"]["candidates"], [])
        self.assertNotIn("preview", self.desk.status())
        self.assertNotIn("rejected_preview", self.desk.status())

    def test_project_question_http_is_explicit_and_preserves_question_without_client_focus(self):
        payload = {"question": "  專案問題\n\t保持原樣  ", "version": self.desk.project["version"]}
        with patch.object(desk_module, "_run_project_cli") as runner, \
                patch.object(desk_module, "_run_locate_cli") as locate, \
                patch.object(desk_module, "_run_explain_cli") as explain:
            for fields, auth, expected in (({}, False, 401), ({"Host": "evil.invalid"}, True, 403),
                    ({"Origin": "https://evil.invalid"}, True, 403)):
                self.assertEqual(self.request("/api/project-question", method="POST", payload=payload,
                    headers=fields, auth=auth)[0], expected)
            for invalid in ({**payload, "focus": []}, {**payload, "reader": "gemma12b"},
                    {**payload, "version": "old"}, {"question": "missing version"}):
                self.assertEqual(self.request("/api/project-question", method="POST", payload=invalid)[0], 400)
            runner.assert_not_called()
            status, _, body = self.request("/api/project-question", method="POST", payload=payload)
            self.assertEqual(status, 202)
            self.desk.worker.join(5)
            self.assertFalse(self.desk.worker.is_alive())
            runner.assert_called_once(); locate.assert_not_called(); explain.assert_not_called()
            self.assertEqual(runner.call_args.args[0].question, payload["question"])
            self.assertEqual(runner.call_args.args[0].focus, [])
            self.assertEqual(runner.call_args.kwargs["expected_snapshot"], payload["version"])
            self.assertEqual(self.desk.status()["kind"], "project")
            self.assertEqual(self.desk.status()["id"], json.loads(body)["id"])

    def test_mutation_requires_origin_and_json_and_arbitrary_paths_are_not_served(self):
        for fields in ({"Origin": "null"}, {"Origin": "https://evil.invalid"}, {"Content-Type": "text/plain"}):
            status, _, _ = self.request("/api/refresh", method="POST", headers=fields)
            self.assertIn(status, (400, 403))
        status, _, _ = self.request("/api/source?file=../../private&version=" + self.desk.project["version"])
        self.assertEqual(status, 400)
        for path in ("/../../private", "/api/execute", "/.env"):
            self.assertEqual(self.request(path)[0], 404)

    def test_definition_route_returns_cached_versioned_candidates_without_model(self):
        (self.source / "main.py").write_text(
            "class Context:\n    @overload\n    def invoke(self): ...\n"
            "    def invoke(self): pass\nclass Command:\n    def invoke(self): pass\n", encoding="utf-8")
        version = self.desk.refresh()["version"]
        expected = self.desk.definitions("Context.invoke", version)
        (self.source / "main.py").write_text("def live_replacement(): pass\n", encoding="utf-8")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("live read")), \
                patch.object(desk_module, "_python_outline", side_effect=AssertionError("reparse")), \
                patch.object(desk_module, "_run_explain_cli") as inference:
            status, headers, body = self.request("/api/definitions?" + urlencode({"q": "Context.invoke", "version": version}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), expected)
        self.assertEqual([item["stub"] for item in expected["matches"]], [True, False])
        self.assertEqual(headers["Cache-Control"], "no-store")
        inference.assert_not_called()
        self.assertIsNone(self.desk.worker)

    def test_definition_route_preserves_auth_validation_and_readonly_boundary(self):
        path = "/api/definitions?" + urlencode({"q": "value", "version": self.desk.project["version"]})
        for fields, auth, expected in (({}, False, 401), ({"Host": "evil.invalid"}, True, 403),
                ({"Origin": "https://evil.invalid"}, True, 403)):
            with self.subTest(fields=fields):
                self.assertEqual(self.request(path, headers=fields, auth=auth)[0], expected)
        for params in ({"q": "value"}, {"q": "value", "version": "stale"},
                {"version": self.desk.project["version"]},
                {"q": "foo()", "version": self.desk.project["version"]}):
            with self.subTest(params=params):
                self.assertEqual(self.request("/api/definitions?" + urlencode(params))[0], 400)
        self.assertEqual(self.request(path, method="POST")[0], 404)
        self.assertEqual(self.desk.status()["status"], "idle")

    def test_slow_project_response_does_not_hold_the_worker_or_cancel_lock(self):
        writing, release = threading.Event(), threading.Event()
        original = self.server.RequestHandlerClass.send
        results = []

        def slow_send(handler, status, body, *args):
            writing.set()
            release.wait(2)
            return original(handler, status, body, *args)

        with patch.object(self.server.RequestHandlerClass, "send", slow_send):
            request = threading.Thread(target=lambda: results.append(self.request("/api/project")))
            request.start()
            try:
                self.assertTrue(writing.wait(2))
                acquired = self.desk.lock.acquire(timeout=0.2)
                if acquired:
                    self.desk.lock.release()
                self.assertTrue(acquired, "HTTP response blocked the desk lifecycle lock")
            finally:
                release.set()
                request.join(3)
        self.assertFalse(request.is_alive())
        self.assertEqual(results[0][0], 200)


if __name__ == "__main__":
    unittest.main()
