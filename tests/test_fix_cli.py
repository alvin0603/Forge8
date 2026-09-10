from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from importlib import metadata
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import forge8.checks as checks_module
import forge8.cli as cli_module
from forge8.actions import ActionEnvelope, WriteTextAction
from forge8.cli import build_parser, main
from forge8.inference import ChatResponse
from forge8.server import ServerPreparation


_BROKEN_APP = "def answer():\n    return 1\n"
_FIXED_APP = "def answer():\n    return 2\n"
_APP_TEST = """import unittest

from app import answer


class AnswerTests(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer(), 2)
"""
_PYTEST_APP_TEST = """import pytest

from app import answer


@pytest.mark.parametrize("expected", [2, 2], ids=["first", "second"])
def test_answer(expected):
    assert answer() == expected
"""
_SECRET = "forge8-test-secret-must-never-persist"


def _has_exact_pytest_runtime() -> bool:
    try:
        return metadata.version("pytest") == "9.1.1"
    except metadata.PackageNotFoundError:
        return False


class _FakeStartResult:
    def __init__(self, *, ok: bool) -> None:
        self.ok = ok
        self.status = "ready" if ok else "port_collision"
        self.error = None if ok else "test port is already occupied"
        self.health_last_error = None

    def as_dict(self):
        return {
            "status": self.status,
            "ok": self.ok,
            "error": self.error,
            "authentication": {"secret_in_result": False},
        }


class _FakeShutdownResult:
    def __init__(self, *, ok: bool, started: bool) -> None:
        self.ok = ok
        self.status = (
            "terminated" if ok and started else "not_started" if ok else "shutdown_error"
        )
        self.return_code = -15 if ok and started else None

    def as_dict(self):
        return {
            "status": self.status,
            "ok": self.ok,
            "return_code": self.return_code,
        }


def _supervisor_type(*, start_ok: bool = True, shutdown_ok: bool = True):
    class FakeSupervisor:
        instances = []

        def __init__(self, plan, **kwargs):
            self.plan = plan
            self.kwargs = kwargs
            self.start_result = None
            self.shutdown_result = None
            self.api_key = None
            self.stop_calls = 0
            type(self).instances.append(self)

        def __enter__(self):
            self.start_result = _FakeStartResult(ok=start_ok)
            self.api_key = _SECRET if start_ok else None
            return self

        def __exit__(self, *_args):
            self.stop()

        def stop(self):
            self.stop_calls += 1
            if self.shutdown_result is None:
                self.shutdown_result = _FakeShutdownResult(
                    ok=shutdown_ok,
                    started=start_ok,
                )
                if shutdown_ok:
                    self.api_key = None
            return self.shutdown_result

    return FakeSupervisor


class _ScriptedTransport:
    instances = []
    expected_sha256 = ""

    def __init__(self, endpoint, *, api_key=None, timeout_seconds=0, **_kwargs):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.requests = []
        type(self).instances.append(self)

    def chat(self, request):
        self.requests.append(request)
        action = ActionEnvelope(
            rationale="Apply the smallest guarded repair proven by the failing test.",
            action=WriteTextAction(
                path="app.py",
                expected_sha256=type(self).expected_sha256,
                content=_FIXED_APP,
            ),
        ).as_json()
        return ChatResponse(
            content=action,
            finish_reason="stop",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            timings={"predicted_ms": 5.0},
        )


class _MalformedTransport(_ScriptedTransport):
    instances = []

    def chat(self, request):
        self.requests.append(request)
        return ChatResponse(
            content="not an action envelope",
            finish_reason="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 5},
            timings={"predicted_ms": 1.0},
        )


class FixCLITests(unittest.TestCase):
    def setUp(self) -> None:
        deployment = patch.object(cli_module, "_load_deployment", return_value=None)
        deployment.start()
        self.addCleanup(deployment.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-fix-cli-")
        self.root = Path(self.temporary.name).resolve()
        self.asset_root = self.root / "assets"
        self.asset_root.mkdir()
        model_manifest = self.asset_root / "config" / "models" / "model.json"
        model_manifest.parent.mkdir(parents=True)
        model_manifest.write_text(
            json.dumps({"schema_version": 1, "id": "scripted-local-model"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()
        _ScriptedTransport.instances.clear()
        _MalformedTransport.instances.clear()

    def make_repo(self, name: str, *, passing: bool = False) -> Path:
        repo = self.root / name
        (repo / "tests").mkdir(parents=True)
        (repo / "app.py").write_bytes(
            (_FIXED_APP if passing else _BROKEN_APP).encode("utf-8")
        )
        (repo / "tests" / "test_app.py").write_text(_APP_TEST, encoding="utf-8")
        return repo

    @staticmethod
    def argv(repo: Path) -> list[str]:
        return [
            "fix",
            str(repo),
            "--goal",
            "Make answer() satisfy the existing unittest.",
            "--allow-write",
            "app.py",
            "--check",
            "python_unittest",
            "--json",
        ]

    def preparation(self):
        asset_root = self.asset_root
        manifest_file = asset_root / "config" / "models" / "model.json"

        class FakePlan:
            workspace = asset_root
            endpoint = "http://127.0.0.1:18080"
            model_manifest_path = manifest_file

            @staticmethod
            def as_dict():
                return {
                    "endpoint": "http://127.0.0.1:18080",
                    "command": ["pinned-llama-server"],
                    "authentication": {"secret_in_command": False},
                }

        return ServerPreparation("ready", (), plan=FakePlan())  # type: ignore[arg-type]

    def test_parser_exposes_only_bounded_repo_fix_contract(self) -> None:
        args = build_parser().parse_args(self.argv(Path("repo")))
        self.assertEqual(args.command, "fix")
        self.assertEqual(args.allow_write, ["app.py"])
        self.assertEqual(args.check, ["python_unittest"])
        for forbidden in ("--apply", "--endpoint", "--model", "--command", "--env"):
            with self.subTest(forbidden=forbidden), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args(self.argv(Path("repo")) + [forbidden, "x"])

    def test_fix_help_states_the_real_process_only_boundary_in_ascii(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["fix", "--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("process_only", help_text)
        self.assertIn("without a filesystem or network sandbox", help_text)
        self.assertIn("trusted repositories", help_text)
        self.assertIn("python_pytest", help_text)
        self.assertIn("forge8[pytest]", help_text)
        self.assertIn("strict-outcome", help_text)
        self.assertIn("ignores repository pytest config", " ".join(help_text.split()))
        self.assertNotIn("never modified", help_text)
        help_text.encode("ascii")

    def test_human_success_output_is_reviewable_truthful_and_ascii(self) -> None:
        stdout = io.StringIO()
        payload = {
            "ok": True,
            "status": "verified",
            "run_root": r"C:\forge8\run",
            "outcome": {
                "delivery": {
                    "handoff_path": r"C:\forge8\run\delivery\HANDOFF.md",
                    "manifest_path": r"C:\forge8\run\delivery\manifest.json",
                    "verification_path": r"C:\forge8\run\delivery\verification.json",
                    "patch_path": r"C:\forge8\run\delivery\changes.patch",
                }
            },
        }

        with redirect_stdout(stdout):
            cli_module._emit_fix_result(payload, as_json=False)

        rendered = stdout.getvalue()
        self.assertIn("did not apply the patch", rendered)
        self.assertIn("Source fingerprint matched", rendered)
        self.assertIn("HANDOFF.md", rendered)
        self.assertIn("manifest.json", rendered)
        self.assertIn("verification.json", rendered)
        self.assertIn("process_only", rendered)
        rendered.encode("ascii")

    def test_json_output_escapes_localized_errors_for_utf8_consumers(self) -> None:
        stdout = io.StringIO()
        payload = {
            "schema_version": 1,
            "kind": "forge8.fix",
            "status": "configuration_error",
            "ok": False,
            "error": "FileNotFoundError: [WinError 2] 系統找不到指定的檔案。",
        }

        with redirect_stdout(stdout):
            cli_module._emit_fix_result(payload, as_json=True)

        rendered = stdout.getvalue()
        self.assertEqual(json.loads(rendered), payload)
        rendered.encode("ascii")

    def test_target_asset_overlap_is_rejected_before_evidence_directory_creation(self) -> None:
        repo = self.asset_root / "nested-target"
        repo.mkdir()
        stdout = io.StringIO()
        with (
            patch("forge8.cli._resolve_fix_asset_root", return_value=self.asset_root),
            patch("forge8.cli.prepare_repository_repair") as prepare,
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(self.argv(repo))

        self.assertEqual(exit_code, 2)
        prepare.assert_not_called()
        self.assertFalse((self.asset_root / ".forge8").exists())
        self.assertIn("must not overlap", json.loads(stdout.getvalue())["error"])

    def test_asset_discovery_never_trusts_malicious_current_directory(self) -> None:
        malicious = self.root / "malicious-cwd"
        for relative in (
            "config/runtimes/llama_cpp_b10621.json",
            "config/models/gemma4_e4b_qat_q4.json",
            "config/profiles/gemma4_e4b_text_8k.json",
        ):
            target = malicious / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
        (malicious / "runtime").mkdir()
        (malicious / "models").mkdir()
        fake_module = self.root / "trusted" / "site-packages" / "forge8" / "cli.py"
        fake_module.parent.mkdir(parents=True)
        fake_module.write_text("# test", encoding="utf-8")

        previous = Path.cwd()
        try:
            os.chdir(malicious)
            with (
                patch.object(cli_module, "__file__", str(fake_module)),
                patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(ValueError, "cannot locate Forge8 assets"),
            ):
                cli_module._resolve_fix_asset_root()
        finally:
            os.chdir(previous)

    def test_bad_repository_stops_before_server_preparation(self) -> None:
        repo = self.make_repo("credential-repo")
        (repo / ".env").write_text("TOKEN=private", encoding="utf-8")
        stdout = io.StringIO()
        with (
            patch("forge8.cli._resolve_fix_asset_root", return_value=self.asset_root),
            patch("forge8.cli.prepare_server") as prepare_server,
            patch("forge8.cli.LocalServerSupervisor") as supervisor,
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(self.argv(repo))

        self.assertEqual(exit_code, 2)
        prepare_server.assert_not_called()
        supervisor.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "configuration_error")
        self.assertIn("credential-like", payload["error"])

    def test_already_passing_baseline_writes_honest_bundle_without_server_or_model(self) -> None:
        repo = self.make_repo("already-passing", passing=True)
        stdout = io.StringIO()
        with (
            patch("forge8.cli._resolve_fix_asset_root", return_value=self.asset_root),
            patch("forge8.cli.prepare_server") as prepare_server,
            patch("forge8.cli.LocalServerSupervisor") as supervisor,
            patch("forge8.cli.OpenAITransport") as transport,
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(self.argv(repo))

        self.assertEqual(exit_code, 1)
        prepare_server.assert_not_called()
        supervisor.assert_not_called()
        transport.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "already_passing")
        self.assertEqual(payload["outcome"]["inference"]["calls"], 0)
        self.assertFalse(payload["outcome"]["delivery"]["verified"])
        self.assertTrue(Path(payload["outcome"]["delivery"]["manifest_path"]).is_file())

    def test_missing_pytest_extra_stops_before_server_or_model(self) -> None:
        repo = self.make_repo("missing-pytest-extra")
        argv = self.argv(repo)
        argv[argv.index("python_unittest")] = "python_pytest"
        definition = checks_module.CHECK_REGISTRY["python_pytest"]
        without_site_packages = replace(
            definition,
            argv=(definition.argv[0], "-I", "-S", *definition.argv[2:]),
        )
        registry = MappingProxyType(
            {**checks_module.CHECK_REGISTRY, "python_pytest": without_site_packages}
        )
        stdout = io.StringIO()
        with (
            patch("forge8.cli._resolve_fix_asset_root", return_value=self.asset_root),
            patch.object(checks_module, "CHECK_REGISTRY", registry),
            patch("forge8.cli.prepare_server") as prepare_server,
            patch("forge8.cli.LocalServerSupervisor") as supervisor,
            patch("forge8.cli.OpenAITransport") as transport,
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(argv)

        self.assertEqual(exit_code, 2)
        prepare_server.assert_not_called()
        supervisor.assert_not_called()
        transport.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "invalid")
        self.assertEqual(payload["outcome"]["inference"]["calls"], 0)
        self.assertIn("pytest==9.1.1", payload["error"])
        self.assertIn("forge8[pytest]", payload["baseline"]["checks"][0]["error"])

    def test_success_owns_server_clears_both_secrets_and_delivers_without_applying(self) -> None:
        repo = self.make_repo("repairable")
        original = {
            path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in repo.rglob("*")
            if path.is_file()
        }
        _ScriptedTransport.expected_sha256 = hashlib.sha256(
            _BROKEN_APP.encode("utf-8")
        ).hexdigest()
        supervisor_type = _supervisor_type()
        stdout = io.StringIO()
        state = self.root / "private-state"
        with (
            patch.dict(os.environ, {"FORGE8_STATE_HOME": str(state)}),
            patch("forge8.cli._resolve_fix_asset_root", return_value=self.asset_root),
            patch("forge8.cli.prepare_server", return_value=self.preparation()),
            patch("forge8.cli.LocalServerSupervisor", supervisor_type),
            patch("forge8.cli.OpenAITransport", _ScriptedTransport),
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(self.argv(repo))

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "verified")
        self.assertFalse(payload["patch_applied"])
        self.assertFalse(payload["source_write_attempted"])
        self.assertTrue(payload["source_unchanged"])
        self.assertTrue(payload["outcome"]["delivery"]["verified"])
        self.assertEqual(
            (repo / "app.py").read_text(encoding="utf-8"),
            _BROKEN_APP,
        )
        final = {
            path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in repo.rglob("*")
            if path.is_file()
        }
        self.assertEqual(final, original)
        patch_text = Path(payload["outcome"]["delivery"]["patch_path"]).read_text(
            encoding="utf-8"
        )
        self.assertIn("+    return 2", patch_text)
        self.assertEqual(len(_ScriptedTransport.instances), 1)
        self.assertIsNone(_ScriptedTransport.instances[0].api_key)
        self.assertNotIn(_SECRET, stdout.getvalue())
        run_root = Path(payload["run_root"])
        self.assertEqual(run_root.parent, state / "runs")
        self.assertEqual(supervisor_type.instances[0].kwargs["log_root"], run_root)
        self.assertFalse((self.asset_root / ".forge8").exists())
        persisted = b"\n".join(
            path.read_bytes() for path in run_root.rglob("*") if path.is_file()
        )
        self.assertNotIn(_SECRET.encode("utf-8"), persisted)
        started = [
            json.loads(line)
            for line in (run_root / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(line)["kind"] == "run.started"
        ][0]
        self.assertEqual(started["payload"]["budget"]["max_context_chars"], 10_000)
        self.assertEqual(started["payload"]["budget"]["max_tokens_per_action"], 1_536)

    @unittest.skipUnless(
        _has_exact_pytest_runtime(),
        "requires the exact pytest optional runtime",
    )
    def test_pytest_full_fix_vertical_is_verified_without_test_cache(self) -> None:
        repo = self.make_repo("pytest-full-vertical")
        (repo / "tests" / "test_app.py").write_text(
            _PYTEST_APP_TEST,
            encoding="utf-8",
        )
        original = {
            path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in repo.rglob("*")
            if path.is_file()
        }
        _ScriptedTransport.expected_sha256 = hashlib.sha256(
            _BROKEN_APP.encode("utf-8")
        ).hexdigest()
        supervisor_type = _supervisor_type()
        argv = self.argv(repo)
        argv[argv.index("python_unittest")] = "python_pytest"
        stdout = io.StringIO()
        with (
            patch("forge8.cli._resolve_fix_asset_root", return_value=self.asset_root),
            patch("forge8.cli.prepare_server", return_value=self.preparation()),
            patch("forge8.cli.LocalServerSupervisor", supervisor_type),
            patch("forge8.cli.OpenAITransport", _ScriptedTransport),
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(argv)

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "verified")
        self.assertEqual(payload["baseline"]["checks"][0]["check_id"], "python_pytest")
        self.assertEqual(payload["outcome"]["inference"]["calls"], 1)
        self.assertEqual(
            {
                path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in repo.rglob("*")
                if path.is_file()
            },
            original,
        )
        self.assertFalse(any(repo.rglob(".pytest_cache")))
        self.assertFalse(any(repo.rglob("__pycache__")))

    def test_startup_failure_never_builds_transport_and_still_seals_failure_bundle(self) -> None:
        repo = self.make_repo("startup-failure")
        supervisor_type = _supervisor_type(start_ok=False)
        stdout = io.StringIO()
        with (
            patch("forge8.cli._resolve_fix_asset_root", return_value=self.asset_root),
            patch("forge8.cli.prepare_server", return_value=self.preparation()),
            patch("forge8.cli.LocalServerSupervisor", supervisor_type),
            patch("forge8.cli.OpenAITransport") as transport,
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(self.argv(repo))

        self.assertEqual(exit_code, 2)
        transport.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "port_collision")
        self.assertEqual(payload["outcome"]["status"], "backend_error")
        self.assertFalse(payload["outcome"]["delivery"]["verified"])
        self.assertTrue(Path(payload["run_root"], "trace.seal.json").is_file())
        self.assertEqual(len(supervisor_type.instances), 1)
        self.assertIsNone(supervisor_type.instances[0].api_key)

    def test_nonverified_model_path_skips_gate_but_still_clears_both_secrets(self) -> None:
        repo = self.make_repo("model-not-verified")
        supervisor_type = _supervisor_type()
        stdout = io.StringIO()
        with (
            patch("forge8.cli._resolve_fix_asset_root", return_value=self.asset_root),
            patch("forge8.cli.prepare_server", return_value=self.preparation()),
            patch("forge8.cli.LocalServerSupervisor", supervisor_type),
            patch("forge8.cli.OpenAITransport", _MalformedTransport),
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(self.argv(repo))

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "parse_budget_exhausted")
        self.assertFalse(payload["outcome"]["delivery"]["verified"])
        self.assertIsNone(_MalformedTransport.instances[0].api_key)
        self.assertIsNone(supervisor_type.instances[0].api_key)
        kinds = {
            json.loads(line)["kind"]
            for line in Path(payload["run_root"], "trace.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        }
        self.assertNotIn("acceptance_gate.passed", kinds)
        self.assertNotIn("acceptance_gate.failed", kinds)

    def test_shutdown_failure_downgrades_passing_code_to_not_verified(self) -> None:
        repo = self.make_repo("shutdown-failure")
        _ScriptedTransport.expected_sha256 = hashlib.sha256(
            _BROKEN_APP.encode("utf-8")
        ).hexdigest()
        supervisor_type = _supervisor_type(shutdown_ok=False)
        stdout = io.StringIO()
        with (
            patch("forge8.cli._resolve_fix_asset_root", return_value=self.asset_root),
            patch("forge8.cli.prepare_server", return_value=self.preparation()),
            patch("forge8.cli.LocalServerSupervisor", supervisor_type),
            patch("forge8.cli.OpenAITransport", _ScriptedTransport),
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(self.argv(repo))

        self.assertEqual(exit_code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "server_shutdown_failed")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"]["status"], "acceptance_gate_failed")
        self.assertTrue(payload["outcome"]["verification"]["ok"])
        self.assertFalse(payload["outcome"]["delivery"]["verified"])
        manifest = json.loads(
            Path(payload["outcome"]["delivery"]["manifest_path"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(manifest["verified"])
        self.assertNotIn(_SECRET, stdout.getvalue())

    def test_operator_exception_after_transport_creation_clears_and_redacts(self) -> None:
        repo = self.make_repo("operator-error")
        supervisor_type = _supervisor_type()
        stdout = io.StringIO()
        with (
            patch("forge8.cli._resolve_fix_asset_root", return_value=self.asset_root),
            patch("forge8.cli.prepare_server", return_value=self.preparation()),
            patch("forge8.cli.LocalServerSupervisor", supervisor_type),
            patch("forge8.cli.OpenAITransport", _ScriptedTransport),
            patch(
                "forge8.cli.run_prepared_task",
                side_effect=RuntimeError("operator reflected " + _SECRET),
            ),
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(self.argv(repo))

        self.assertEqual(exit_code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "runtime_error")
        self.assertNotIn(_SECRET, stdout.getvalue())
        self.assertIsNone(_ScriptedTransport.instances[0].api_key)
        self.assertIsNone(supervisor_type.instances[0].api_key)
        self.assertTrue(payload["server"]["supervisor_secret_cleared"])
        self.assertTrue(payload["server"]["transport_secret_cleared"])
        run_root = Path(payload["run_root"])
        persisted = b"\n".join(
            path.read_bytes() for path in run_root.rglob("*") if path.is_file()
        )
        self.assertNotIn(_SECRET.encode("utf-8"), persisted)

    def test_post_secret_exception_stops_server_and_redacts_exception_text(self) -> None:
        repo = self.make_repo("transport-error")
        supervisor_type = _supervisor_type()
        stdout = io.StringIO()
        with (
            patch("forge8.cli._resolve_fix_asset_root", return_value=self.asset_root),
            patch("forge8.cli.prepare_server", return_value=self.preparation()),
            patch("forge8.cli.LocalServerSupervisor", supervisor_type),
            patch(
                "forge8.cli.OpenAITransport",
                side_effect=RuntimeError("constructor exposed " + _SECRET),
            ),
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = main(self.argv(repo))

        self.assertEqual(exit_code, 2)
        self.assertEqual(len(supervisor_type.instances), 1)
        supervisor = supervisor_type.instances[0]
        self.assertIsNotNone(supervisor.shutdown_result)
        self.assertIsNone(supervisor.api_key)
        self.assertNotIn(_SECRET, stdout.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["error"],
            "RuntimeError: operation failed after local secret issuance",
        )
        self.assertEqual(payload["status"], "runtime_error")


if __name__ == "__main__":
    unittest.main()
