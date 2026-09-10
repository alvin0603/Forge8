"""Synthetic imported reports only: never execute a source fixture or model."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge8 import cli, desk as desk_module


class ObservedDeskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="forge8-observed-desk-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source, self.assets = self.root / "project", self.root / "assets"
        self.source.mkdir()
        self.assets.mkdir()
        self.sources = {"main.py": "def run():\n    value = helper()\n    return value\n",
            "worker.py": "def helper():\n    value = 7\n    return value\n"}
        for name, text in self.sources.items():
            (self.source / name).write_bytes(text.encode())
        for target, name, value in ((cli, "_load_deployment", None),
                (cli, "_resolve_fix_asset_root", self.assets),
                (desk_module, "_resolve_fix_asset_root", self.assets)):
            mocked = patch.object(target, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.root / "observation.json"

    def report(self):
        pins = {p: hashlib.sha256((self.source / p).read_bytes()).hexdigest() for p in self.sources}
        counts = dict.fromkeys(("passed", "failed", "errors", "skipped", "expected_failures",
            "unexpected_successes", "subtests_passed", "subtests_failed", "subtests_errors"), 0)
        counts["passed"] = 1
        def event(kind, cid, parent, path, function, line):
            return {"event": kind, "call_id": cid, "parent_call_id": parent, "path": path,
                "function": function, "first_line": 1, "line": line}
        return {"schema_version": 1, "kind": "forge8.observation", "test": "main.Case.test_run",
            "python": "3.12.synthetic", "complete": True, "source_unchanged": True,
            "trace_restored": True, "truncated": False, "unsupported": False,
            "unsupported_calls": 0, "observer_error": False, "aborted": False,
            "trace_replaced": False, "unmatched_source_code": False, "method_observed": True,
            "test_passed": True, "tests_run": 1, "counts": counts, "error": None,
            "event_limit": 1000, "report_byte_limit": 128 * 1024,
            "source_before": pins, "source_after": dict(pins),
            "scope": "UNTRUSTED IMPORTED SCOPE MUST NOT BE SHOWN",
            "capture_process": {"status": "passed", "return_code": 0, "timed_out": False, "output_truncated": False},
            "events": [event("call", 1, None, "main.py", "run", 1),
                event("line", 1, None, "main.py", "run", 2),
                event("call", 2, 1, "worker.py", "helper", 0),
                event("line", 2, 1, "worker.py", "helper", 2),
                event("line", 2, 1, "worker.py", "helper", 2),
                event("exception", 2, 1, "worker.py", "helper", 2),
                event("line", 2, 1, "worker.py", "helper", 3),
                event("return", 2, 1, "worker.py", "helper", 3),
                event("line", 1, None, "main.py", "run", 3),
                event("return", 1, None, "main.py", "run", 3)]}

    def open_desk(self, report=None, *, path=None, raw=None):
        if path is None:
            path = self.path
            path.write_bytes(raw if raw is not None else json.dumps(report or self.report()).encode())
        desk = desk_module.ReadingDesk(self.source, observation=path)
        self.addCleanup(desk.close)
        return desk

    def assert_unavailable(self, report=None, *, raw=None, path=None):
        desk = self.open_desk(report, raw=raw, path=path)
        self.assertIsNone(desk.project["observation"])
        self.assertIsInstance(desk.project["observation_error"], str)
        self.assertEqual(desk.job["status"], "idle")
        self.assertTrue(desk.contents)

    def test_grouped_positions_bind_cached_ids_keep_order_and_do_not_run(self):
        with patch.object(desk_module, "_run_explain_cli") as inference, \
                patch("forge8.observation.capture_test") as capture:
            report = self.report()
            desk = self.open_desk(report)
            view = desk.project["observation"]
            self.assertEqual(view["test"], report["test"])
            self.assertEqual(view["event_count"], 10)
            self.assertIsNone(desk.project["observation_error"])
            self.assertNotEqual(view["scope"], report["scope"])
            self.assertIn("not authenticated", view["scope"])
            self.assertEqual(view["calls"][0]["visits"], [2, 3])
            self.assertIsNone(view["calls"][0]["parent_line"])
            self.assertEqual(view["calls"][1]["parent_line"], 2)
            self.assertEqual(view["calls"][1]["visits"], [2, 2, 3])
            self.assertEqual(view["calls"][1]["exceptions"], 1)
            self.assertTrue(all(c["returned"] for c in view["calls"]))
            self.assertEqual(view["calls"][1]["parent"], 1)
            call = view["calls"][1]
            source = desk.source_view(call["file"], desk.project["version"])
            self.assertEqual(source["path"], "worker.py")
            self.assertEqual(source["lines"][1], "    value = 7")
            inference.assert_not_called()
            capture.assert_not_called()

    def test_parent_position_is_absent_without_parent_line_event(self):
        report = self.report()
        report["events"].pop(1)
        view = self.open_desk(report).project["observation"]
        self.assertIsNone(view["calls"][0]["parent_line"])
        self.assertIsNone(view["calls"][1]["parent_line"])
        self.assertEqual(view["calls"][0]["visits"], [3])

    def test_plain_read_has_no_observation_and_never_loads_report(self):
        with patch.object(desk_module, "_load_observation") as load:
            desk = desk_module.ReadingDesk(self.source)
            self.addCleanup(desk.close)
            self.assertIsNone(desk.project["observation"])
            self.assertIsNone(desk.project["observation_error"])
            load.assert_not_called()

    def test_unchanged_refresh_does_not_reread_report_and_remaps_file_ids(self):
        desk = self.open_desk()
        original = desk.project["observation"]["calls"][0]["file"]
        self.path.write_bytes(b"not JSON anymore")
        (self.source / "a.py").write_bytes(b"# unrelated new file\n")
        with patch.object(desk_module, "_load_observation") as load:
            project = desk.refresh()
            load.assert_not_called()
        call = project["observation"]["calls"][0]
        self.assertNotEqual(call["file"], original)
        self.assertEqual(desk.source_view(call["file"], project["version"])["path"], call["path"])

    def test_changed_source_clears_observation_permanently(self):
        desk = self.open_desk()
        original = (self.source / "main.py").read_bytes()
        (self.source / "main.py").write_bytes(original + b"# changed\n")
        self.assertIsNone(desk.refresh()["observation"])
        self.assertTrue(desk.project["observation_error"])
        (self.source / "main.py").write_bytes(original)
        self.assertIsNone(desk.refresh()["observation"])
        self.assertTrue(desk.project["observation_error"])

    def test_removed_selected_source_clears_observation(self):
        desk = self.open_desk()
        (self.source / "worker.py").unlink()
        self.assertIsNone(desk.refresh()["observation"])
        self.assertTrue(desk.project["observation_error"])

    def test_complete_failed_or_skipped_test_is_not_renamed_passed(self):
        report = self.report()
        report["test_passed"] = False
        report["counts"].update(passed=0, failed=1)
        desk = self.open_desk(report)
        self.assertFalse(desk.project["observation"]["test_passed"])
        self.assertEqual(desk.project["observation"]["counts"]["failed"], 1)
        report["counts"].update(failed=0, skipped=1)
        report.update(method_observed=False, events=[])
        self.assertEqual(self.open_desk(report).project["observation"]["calls"], [])

    def test_incomplete_process_flags_and_counts_are_rejected(self):
        bad_fields = {"complete": False, "source_unchanged": False, "trace_restored": False,
            "truncated": True, "unsupported": True, "observer_error": True, "aborted": True,
            "trace_replaced": True, "unmatched_source_code": True, "method_observed": False,
            "unsupported_calls": 1, "test_passed": 1, "tests_run": 2, "event_limit": 1001,
            "report_byte_limit": 131073, "error": "private exception message"}
        for key, value in bad_fields.items():
            with self.subTest(key=key):
                report = self.report()
                report[key] = value
                self.assert_unavailable(report)
        for key, value in (("status", "failed"), ("return_code", 1), ("return_code", False),
                           ("timed_out", True), ("output_truncated", 1)):
            report = self.report()
            report["capture_process"][key] = value
            self.assert_unavailable(report)
        for value in (-1, True, "1", 2**31):
            report = self.report()
            report["counts"]["passed"] = value
            self.assert_unavailable(report)

    def test_strict_json_size_and_shape(self):
        for data in (b"null", b"[]", b"{}{}", b'{"x":1,"x":2}', b'{"x":NaN}', b"\xff",
                     b" " * (128 * 1024 + 1), b"[" * 2000 + b"]" * 2000):
            with self.subTest(size=len(data)):
                self.assert_unavailable(raw=data)

    def test_wrong_source_pins_removed_files_and_unsafe_lines_are_rejected(self):
        for change in ("sha", "after", "unknown", "separator"):
            report = self.report()
            if change == "sha":
                report["source_before"]["main.py"] = report["source_after"]["main.py"] = "0" * 64
            elif change == "after":
                report["source_after"]["main.py"] = "0" * 64
            elif change == "unknown":
                report["source_before"]["../secret.py"] = "0" * 64
                report["source_after"] = dict(report["source_before"])
            else:
                (self.source / "main.py").write_bytes("#\u2028\ndef run(): pass\n".encode())
                report = self.report()
            with self.subTest(change=change), patch.object(desk_module, "_OUTLINE_CACHE_BYTES", 1):
                self.assert_unavailable(report)

    def test_event_identity_ancestry_coordinates_and_payload_fields_are_rejected(self):
        for key, value in (("call_id", True), ("parent_call_id", 99), ("path", "../secret.py"),
                           ("path", []), ("function", "bad\u2028name"), ("function", "x" * 201),
                           ("first_line", 4), ("line", 4), ("line", 0), ("line", True),
                           ("event", "yield"), ("value", "private payload")):
            report = self.report()
            report["events"][3][key] = value
            with self.subTest(key=key, value=value):
                self.assert_unavailable(report)
        for index in (0, 2, 7, 9):
            report = self.report()
            report["events"].pop(index)
            self.assert_unavailable(report)
        report = self.report()
        report["events"][6]["function"] = "another_function"
        self.assert_unavailable(report)
        report = self.report()
        report["events"] *= 101
        self.assert_unavailable(report)

    def test_report_links_and_directory_are_not_read(self):
        self.path.write_text(json.dumps(self.report()))
        directory = self.root / "directory.json"
        directory.mkdir()
        self.assert_unavailable(path=directory)
        alias = self.root / "alias.json"
        os.link(self.path, alias)
        with patch.object(desk_module, "_read_regular_file") as read:
            self.assert_unavailable(path=alias)
            read.assert_not_called()

    def test_report_symlink_is_not_read(self):
        self.path.write_text(json.dumps(self.report()))
        alias = self.root / "symlink.json"
        try:
            alias.symlink_to(self.path)
        except OSError as exc:
            self.skipTest("Symlink privilege unavailable: " + type(exc).__name__)
        with patch.object(desk_module, "_read_regular_file") as read:
            self.assert_unavailable(path=alias)
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
