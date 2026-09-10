"""Retained-snapshot import navigation; never execute imported project code."""
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


class ImportDeskTests(fixtures.DeskFixture):
    def setUp(self):
        super().setUp()
        self.texts = {
            "main.py": "from pkg import public as helper\ndef run():\n    return helper()\n",
            "src/pkg/__init__.py": "from .impl import actual as public\n",
            "src/pkg/impl.py": "raise RuntimeError('NEVER IMPORT THIS')\ndef actual():\n    return 7\n",
        }
        for name, text in self.texts.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode())
        self.desk.refresh()

    def request_for(self, path="main.py"):
        item = next(row for row in self.desk.project["files"] if row["path"] == path)
        selection = {"file": item["id"], "version": self.desk.project["version"], "start": 2, "end": 3}
        result = self.desk.context(selection)
        candidate = next(row for row in result["candidates"] if row["name"] == "helper")
        return {**selection, "binding_id": candidate["binding_id"],
            "declaration_start": candidate["start_line"], "declaration_end": candidate["end_line"]}

    def test_reexport_chain_uses_exact_retained_files_without_model_or_mutation(self):
        request = self.request_for()
        before = json.loads(json.dumps({"project": self.desk.project, "job": self.desk.status(), "history": self.desk.history()}))
        with patch.object(desk_module, "_run_explain_cli") as model:
            result = self.desk.import_source(request)
        model.assert_not_called()
        self.assertEqual(result["status"], "available")
        self.assertFalse(result["semantics_verified"])
        self.assertEqual({key: result[key] for key in request}, request)
        self.assertEqual(len(result["routes"]), 1)
        route = result["routes"][0]
        self.assertEqual(route["outcome"], "declaration")
        self.assertEqual([(step["path"], step["name"], step["start_line"], step["end_line"])
            for step in route["steps"]], [("main.py", "helper", 1, 1),
                ("src/pkg/__init__.py", "public", 1, 1), ("src/pkg/impl.py", "actual", 2, 3)])
        for step in route["steps"]:
            self.assertEqual(self.desk.source_view(step["file"], request["version"])["path"], step["path"])
        self.assertEqual({"project": self.desk.project, "job": self.desk.status(), "history": self.desk.history()}, before)
        self.assertIsNone(self.desk.worker)
        self.assertEqual(self.desk.model_status(), {"enabled": False})

    def test_live_source_edits_cannot_substitute_imported_snapshot_bytes(self):
        request = self.request_for()
        expected = self.desk.import_source(request)
        (self.source / "src/pkg/impl.py").write_text("def wrong():\n    return 99\n", encoding="utf-8")
        self.assertEqual(self.desk.import_source(request), expected)

    def test_tampered_target_or_reexport_refuses_entire_result(self):
        for name in ("src/pkg/__init__.py", "src/pkg/impl.py"):
            with self.subTest(name=name):
                path = Path(self.desk.browse_snapshot.snapshot_root) / name
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
                path.write_bytes(b"pass\n")
                with self.assertRaisesRegex(ValueError, "snapshot changed"):
                    self.desk.import_source(self.request_for())
                self.desk.refresh()

    def test_invalid_stale_and_forged_declarations_are_not_navigation_requests(self):
        request = self.request_for()
        invalid = [None, {}, {**request, "target": "src/pkg/impl.py"},
            {**request, "declaration_start": True}, {**request, "declaration_end": 3},
            {**request, "binding_id": "B64"}, {**request, "version": "stale"},
            {**request, "file": "../main.py"}, {**request, "start": False},
            {**request, "binding_id": []}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.desk.import_source(value)
        (self.source / "main.py").write_text(self.texts["main.py"] + "# new snapshot\n", encoding="utf-8")
        self.desk.refresh()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.desk.import_source(request)
        request = self.request_for()
        self.desk.close()
        with self.assertRaisesRegex(ValueError, "closing"):
            self.desk.import_source(request)

    def test_second_integrity_read_detects_change_during_multi_file_analysis(self):
        request = self.request_for()
        original = desk_module.trace_import

        def changed(*args):
            result = original(*args)
            path = Path(self.desk.browse_snapshot.snapshot_root) / "src/pkg/impl.py"
            path.chmod(stat.S_IREAD | stat.S_IWRITE)
            path.write_bytes(b"pass\n")
            return result

        with patch.object(desk_module, "trace_import", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "snapshot changed"):
                self.desk.import_source(request)

    def test_comparison_keeps_head_and_current_targets_separate_without_fallback(self):
        self.desk.close()
        with patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.root / "state")}):
            self.desk = desk_module.ReadingDesk(self.source)
        self.addCleanup(self.desk.close)
        before = {name: text.encode() for name, text in self.texts.items()}
        before["src/pkg/impl.py"] = b"# HEAD\n# different coordinates\ndef actual():\n    return 3\n"
        with patch.object(comparison, "read_head", return_value=GitBaseline("a" * 40, before, ())), \
                patch.object(comparison, "head_identity", return_value="a" * 40):
            self.desk.refresh(mode="changes")
        for side, first in (("before", 3), ("after", 2)):
            result = self.desk.import_source(self.request_for(side + "/main.py"))
            steps = result["routes"][0]["steps"]
            self.assertTrue(all(row["path"].startswith(side + "/") for row in steps))
            self.assertEqual(steps[-1]["start_line"], first)
        del before["src/pkg/impl.py"]
        with patch.object(comparison, "read_head", return_value=GitBaseline("b" * 40, before, ())), \
                patch.object(comparison, "head_identity", return_value="b" * 40):
            self.desk.refresh(mode="changes")
        result = self.desk.import_source(self.request_for("before/main.py"))
        self.assertTrue(all(route["outcome"] == "unresolved" for route in result["routes"]))
        self.assertTrue(all(step["path"].startswith("before/") for route in result["routes"] for step in route["steps"]))

    request = fixtures.ReadingHTTPTests.request
    close_server = fixtures.ReadingHTTPTests.close_server

    def test_route_uses_existing_auth_origin_and_bounded_payload(self):
        self.server, url = desk_module.make_server(self.desk)
        self.origin = url.split("/#", 1)[0]
        self.token = parse_qs(urlsplit(url).fragment)["token"][0]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.close_server)
        payload = self.request_for()
        for headers, auth, status in (({}, False, 401), ({"Host": "evil.invalid"}, True, 403),
                ({"Origin": "https://evil.invalid"}, True, 403)):
            actual, _, data = self.request("/api/import-source", method="POST", payload=payload, headers=headers, auth=auth)
            self.assertEqual(actual, status)
            self.assertNotIn(b"impl.py", data)
        status, headers, raw = self.request("/api/import-source", method="POST", payload=payload)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(raw)["routes"][0]["steps"][-1]["path"], "src/pkg/impl.py")
        self.assertEqual(self.request("/api/import-source", method="POST", payload={**payload, "module": "os"})[0], 400)
