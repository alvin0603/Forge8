"""Owned static-source fixtures; no target import, bytecode, model or guest calls."""

from __future__ import annotations

import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from forge8 import cli
from forge8 import desk as desk_module
from forge8 import experiments


class _BrowseFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="forge8-browse-only-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source, self.state = self.root / "source", self.root / "private-state"
        self.source.mkdir()
        self.original = {
            "main.py": b"from helper import merge_headers as combine\n\ndef prepare(headers):\n    return combine(headers)\n\nraise RuntimeError('never execute target source')\n",
            "helper.py": b"def merge_headers(headers):\n    return dict(headers)\n",
        }
        for name, content in self.original.items():
            (self.source / name).write_bytes(content)
        environment = patch.dict(os.environ, {"FORGE8_HOME": str(self.root / "missing-assets"),
            "FORGE8_STATE_HOME": "invalid ambient state must be ignored"})
        environment.start()
        self.addCleanup(environment.stop)
        self.forbidden = []
        for owner, name in ((cli, "_load_deployment"), (cli, "_resolve_fix_asset_root"),
                (desk_module, "_resolve_fix_asset_root"), (desk_module, "ResidentModel"),
                (desk_module, "prepare_comparison"), (desk_module, "_run_explain_cli"),
                (desk_module, "_run_locate_cli"), (desk_module, "_run_project_cli"),
                (experiments, "run_experiment")):
            replacement = patch.object(owner, name,
                side_effect=AssertionError(f"browse-only called forbidden {name}"))
            self.forbidden.append(replacement.start())
            self.addCleanup(replacement.stop)
        self.desk = desk_module.ReadingDesk(self.source, browse_only=True, state_directory=self.state)
        self.addCleanup(self.desk.close)

    def file(self, name="main.py"):
        return next(item for item in self.desk.project["files"] if item["path"] == name)

    def selection(self):
        return {"file": self.file()["id"], "version": self.desk.project["version"], "start": 3, "end": 4}

    def question(self, kind="explain"):
        value = {"question": "Explain these headers.", "version": self.desk.project["version"]}
        if kind == "explain":
            value["focus"] = [{"file": self.file()["id"], "start": 3, "end": 4}]
        elif kind == "continue":
            value["parent"] = "nonexistent-parent"
        return value

    def guest_target(self):
        return {"file": self.file()["id"], "version": self.desk.project["version"], "entry": "prepare"}

    def import_payload(self, context):
        candidate, = [row for row in context["candidates"] if row["name"] == "combine" and row["kind"] == "from import"]
        return {**self.selection(), "binding_id": candidate["binding_id"],
            "declaration_start": candidate["start_line"], "declaration_end": candidate["end_line"]}

    def assert_no_execution(self):
        for forbidden in self.forbidden:
            forbidden.assert_not_called()
        self.assertIsNone(self.desk.worker)
        self.assertIsNone(self.desk.experiment_worker)
        self.assertIsNone(self.desk.model)
        self.assertEqual(self.desk.status()["status"], "idle")
        self.assertEqual(self.desk.history()["entries"], [])
        self.assertFalse(list(self.source.rglob("__pycache__")))
        self.assertFalse(list(self.source.rglob("*.pyc")))


class BrowseOnlyTests(_BrowseFixture):
    def test_explicit_state_needs_no_asset_or_deployment_configuration(self):
        self.assertTrue(self.desk.project["browse_only"])
        self.assertIsNone(self.desk.assets)
        self.assertIsNone(self.desk.reader)
        self.assertIsNone(self.desk.project["reader"])
        self.assertFalse(self.desk.project["experiments_enabled"])
        self.assertNotIn("model_policy", self.desk.project)
        self.assertEqual(self.desk.model_status(), {"enabled": False})
        self.assertEqual(self.desk.root.parent, self.state / "runs")
        self.assertIsNone(self.desk.project["comparison"])
        self.assertEqual({item["path"] for item in self.desk.project["files"]}, set(self.original))
        for name, content in self.original.items():
            self.assertEqual((self.source / name).read_bytes(), content)
            self.assertEqual((Path(self.desk.browse_snapshot.snapshot_root) / name).read_bytes(), content)
        self.assertFalse((self.root / "missing-assets").exists())
        self.assert_no_execution()

    def test_credential_like_source_still_rejects_admission_without_execution(self):
        forbidden_source, private_state = self.root / "credential-source", self.root / "credential-state"
        forbidden_source.mkdir()
        (forbidden_source / "main.py").write_bytes(self.original["main.py"])
        credential = forbidden_source / ".env"
        original_credential = b"OWNED_SECRET_FIXTURE=must-not-be-admitted\n"
        credential.write_bytes(original_credential)
        with self.assertRaisesRegex(ValueError, "credential-like path is not allowed"):
            desk_module.ReadingDesk(forbidden_source, browse_only=True, state_directory=private_state)
        self.assertEqual(credential.read_bytes(), original_credential)
        self.assertEqual((forbidden_source / "main.py").read_bytes(), self.original["main.py"])
        self.assertFalse(list(private_state.rglob(".env")), "refused credential content must never enter a published snapshot")
        self.assertFalse(list(forbidden_source.rglob("__pycache__")))
        self.assert_no_execution()

    def test_static_search_name_import_and_traceback_navigation_use_retained_source(self):
        version = self.desk.project["version"]
        self.assertEqual(self.desk.source_view(self.file()["id"], version)["lines"][3], "    return combine(headers)")
        self.assertEqual(self.desk.search("return combine")["matches"][0]["line"], 4)
        self.assertEqual(self.desk.definitions("prepare", version)["matches"][0]["path"], "main.py")
        keyword = self.desk.definitions("merge headers", version, mode="keywords")
        self.assertEqual(keyword["matches"][0]["name"], "merge_headers")
        context = self.desk.context(self.selection())
        imported = self.desk.import_source(self.import_payload(context))
        self.assertIs(imported["semantics_verified"], False)
        self.assertEqual([(route["steps"][-1]["path"], route["steps"][-1]["name"])
            for route in imported["routes"] if route["outcome"] == "declaration"], [("helper.py", "merge_headers")])
        trace = self.desk.tracebacks({"version": version,
            "text": 'Traceback (most recent call last):\n  File "main.py", line 4, in prepare\nValueError: owned fixture'})
        self.assertEqual(trace["scope"], "unverified_user_traceback")
        self.assertEqual(trace["matched"], 1)
        self.assertEqual(trace["frames"][0]["file"], self.file()["id"])
        self.assert_no_execution()

    def test_source_refresh_changes_version_without_promoting_browse_to_model_mode(self):
        version = self.desk.project["version"]
        (self.source / "main.py").write_bytes(self.original["main.py"].replace(b"return combine(headers)", b"return combine({})"))
        self.assertIn("combine(headers)", self.desk.source_view(self.file()["id"], version)["lines"][3])
        self.desk.refresh(mode="source")
        self.assertNotEqual(self.desk.project["version"], version)
        with self.assertRaises(ValueError):
            self.desk.source_view(self.file()["id"], version)
        self.assertTrue(self.desk.project["browse_only"])
        self.assertIn("combine({})", self.desk.source_view(self.file()["id"], self.desk.project["version"])["lines"][3])
        self.assert_no_execution()

    def test_every_question_guest_release_and_git_comparison_is_refused_without_effects(self):
        before = sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*"))
        project = json.dumps(self.desk.project, sort_keys=True)
        target = self.guest_target()
        attempts = [lambda kind=kind: self.desk.start(self.question(kind), kind=kind)
            for kind in ("explain", "locate", "project", "continue", "unknown")]
        attempts += [lambda: self.desk.refresh(mode="changes"),
            lambda: self.desk.release_model({"session_id": "forged-session"}),
            lambda: self.desk.prepare_experiment(target),
            lambda: self.desk.prepare_experiment({**target, "mode": "head_current"}),
            lambda: self.desk.prepare_experiment_input({**self.selection(), "entry": "prepare"}),
            lambda: self.desk.start_experiment({**target, "source_sha256": "a" * 64,
                "input_text": '{"args": [{}], "kwargs": {}}', "allow_execution": True}),
            lambda: self.desk.cancel_experiment({"id": "forged-trial"})]
        with patch.object(threading.Thread, "start", side_effect=AssertionError("browse-only created a worker")):
            for attempt in attempts:
                with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                    attempt()
        self.assertEqual(json.dumps(self.desk.project, sort_keys=True), project)
        self.assertEqual(sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*")), before)
        self.assert_no_execution()

    def test_invalid_mode_options_and_state_paths_fail_before_creating_directories(self):
        unused = self.root / "must-not-be-created"
        for options in ({"state_directory": None}, {"allow_experiments": True}, {"allow_experiments": 1},
                {"reader": "gemma12b"}, {"idle_timeout": 600}, {"idle_timeout": False}, {"browse_only": 1},
                {"browse_only": False}, {"state_directory": Path("relative-state")},
                {"state_directory": Path(self.root.anchor)}, {"state_directory": unused / ".." / "other"},
                {"state_directory": self.source}, {"state_directory": self.source / "never" / "state"},
                {"state_directory": self.source / "main.py"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                desk_module.ReadingDesk(self.source, **{"browse_only": True, "state_directory": unused, **options})
            self.assertFalse(unused.exists())
            self.assertFalse((self.source / "never").exists())
        self.assert_no_execution()

    def test_redirected_state_ancestor_is_rejected_without_writing_the_destination(self):
        actual, linked = self.root / "actual-state", self.root / "linked-state"
        actual.mkdir()
        try:
            linked.symlink_to(actual, target_is_directory=True)
        except OSError:
            self.skipTest("creating an owned directory symlink is unavailable on this host")
        with self.assertRaises(ValueError):
            desk_module.ReadingDesk(self.source, browse_only=True, state_directory=linked / "nested")
        self.assertEqual(list(actual.iterdir()), [])
        self.assert_no_execution()

    def test_legacy_asset_and_saved_state_selection_still_use_shared_runs_validation(self):
        assets, saved_state = self.root / "ordinary-assets", self.root / "saved-state"
        assets.mkdir()
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(cli, "_load_deployment", return_value={"state": str(saved_state)}) as settings, \
                patch.object(desk_module, "_resolve_fix_asset_root", return_value=assets) as resolve:
            ordinary = desk_module.ReadingDesk(self.source)
            try:
                self.assertEqual(ordinary.root.parent, saved_state / "runs")
                self.assertEqual(ordinary.assets, assets)
                self.assertEqual(ordinary.reader, "qwen35")
                self.assertNotIn("browse_only", ordinary.project)
                self.assertIsNone(ordinary.model)
                resolve.assert_called_once_with("qwen35")
                settings.assert_called_once_with()
            finally:
                ordinary.close()
        self.assert_no_execution()


class BrowseOnlyHTTPTests(_BrowseFixture):
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
        self.assertFalse(self.thread.is_alive())

    def request(self, route, payload=None, *, auth=True, headers=None):
        fields = {"Authorization": "Bearer " + self.token} if auth else {}
        if payload is not None:
            fields.update({"Origin": self.origin, "Content-Type": "application/json"})
        fields.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET" if payload is None else "POST", route,
                None if payload is None else json.dumps(payload).encode(), fields)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_authenticated_static_apis_work_and_keep_private_host_origin_gates(self):
        for headers, auth, expected in (({}, False, 401), ({"Host": "evil.invalid"}, True, 403),
                ({"Origin": "https://evil.invalid"}, True, 403)):
            status, _, raw = self.request("/api/project", auth=auth, headers=headers)
            self.assertEqual(status, expected)
            self.assertNotIn(b"main.py", raw)
        status, headers, raw = self.request("/api/project")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["browse_only"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(self.request("/api/model")[2]), {"enabled": False})
        query = urlencode({"file": self.file()["id"], "version": self.desk.project["version"]})
        self.assertEqual(self.request("/api/source?" + query)[0], 200)
        query = urlencode({"q": "merge headers", "version": self.desk.project["version"], "mode": "keywords"})
        status, _, raw = self.request("/api/definitions?" + query)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["matches"][0]["name"], "merge_headers")
        status, _, raw = self.request("/api/context", self.selection())
        self.assertEqual(status, 200)
        status, _, raw = self.request("/api/import-source", self.import_payload(json.loads(raw)))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["routes"][0]["steps"][-1]["path"], "helper.py")
        self.assertEqual(self.request("/api/refresh", {"mode": "source"})[0], 200)
        self.assert_no_execution()

    def test_forged_authorized_mutations_cannot_enable_ai_guests_or_git(self):
        target = self.guest_target()
        attempts = [("/api/jobs", self.question()), ("/api/locate", self.question("locate")),
            ("/api/project-question", self.question("project")), ("/api/continue-question", self.question("continue")),
            ("/api/refresh", {"mode": "changes"}), ("/api/model/release", {"session_id": "forged-session"}),
            ("/api/experiment/prepare", target), ("/api/experiment/input", {**self.selection(), "entry": "prepare"}),
            ("/api/experiment/run", {**target, "source_sha256": "a" * 64, "allow_execution": True, "input_text": '{"args": [], "kwargs": {}}'}),
            ("/api/experiment/cancel", {"id": "forged-trial"})]
        before = sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*"))
        for route, payload in attempts:
            with self.subTest(route=route):
                self.assertEqual(self.request(route, payload)[0], 400)
        self.assertEqual(self.request("/api/jobs", self.question(), auth=False)[0], 401)
        self.assertEqual(self.request("/api/jobs", self.question(), headers={"Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*")), before)
        self.assert_no_execution()


class BrowseOnlyCLITests(unittest.TestCase):
    def test_explicit_browse_cli_forwarding_and_ordinary_defaults_are_distinct(self):
        state = Path(tempfile.gettempdir()).resolve() / "owned-browse-state"
        args = cli.build_parser().parse_args(["read", "repo", "--browse-only", "--state", str(state)])
        self.assertTrue(args.browse_only)
        self.assertEqual(args.state, state)
        self.assertEqual(args.idle_timeout, 600)
        with patch.object(desk_module, "run_desk", return_value=0) as run:
            self.assertEqual(cli.main(["read", "repo", "--browse-only", "--state", str(state)]), 0)
            run.assert_called_once_with(Path("repo"), browse_only=True, state_directory=state)
            run.reset_mock()
            self.assertEqual(cli.main(["read", "repo"]), 0)
            run.assert_called_once_with(Path("repo"))
            run.reset_mock()
            self.assertEqual(cli.main(["read", "repo", "--reader", "gemma12b", "--idle-timeout", "0"]), 0)
            run.assert_called_once_with(Path("repo"), reader="gemma12b", idle_timeout=0)

    def test_incompatible_cli_options_are_rejected_before_entering_the_desk(self):
        state = str(Path(tempfile.gettempdir()).resolve() / "owned-browse-state")
        for options in (["--browse-only"], ["--state", state],
                ["--browse-only", "--state", state, "--reader", "gemma12b"],
                ["--browse-only", "--state", state, "--allow-experiments"],
                ["--browse-only", "--state", state, "--idle-timeout", "10"]):
            with self.subTest(options=options), patch.object(desk_module, "run_desk") as run, redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["read", "repo", *options]), 2)
                run.assert_not_called()

    def test_run_desk_normalizes_only_browse_default_timeout_and_closes_without_model(self):
        state = Path(tempfile.gettempdir()).resolve() / "owned-browse-state"
        for timeout in (600, 0):
            with self.subTest(timeout=timeout):
                fake = Mock(root=state / "runs" / "read-owned", model=None, experiment={})
                server = Mock()
                server.serve_forever.side_effect = KeyboardInterrupt
                with patch.object(desk_module, "ReadingDesk", return_value=fake) as constructor, \
                        patch.object(desk_module, "make_server", return_value=(server, "http://127.0.0.1:1/#token=owned-fixture")), \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(desk_module.run_desk(Path("repo"), browse_only=True, state_directory=state, idle_timeout=timeout), 0)
                constructor.assert_called_once_with(Path("repo"), observation=None, reader="qwen35", idle_timeout=0,
                    browse_only=True, state_directory=state)
                server.server_close.assert_called_once_with()
                fake.close.assert_called_once_with()
                self.assertIn("純原碼瀏覽", output.getvalue())
                self.assertNotIn("每題完成後釋放模型", output.getvalue())
        for timeout in (10, False, 0.0, 600.0):
            with self.subTest(timeout=timeout), patch.object(desk_module, "ReadingDesk") as constructor, self.assertRaises(ValueError):
                desk_module.run_desk(Path("repo"), browse_only=True, state_directory=state, idle_timeout=timeout)
            constructor.assert_not_called()

    def test_run_desk_rejects_nonboolean_mode_and_experiment_options_before_setup(self):
        state = Path(tempfile.gettempdir()).resolve() / "owned-browse-state"
        for name in ("browse_only", "allow_experiments"):
            for value in (0, 1, None, "false", [], {}):
                options = {"browse_only": True, "state_directory": state, name: value}
                with self.subTest(name=name, value=value), \
                        patch.object(desk_module, "ReadingDesk") as constructor, \
                        patch.object(desk_module, "make_server") as server, self.assertRaises(ValueError):
                    desk_module.run_desk(Path("repo"), **options)
                constructor.assert_not_called()
                server.assert_not_called()
