"""Owned static-context integration; no model or reading-source execution."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from forge8 import comparison, desk as desk_module
from forge8.git_source import GitBaseline
import test_desk as fixtures


class ContextDeskTests(fixtures.DeskFixture):
    def setUp(self):
        super().setUp()
        self.text = 'DEFAULT = {"label": "unknown"}\n\ndef get_record(cache, key):\n    return cache.get(key, DEFAULT)\n'
        (self.source / "main.py").write_bytes(self.text.encode())
        self.desk.refresh()

    def selection(self, path="main.py", first=3, last=4):
        item = next(item for item in self.desk.project["files"] if item["path"] == path)
        return {"file": item["id"], "version": self.desk.project["version"], "start": first, "end": last}

    def test_declared_constant_retains_coordinates_without_model_or_state_mutation(self):
        job, history, project = self.desk.status(), self.desk.history(), self.desk.project
        payload = self.selection()
        with patch.object(desk_module, "_run_explain_cli") as model:
            result = self.desk.context(payload)
        model.assert_not_called()
        self.assertIsNone(self.desk.worker)
        self.assertEqual(result["status"], "available")
        self.assertFalse(result["semantics_verified"])
        self.assertEqual(result["scope"], "same-file lexical scopes")
        self.assertEqual({key: result[key] for key in payload}, payload)
        self.assertEqual(result["path"], "main.py")
        self.assertEqual([(row["name"], row["start_line"], row["end_line"], row["use_lines"])
            for row in result["candidates"] if row["name"] == "DEFAULT"], [("DEFAULT", 1, 1, [4])])
        self.assertEqual({row["name"]: row["classification"] for row in result["bindings"]},
            {"cache": "parameter", "key": "parameter", "DEFAULT": "module"})
        self.assertEqual({row["name"] for row in result["candidates"] if row["kind"] == "parameter"},
            {"cache", "key"})
        self.assertEqual((self.desk.status(), self.desk.history()), (job, history))
        self.assertIs(self.desk.project, project)

    def test_live_edits_do_not_replace_the_retained_source_being_inspected(self):
        expected = self.desk.context(self.selection())
        (self.source / "main.py").write_text("raise RuntimeError('must never execute')\n", encoding="utf-8")
        with patch.object(desk_module, "_read_regular_file", wraps=desk_module._read_regular_file) as reader:
            actual = self.desk.context(self.selection())
        self.assertEqual(actual, expected)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(reader.call_args.args[0], Path(self.desk.browse_snapshot.snapshot_root) / "main.py")

    def test_tampered_retained_bytes_are_rejected_without_replacing_project(self):
        path = Path(self.desk.browse_snapshot.snapshot_root) / "main.py"
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
        path.write_bytes(self.text.replace("unknown", "CHANGED").encode())
        project = self.desk.project
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            self.desk.context(self.selection())
        self.assertIs(self.desk.project, project)
        self.assertIsNone(self.desk.worker)

    def test_invalid_requests_and_stale_versions_never_load_a_model(self):
        valid = self.selection()
        invalid = [None, [], {}, {**valid, "extra": True}, {**valid, "file": "../main.py"},
            {**valid, "version": "stale"}, {**valid, "start": True}, {**valid, "end": 81},
            {**valid, "start": 0}, {**valid, "start": 4, "end": 3}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.desk.context(payload)
        (self.source / "main.py").write_bytes((self.text + "# saved change\n").encode())
        self.desk.refresh()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.desk.context(valid)
        self.desk.close()
        with self.assertRaisesRegex(ValueError, "closing"):
            self.desk.context(self.selection())
        self.assertIsNone(self.desk.worker)

    def test_visible_origins_and_unsupported_language_do_not_claim_complete_context(self):
        visible = self.desk.context(self.selection(first=1, last=4))
        self.assertEqual({row["name"] for row in visible["candidates"]}, {"DEFAULT", "cache", "key"})
        self.assertTrue(all(1 <= row["start_line"] <= row["end_line"] <= 4
            for row in visible["candidates"]))
        self.assertFalse(visible["semantics_verified"])
        (self.source / "main.cpp").write_text("int answer() { return 42; }\n", encoding="utf-8")
        self.desk.refresh()
        result = self.desk.context(self.selection("main.cpp", 1, 1))
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["semantics_verified"])

    def test_comparison_roles_inspect_their_own_retained_version(self):
        self.desk.close()
        state = patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.root / "state")})
        state.start()
        self.addCleanup(state.stop)
        self.desk = desk_module.ReadingDesk(self.source)
        self.addCleanup(self.desk.close)
        before = self.text.replace('"unknown"', '"before-value"').replace("cache.get(key, DEFAULT)", "cache.get(key) or DEFAULT")
        with patch.object(comparison, "read_head", return_value=GitBaseline("a" * 40, {"main.py": before.encode()}, ())), \
                patch.object(comparison, "head_identity", return_value="a" * 40):
            self.desk.refresh(mode="changes")
            for side, expected in (("before", "before-value"), ("after", "unknown")):
                payload = self.selection(side + "/main.py")
                result = self.desk.context(payload)
                self.assertEqual(result["path"], side + "/main.py")
                self.assertEqual(result["candidates"][0]["start_line"], 1)
                view = self.desk.source_view(payload["file"], payload["version"])
                self.assertIn(expected, view["lines"][0])
        self.assertIsNone(self.desk.worker)


class ContextHTTPTests(fixtures.DeskFixture):
    request = fixtures.ReadingHTTPTests.request
    close_server = fixtures.ReadingHTTPTests.close_server

    def setUp(self):
        super().setUp()
        (self.source / "main.py").write_text("VALUE = 7\ndef read():\n    return VALUE\n", encoding="utf-8")
        self.desk.refresh()
        self.server, url = desk_module.make_server(self.desk)
        self.origin = url.split("/#", 1)[0]
        self.token = parse_qs(urlsplit(url).fragment)["token"][0]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.close_server)

    def test_context_route_obeys_existing_auth_origin_and_no_store(self):
        payload = {"file": "0", "version": self.desk.project["version"], "start": 2, "end": 3}
        for headers, auth, expected in (({}, False, 401), ({"Host": "evil.invalid"}, True, 403),
                ({"Origin": "https://evil.invalid"}, True, 403)):
            with self.subTest(headers=headers, auth=auth):
                status, _, body = self.request("/api/context", method="POST", payload=payload,
                    headers=headers, auth=auth, headers_only=True)
                self.assertEqual(status, expected)
                self.assertNotIn(b"VALUE", body)
        status, headers, body = self.request("/api/context", method="POST", payload=payload)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(body)["candidates"][0]["name"], "VALUE")
        self.assertEqual(self.request("/api/context", method="POST", payload={**payload, "version": "stale"})[0], 400)
        self.assertIsNone(self.desk.worker)
