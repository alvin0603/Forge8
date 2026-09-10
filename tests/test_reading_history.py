"""Owned, model-free tests for bounded same-snapshot reading history."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from forge8 import desk as desk_module
from test_desk import DeskFixture


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        allow_nan=False).encode("utf-8")


class ReadingHistoryTests(DeskFixture):
    def result(self, *, accepted=True):
        return {"kind": "forge8.explain", "ok": accepted,
            "status": "answered" if accepted else "stalled",
            "outcome": {"ok": accepted, "status": "answered" if accepted else "stalled",
                "answer": {"claims": [{"text": "The source assigns seven.", "citations": []}]} if accepted else None,
                "acceptance": {"ok": True}, "source_unchanged": True, "snapshot_unchanged": True},
            "server": {"start": {"ok": True, "pid": 123},
                "shutdown": {"ok": True, "status": "terminated", "return_code": 0},
                "supervisor_secret_cleared": True, "transport_secret_cleared": True}}

    def finish(self, value=None, *, question="Explain value.", kind="explain", cancel=False, error=False, preview=""):
        value = self.result() if value is None else value

        def run(_args, **hooks):
            if "on_text" in hooks and preview:
                hooks["on_text"](preview)
            hooks["on_result"](value)
            if cancel:
                hooks["cancel_event"].set()
            if error:
                raise RuntimeError("private exception must not enter history")

        payload = {**self.question(), "question": question}
        if kind != "explain":
            payload.pop("focus")
        runner_name = {"explain": "_run_explain_cli", "locate": "_run_locate_cli", "project": "_run_project_cli"}[kind]
        with patch.object(desk_module, runner_name, side_effect=run) as runner:
            identifier = self.desk.start(payload, kind=kind)["id"]
            self.desk.worker.join(5)
            self.assertFalse(self.desk.worker.is_alive())
        runner.assert_called_once()
        return identifier

    def test_empty_and_repeated_fetch_are_model_free_and_do_not_read_or_write_files(self):
        initial = self.desk.status()
        with patch.object(desk_module, "_run_explain_cli") as explain, \
                patch.object(desk_module, "_run_locate_cli") as locate, \
                patch.object(desk_module, "_run_project_cli") as project, \
                patch.object(Path, "read_bytes", side_effect=AssertionError("disk read")), \
                patch.object(Path, "write_bytes", side_effect=AssertionError("disk write")):
            for _ in range(2):
                self.assertEqual(self.desk.history(), {"version": self.desk.project["version"], "entries": [], "evicted": 0})
            explain.assert_not_called(); locate.assert_not_called(); project.assert_not_called()
        self.assertEqual(self.desk.status(), initial)

    def test_finished_jobs_are_ordered_with_original_question_and_frozen_elapsed(self):
        questions = ("  第一題\n保持原樣。  ", "Next question.")
        identifiers = [self.finish(question=question) for question in questions]
        entries = self.desk.history()["entries"]
        self.assertEqual([item["id"] for item in entries], identifiers)
        self.assertEqual([item["question"] for item in entries], list(questions))
        self.assertEqual(entries[-1]["elapsed_seconds"], self.desk.status()["elapsed_seconds"])
        for item in entries:
            self.assertEqual(item["version"], self.desk.project["version"])
            self.assertEqual((item["status"], item["reader"], item["gpu"]), ("answered", "qwen35", "released"))
            self.assertGreaterEqual(item["elapsed_seconds"], 0)
        with patch.object(desk_module.time, "monotonic", return_value=10**12):
            self.assertEqual(self.desk.history()["entries"], entries)

    def test_stored_bytes_and_return_values_do_not_alias_live_results_or_source_catalogue(self):
        value = self.result()
        self.finish(value)
        original = self.desk.history()
        value["outcome"]["answer"]["claims"][0]["text"] = "mutated live result"
        self.desk.job["files"][0]["path"] = "mutated.py"
        self.assertEqual(self.desk.history(), original)
        returned = self.desk.history()
        returned["entries"][0]["result"]["outcome"]["answer"]["claims"].clear()
        returned["entries"][0]["files"][0]["path"] = "caller mutation"
        returned["entries"].clear()
        self.assertEqual(self.desk.history(), original)

    def test_running_result_is_not_archived_and_cancelling_second_job_keeps_first(self):
        first = self.finish(question="First question.")
        entered, release = threading.Event(), threading.Event()

        def run(_args, **hooks):
            hooks["on_text"]("never archive this live draft")
            hooks["on_result"](self.result(accepted=False))
            entered.set()
            self.assertTrue(release.wait(3))

        with patch.object(desk_module, "_run_explain_cli", side_effect=run) as runner:
            second = self.desk.start({**self.question(), "question": "Second question."})["id"]
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual([item["id"] for item in self.desk.history()["entries"]], [first])
                self.assertNotIn("result", self.desk.status())
                self.assertEqual(self.desk.status()["id"], second)
                with self.assertRaisesRegex(ValueError, "unknown reading job"):
                    self.desk.cancel(first)
                self.desk.cancel(second)
                self.assertTrue(self.desk.cancel_event.is_set())
                self.assertEqual(len(self.desk.history()["entries"]), 1)
            finally:
                release.set(); self.desk.worker.join(5)
        runner.assert_called_once()
        self.assertFalse(self.desk.worker.is_alive())
        entries = self.desk.history()["entries"]
        self.assertEqual([item["id"] for item in entries], [first, second])
        self.assertEqual(entries[1]["status"], "cancelled")
        self.assertNotIn("result", entries[1])
        self.assertNotIn("never archive this live draft", encoded(entries).decode())

    def test_complete_unverified_output_survives_but_transient_rejected_preview_does_not(self):
        value = self.result(accepted=False)
        value["outcome"]["unverified_prose"] = "完整但未驗證 [E1-L3]"
        self.finish(value, preview="different streamed draft")
        self.finish(self.result(accepted=False), preview="transient rejected excerpt")
        self.assertIn("rejected_preview", self.desk.status())
        entries = self.desk.history()["entries"]
        self.assertEqual(entries[0]["result"]["outcome"]["unverified_prose"], value["outcome"]["unverified_prose"])
        self.assertIsNone(entries[0]["result"]["outcome"]["answer"])
        for item in entries:
            self.assertNotIn("preview", item)
            self.assertNotIn("rejected_preview", item)
        self.assertNotIn("transient rejected excerpt", encoded(entries).decode())

    def test_cancel_error_unknown_cleanup_and_setup_failure_keep_only_metadata(self):
        for fault in ("cancel", "error", "cleanup", "setup"):
            with self.subTest(fault=fault):
                value = self.result(accepted=False)
                value["outcome"]["unverified_prose"] = "must not survive"
                if fault == "cleanup": value["server"]["shutdown"]["return_code"] = None
                if fault == "setup": value.pop("server")
                self.finish(value, cancel=fault == "cancel", error=fault == "error", preview="draft")
                entry = self.desk.history()["entries"][-1]
                self.assertNotIn("result", entry)
                self.assertNotIn("files", entry)
                self.assertNotIn("must not survive", encoded(entry).decode())
                self.assertNotIn("preview", entry)
                self.assertNotIn("rejected_preview", entry)
                if fault == "error":
                    self.assertIn("RuntimeError", entry["error"])
                    self.assertNotIn("private exception", entry["error"])

    def test_existing_source_and_unverified_gates_are_not_bypassed(self):
        for flag in ("source_unchanged", "snapshot_unchanged"):
            with self.subTest(flag=flag):
                value = self.result(accepted=False)
                value["outcome"][flag] = False
                value["outcome"]["unverified_prose"] = "rejected by existing gate"
                self.finish(value, preview="rejected draft")
                entry = self.desk.history()["entries"][-1]
                self.assertNotIn("unverified_prose", entry["result"]["outcome"])
                self.assertNotIn("preview", entry)
                self.assertNotIn("rejected_preview", entry)

    def test_located_result_and_project_source_scope_preserve_original_kind(self):
        value = self.result()
        value.update(kind="forge8.locate", status="located")
        value["outcome"].update(status="located", ingress_unchanged=True,
            candidates=[{"path": "main.py", "start_line": 1, "end_line": 2}])
        self.finish(value, kind="locate")
        self.assertEqual(self.desk.history()["entries"][-1]["kind"], "locate")
        self.assertEqual(self.desk.history()["entries"][-1]["result"], value)
        value = self.result()
        value["project_reading"] = {"answer_attempted": True,
            "focus": [{"path": "main.py", "start_line": 1, "end_line": 2}],
            "discovery": {"ok": True, "status": "located", "snapshot_sha256": self.desk.project["version"],
                "source_unchanged": True, "snapshot_unchanged": True, "ingress_unchanged": True,
                "acceptance": {"ok": True}, "candidates": []}}
        self.finish(value, kind="project")
        self.assertEqual(self.desk.history()["entries"][-1]["kind"], "project")
        self.assertEqual(self.desk.history()["entries"][-1]["result"], value)

    def test_count_bound_evicts_oldest_and_fetch_never_increments_counter(self):
        identifiers = [self.finish(question=f"Question {index}") for index in range(7)]
        expected = self.desk.history()
        self.assertEqual([item["id"] for item in expected["entries"]], identifiers[-5:])
        self.assertEqual(expected["evicted"], 2)
        self.assertEqual(self.desk.history(), expected)

    def test_utf8_byte_bound_includes_json_array_punctuation(self):
        value = self.result()
        value["outcome"]["answer"]["claims"][0]["text"] = "字" * 85_000
        identifiers = [self.finish(value, question=f"Question {index}") for index in range(3)]
        history = self.desk.history()
        self.assertEqual([item["id"] for item in history["entries"]], identifiers[-2:])
        self.assertEqual(history["evicted"], 1)
        self.assertLessEqual(len(encoded(history["entries"])), 512 * 1024)
        self.assertGreater(len(encoded(history["entries"])), 500_000)

    def test_single_entry_exact_byte_boundary_counts_both_brackets(self):
        self.finish()
        expected = self.desk.history()["entries"]
        size = len(encoded(expected))
        for limit, kept in ((size, True), (size - 1, False)):
            with self.subTest(limit=limit):
                self.desk._history.clear()
                self.desk._history_evicted = 0
                with self.desk.lock, patch.object(desk_module, "_HISTORY_BYTES", limit):
                    self.desk._remember_finished_job()
                self.assertEqual(self.desk.history()["entries"], expected if kept else [])
                self.assertEqual(self.desk.history()["evicted"], 0 if kept else 1)

    def test_single_oversize_entry_is_skipped_whole_without_evicting_earlier_answer(self):
        first = self.finish()
        value = self.result()
        value["outcome"]["answer"]["claims"][0]["text"] = "字" * 180_000
        latest = self.finish(value)
        history = self.desk.history()
        self.assertEqual([item["id"] for item in history["entries"]], [first])
        self.assertEqual(history["evicted"], 1)
        self.assertEqual(self.desk.status()["id"], latest)
        self.assertEqual(self.desk.status()["result"], value)

    def test_unserializable_history_cannot_break_finished_live_job(self):
        value = self.result()
        value["unexpected"] = {"not-json"}
        self.finish(value)
        self.assertEqual(self.desk.status()["status"], "answered")
        self.assertIsNotNone(self.desk.finished)
        self.assertEqual(self.desk.history()["entries"], [])
        self.assertEqual(self.desk.history()["evicted"], 1)

    def test_failed_refresh_keeps_history_and_successful_refresh_clears_even_same_version(self):
        for index in range(6): self.finish(question=f"Question {index}")
        before = self.desk.history()
        with patch.object(desk_module, "prepare_repository_snapshot", side_effect=ValueError("unsafe source")):
            with self.assertRaisesRegex(ValueError, "unsafe source"):
                self.desk.refresh()
        self.assertEqual(self.desk.history(), before)
        self.desk.refresh()
        self.assertEqual(self.desk.history(), {"version": before["version"], "entries": [], "evicted": 0})

    def test_source_refresh_does_not_replay_previous_version(self):
        self.finish()
        previous = self.desk.history()
        (self.source / "main.py").write_text("value = 8\n", encoding="utf-8")
        self.desk.refresh()
        self.assertNotEqual(self.desk.project["version"], previous["version"])
        self.assertEqual(self.desk.history()["entries"], [])
        # Defense in depth: even an accidentally retained internal entry may not
        # be served as if its source version were the refreshed source.
        self.desk._history.append(encoded(previous["entries"][0]))
        self.assertEqual(self.desk.history()["entries"], [])


class ReadingHistoryHTTPTests(DeskFixture):
    def setUp(self):
        super().setUp()
        self.server, url = desk_module.make_server(self.desk)
        self.origin = url.split("/#", 1)[0]
        self.token = parse_qs(urlsplit(url).fragment)["token"][0]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)

    def request(self, *, headers=None, auth=True):
        fields = {"Authorization": "Bearer " + self.token} if auth else {}
        fields.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", "/api/history", headers=fields)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_history_requires_existing_token_host_and_origin_policy(self):
        for fields, auth, expected in (({}, False, 401), ({"Host": "evil.invalid"}, True, 403),
                ({"Origin": "https://evil.invalid"}, True, 403), ({"Authorization": "Bearer wrong"}, True, 401)):
            with self.subTest(fields=fields, auth=auth):
                status, _, body = self.request(headers=fields, auth=auth)
                self.assertEqual(status, expected)
                self.assertNotIn(b"entries", body)
        with patch.object(desk_module, "_run_explain_cli") as explain, \
                patch.object(desk_module, "_run_locate_cli") as locate, \
                patch.object(desk_module, "_run_project_cli") as project:
            for fields in ({}, {"Origin": self.origin}):
                status, headers, body = self.request(headers=fields)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), self.desk.history())
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            explain.assert_not_called(); locate.assert_not_called(); project.assert_not_called()

    def test_http_returns_independent_body_and_uses_existing_error_handling(self):
        with patch.object(self.desk, "history", side_effect=ValueError("history unavailable")):
            status, _, body = self.request()
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "history unavailable"})
        status, _, body = self.request()
        self.assertEqual(status, 200)
        result = json.loads(body)
        result["entries"].append({"forged": True})
        self.assertEqual(self.desk.history()["entries"], [])
