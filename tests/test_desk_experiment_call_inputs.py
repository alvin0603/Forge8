"""Static retained-call conversion; owned strings only, never guest/model execution."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import http.client
import json
import os
from pathlib import Path
import stat
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from forge8 import desk as desk_module, experiments
import test_desk as fixtures


class _CallInputFixture(fixtures.DeskFixture):
    def setUp(self):
        super().setUp()
        self.desk.close()
        self.code = (
            '# Owned static fixture; never execute.\r\n'
            "raise RuntimeError('must not execute')\r\n"
            'def entry(value, *, label):\r\n    return value, label\r\n\r\n'
            'entry(\r\n    9007199254740993,\r\n'
            '    label={"text": "中文x\u200by\\n\\\"quoted\\\"", "missing": None},\r\n)\r\n'
        ).encode("utf-8")
        self.live = self.source / "main.py"
        self.live.write_bytes(self.code)
        (self.source / "caller.py").write_bytes(b'entry(9007199254740993, label="caller")\r\n')
        (self.source / "boundary.py").write_bytes(b"entry(1)\n" + b"# owned comment\n" * 80)
        (self.source / "dynamic.py").write_bytes(b"entry(value)\n")
        (self.source / "multiple.py").write_bytes(b"entry(1)\nentry(2)\n")
        (self.source / "main.cpp").write_bytes(b"entry(1);\n")
        (self.source / "nonphysical.py").write_bytes('# comment\u2028entry(1)\n'.encode("utf-8"))
        state = patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.root / "state")})
        state.start()
        self.addCleanup(state.stop)
        self.desk = desk_module.ReadingDesk(self.source, allow_experiments=True)
        self.addCleanup(self.desk.close)
        self.guards = []
        for target, name in ((experiments, "run_experiment"), (experiments, "_worker"),
                (experiments, "prepare_module"), (experiments, "prepare_module_set"),
                (experiments, "native_platform"), (desk_module, "_run_explain_cli"),
                (desk_module, "_run_locate_cli"), (desk_module, "_run_project_cli")):
            guard = patch.object(target, name, side_effect=AssertionError("no target/runtime/operation allowed"))
            self.guards.append(guard.start())
            self.addCleanup(guard.stop)
        spawn = patch("subprocess.Popen", side_effect=AssertionError("no process allowed"))
        self.guards.append(spawn.start())
        self.addCleanup(spawn.stop)
        self.desk.job = {"id": "prior-reading", "status": "answered", "question": "保留問題",
            "version": self.desk.project["version"], "reader": "qwen35", "gpu": "released",
            "result": {"answer": "Owned prior answer"}}
        self.desk.started, self.desk.finished = 1.0, 2.0
        with self.desk.lock:
            self.desk._remember_finished_job()
        self.desk.experiment = {**self.selection(), "id": "prior-trial", "status": "completed",
            "path": "main.py", "source_sha256": hashlib.sha256(self.code).hexdigest(),
            "input_text": ' {"args": [42], "kwargs": {}}\n', "result_text": '{"return": 42}'}

    def selection(self, path="main.py", start=6, end=9, *, desk=None):
        desk = self.desk if desk is None else desk
        item = next(item for item in desk.project["files"] if item["path"] == path)
        return {"file": item["id"], "version": desk.project["version"],
            "start": start, "end": end, "entry": "entry"}

    def retained_state(self):
        return deepcopy((self.desk.status(), self.desk.history(), self.desk.project,
            self.desk.contents, self.desk.experiment_status()))

    def assert_no_operations(self):
        for guard in self.guards:
            guard.assert_not_called()
        self.assertIsNone(self.desk.worker)
        self.assertIsNone(self.desk.experiment_worker)


class DeskExperimentCallInputTests(_CallInputFixture):
    def test_raw_source_and_bigint_conversion_preserve_reading_trial_and_artifacts(self):
        before, artifacts = self.retained_state(), set(self.desk.root.rglob("*"))
        payload = self.selection()
        view = self.desk.source_view(payload["file"], payload["version"])
        self.assertIn("x\\u200by", view["lines"][7])
        with patch.object(experiments, "prepare_call_inputs", wraps=experiments.prepare_call_inputs) as convert, \
                patch.object(self.desk, "_busy", side_effect=AssertionError("static conversion needs no CPU slot")):
            result = self.desk.prepare_experiment_input(payload)
        convert.assert_called_once_with(self.code, "entry", 6, 9)
        expected = {"args": [9007199254740993],
            "kwargs": {"label": {"text": '中文x\u200by\n"quoted"', "missing": None}}}
        self.assertEqual(result, {**payload, "call_start": 6, "call_end": 9,
            "input_text": json.dumps(expected, ensure_ascii=False, allow_nan=False, indent=2),
            "path": "main.py", "source_sha256": hashlib.sha256(self.code).hexdigest()})
        self.assertEqual(json.loads(result["input_text"]), expected)
        self.assertIn("9007199254740993", result["input_text"])
        self.assertEqual(self.retained_state(), before)
        self.assertEqual(set(self.desk.root.rglob("*")), artifacts)
        self.assert_no_operations()

    def test_other_file_call_needs_no_definition_and_live_edits_do_not_replace_snapshot(self):
        payload = self.selection("caller.py", 1, 1)
        expected = self.desk.prepare_experiment_input(payload)
        (self.source / "caller.py").write_bytes(b"entry('LIVE changed')\n")
        before = self.retained_state()
        with patch.object(desk_module, "_read_regular_file", wraps=desk_module._read_regular_file) as reader:
            actual = self.desk.prepare_experiment_input(payload)
        self.assertEqual(actual, expected)
        self.assertEqual(reader.call_args.args[0], Path(self.desk.browse_snapshot.snapshot_root) / "caller.py")
        self.assertEqual(json.loads(actual["input_text"]), {"args": [9007199254740993], "kwargs": {"label": "caller"}})
        self.assertEqual(self.retained_state(), before)
        self.assert_no_operations()

    def test_invalid_exact_payload_types_and_ranges_preserve_every_existing_state(self):
        valid = self.selection()
        invalid = [None, [], {}, {**valid, "extra": True},
            {key: value for key, value in valid.items() if key != "entry"},
            {**valid, "file": "../main.py"}, {**valid, "file": 0}, {**valid, "file": []},
            {**valid, "version": "stale"}, {**valid, "version": []},
            {**valid, "start": True}, {**valid, "end": 9.0}, {**valid, "start": 0},
            {**valid, "start": 9, "end": 6}, {**valid, "end": 10}]
        invalid.extend({**valid, "entry": entry} for entry in (None, True, 1, [], "", "x" * 201, "entry()", "owner.entry"))
        before, artifacts = self.retained_state(), set(self.desk.root.rglob("*"))
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.desk.prepare_experiment_input(payload)
        self.assertEqual(self.retained_state(), before)
        self.assertEqual(set(self.desk.root.rglob("*")), artifacts)
        self.assert_no_operations()

    def test_literal_refusals_and_selection_boundary_do_not_change_the_draft(self):
        before = self.retained_state()
        for payload in (self.selection("dynamic.py", 1, 1), self.selection("multiple.py", 1, 2),
                        self.selection(start=6, end=8), self.selection("boundary.py", 1, 81)):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.desk.prepare_experiment_input(payload)
        result = self.desk.prepare_experiment_input(self.selection("boundary.py", 1, 80))
        self.assertEqual((result["start"], result["end"], result["call_start"], result["call_end"]), (1, 80, 1, 1))
        self.assertEqual(json.loads(result["input_text"]), {"args": [1], "kwargs": {}})
        self.assertEqual(self.retained_state(), before)
        self.assert_no_operations()

    def test_changed_retained_bytes_are_rejected_before_conversion(self):
        path = Path(self.desk.browse_snapshot.snapshot_root) / "main.py"
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        path.write_bytes(self.code.replace(b"9007199254740993", b"9007199254740994"))
        before = self.retained_state()
        with patch.object(experiments, "prepare_call_inputs") as convert, self.assertRaisesRegex(ValueError, "snapshot changed"):
            self.desk.prepare_experiment_input(self.selection())
        convert.assert_not_called()
        self.assertEqual(self.retained_state(), before)
        self.assert_no_operations()

    def test_default_off_comparison_nonpython_nonphysical_stale_and_closed_reject(self):
        before = self.retained_state()
        with patch.object(experiments, "prepare_call_inputs") as convert:
            disabled = desk_module.ReadingDesk(self.source)
            self.addCleanup(disabled.close)
            with self.assertRaisesRegex(ValueError, "disabled"):
                disabled.prepare_experiment_input(self.selection(desk=disabled))
            with patch.object(self.desk, "comparison", object()), self.assertRaisesRegex(ValueError, "ordinary source mode"):
                self.desk.prepare_experiment_input(self.selection())
            for path in ("main.cpp", "nonphysical.py"):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "physical source lines"):
                    self.desk.prepare_experiment_input(self.selection(path, 1, 1))
            self.assertEqual(self.retained_state(), before)
            old = self.selection()
            self.live.write_bytes(self.code + b"# new saved version\n")
            self.desk.refresh()
            refreshed = self.retained_state()
            with self.assertRaisesRegex(ValueError, "stale"):
                self.desk.prepare_experiment_input(old)
            self.desk.close()
            with self.assertRaisesRegex(ValueError, "closing"):
                self.desk.prepare_experiment_input(self.selection())
            self.assertEqual(self.retained_state(), refreshed)
        convert.assert_not_called()
        self.assert_no_operations()


class DeskExperimentCallInputHTTPTests(_CallInputFixture):
    request = fixtures.ReadingHTTPTests.request
    close_server = fixtures.ReadingHTTPTests.close_server

    def setUp(self):
        super().setUp()
        self.server, url = desk_module.make_server(self.desk)
        self.origin = url.split("/#", 1)[0]
        self.token = parse_qs(urlsplit(url).fragment)["token"][0]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.close_server)

    def raw_request(self, body, *, headers_only=False):
        raw = body.encode("utf-8")
        fields = {"Authorization": "Bearer " + self.token, "Origin": self.origin, "Content-Type": "application/json"}
        if headers_only:
            fields["Content-Length"] = str(len(raw))
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("POST", "/api/experiment/input", None if headers_only else raw, fields)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_route_auth_strict_bounded_json_and_lossless_text_without_operations(self):
        payload, before = self.selection(), self.retained_state()
        with patch.object(self.desk, "prepare_experiment_input", side_effect=AssertionError("unauthorized dispatch")) as dispatch:
            for headers, auth, expected, message in (
                    ({}, False, 401, "open the private URL printed in your Forge8 terminal"),
                    ({"Host": "evil.invalid"}, True, 403, "invalid local host"),
                    ({"Origin": "https://evil.invalid"}, True, 403, "invalid local origin")):
                with self.subTest(headers=headers, auth=auth):
                    status, _, body = self.request("/api/experiment/input", method="POST", payload=payload,
                        headers=headers, auth=auth, headers_only=True)
                    self.assertEqual(status, expected)
                    self.assertEqual(json.loads(body), {"error": message})
                    self.assertNotIn(b"9007199254740993", body)
            dispatch.assert_not_called()
        status, headers, body = self.request("/api/experiment/input", method="POST", payload=payload)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        result = json.loads(body)
        self.assertIn("9007199254740993", result["input_text"])
        self.assertEqual(result["source_sha256"], hashlib.sha256(self.code).hexdigest())
        with patch.object(self.desk, "prepare_experiment_input") as dispatch:
            for raw in ('{"entry":"entry","entry":"different"}', '{"start":NaN}', '{"start":Infinity}'):
                with self.subTest(raw=raw[:60]):
                    self.assertEqual(self.raw_request(raw)[0], 400)
            self.assertEqual(self.raw_request(" " * 16_385, headers_only=True)[0], 400)
            dispatch.assert_not_called()
        self.assertEqual(self.request("/api/experiment/input", method="POST", payload={**payload, "start": 1e999})[0], 400)
        self.assertEqual(self.request("/api/experiment/input", method="POST", payload={**payload, "version": "stale"})[0], 400)
        self.assertEqual(self.retained_state(), before)
        self.assert_no_operations()
