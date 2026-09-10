from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shlex
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

from forge8 import __version__
import forge8.cli as cli_module
from forge8.cli import build_parser, main
from test_structured_reading import answer_document, citation


class _PassingIntegrity:
    ok = True
    checked = ("pinned-asset",)
    missing: tuple[str, ...] = ()
    mismatched: tuple[str, ...] = ()

    @staticmethod
    def as_dict() -> dict[str, object]:
        return {
            "ok": True,
            "checked": ["pinned-asset"],
            "missing": [],
            "mismatched": [],
        }


class PublicCLIContractTests(unittest.TestCase):
    @staticmethod
    def _subcommands(parser: argparse.ArgumentParser) -> tuple[str, ...]:
        actions = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        if len(actions) != 1:
            raise AssertionError(f"expected one subparser action, found {len(actions)}")
        return tuple(actions[0].choices)

    def test_public_surface_is_exactly_the_product_path_and_asset_verifiers(self) -> None:
        parser = build_parser()

        self.assertEqual(
            self._subcommands(parser), ("configure", "doctor", "experiment", "fix", "explain", "locate", "ask", "read", "observe", "runtime", "model")
        )
        self.assertIn("{configure,doctor,experiment,fix,explain,locate,ask,read,observe,runtime,model}", parser.format_help())

    def test_doctor_checks_selected_reader_without_reactivating_general_tool_permissions(self):
        args = build_parser().parse_args(["doctor"])
        self.assertEqual(args.reader, "qwen35")
        self.assertFalse(args.as_json)
        self.assertFalse(hasattr(args, "repo"))
        for option in (["--root", "project"], ["--allow-write", "app.py"],
                       ["--reader", "unknown"], ["--allow-execution"]):
            with self.subTest(option=option), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["doctor", *option])
        with patch("forge8.doctor.run_doctor", return_value=2) as diagnostic, \
                patch.object(cli_module, "_run_explain_cli") as explain:
            self.assertEqual(main(["doctor", "--reader", "gemma4", "--json"]), 2)
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.args[0].reader, "gemma4")
        self.assertTrue(diagnostic.call_args.args[0].as_json)
        explain.assert_not_called()

    def test_project_question_has_no_client_source_or_write_permissions(self) -> None:
        for reader in ("qwen35", "gemma12b"):
            args = build_parser().parse_args(["ask", "repo", "--question", "Why?", "--reader", reader])
            self.assertEqual(args.reader, reader)
            self.assertFalse(hasattr(args, "focus"))
            self.assertFalse(hasattr(args, "allow_write"))
        for option in (["--focus", "app.py:1-2"], ["--allow-write", "app.py"], ["--reader", "gemma4"]):
            with self.subTest(option=option), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["ask", "repo", "--question", "Why?", *option])
        with patch.object(cli_module, "_run_project_cli", return_value=0) as project, \
                patch.object(cli_module, "_run_locate_cli") as locate, patch.object(cli_module, "_run_explain_cli") as explain:
            self.assertEqual(main(["ask", "repo", "--question", "Why?"]), 0)
        project.assert_called_once(); locate.assert_not_called(); explain.assert_not_called()
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            build_parser().parse_args(["ask", "--help"])
        text = " ".join(output.getvalue().split())
        for phrase in ("Two model loads", "ALL returned candidate definitions", "manual selection", "no clipping", "may still be missed"):
            self.assertIn(phrase, text)

    def test_retired_surfaces_are_rejected(self) -> None:
        # doctor is intentionally restored as the bounded selected-reader diagnostic.
        for command in ("plan", "benchmark", "serve"):
            stderr = io.StringIO()
            with self.subTest(command=command), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    build_parser().parse_args([command])

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("invalid choice", stderr.getvalue())

    def test_explain_accepts_repeated_source_focus_without_repair_permissions(self) -> None:
        args = build_parser().parse_args([
            "explain", "repo", "--question", "Explain this lifecycle",
            "--focus", "src/work.py:20-50", "--focus", "src/queue.cpp:10-25",
        ])
        self.assertEqual(args.focus, ["src/work.py:20-50", "src/queue.cpp:10-25"])
        self.assertFalse(hasattr(args, "allow_write"))

    def test_locate_is_explicit_and_needs_no_focus_or_write_permission(self) -> None:
        args = build_parser().parse_args([
            "locate", "repo", "--question", "Where does this workflow begin?", "--json",
        ])
        self.assertEqual(args.reader, "qwen35")
        self.assertEqual(args.repo, Path("repo"))
        self.assertTrue(args.as_json)
        self.assertFalse(hasattr(args, "focus"))
        self.assertFalse(hasattr(args, "allow_write"))
        for reader in ("qwen35", "gemma12b"):
            self.assertEqual(build_parser().parse_args([
                "locate", "repo", "--question", "Why?", "--reader", reader,
            ]).reader, reader)
        for option in (["--focus", "app.py:1-2"], ["--allow-write", "app.py"], ["--reader", "gemma4"]):
            with self.subTest(option=option), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["locate", "repo", "--question", "Why?", *option])

    def test_locate_dispatches_only_to_the_explicit_locator(self) -> None:
        with patch.object(cli_module, "_run_locate_cli", return_value=0) as locate, \
                patch.object(cli_module, "_run_explain_cli") as explain:
            self.assertEqual(main(["locate", "repo", "--question", "Where should I read?"]), 0)
        locate.assert_called_once()
        explain.assert_not_called()

    def test_locate_help_discloses_bounded_file_selection_without_new_options(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["locate", "--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = " ".join(output.getvalue().split())
        for text in ("one request", "at most 2 requests", "up to 3 files", "only those files",
                "may miss relevant files", "not function bodies", "no automatic explanation"):
            self.assertIn(text, help_text)

    def test_locate_human_output_discloses_selected_scope_without_claiming_complete_repo_lookup(self) -> None:
        scope = {"mode": "files_then_definitions", "files": ["app.py", "helpers/接收.py"],
            "total_files": 36, "total_functions": 174, "selected_functions": 22}
        outcome = {"scope": scope, "candidates": [{"name": "begin", "path": "app.py",
            "start_line": 1, "end_line": 3}], "snapshot_sha256": "snapshot", "failure_reason": "selected catalogue too large"}
        for ok in (True, False):
            output, error = io.StringIO(), io.StringIO()
            result = {"ok": ok, "status": "located" if ok else "backend_error", "question": "Where?",
                "outcome": outcome, "server": None, "run_root": "private-run", "error": None}
            with self.subTest(ok=ok), redirect_stdout(output), redirect_stderr(error):
                cli_module._emit_locate_human(result)
            text = (output if ok else error).getvalue()
            for expected in ("2/36 files", "22/174 Python function definitions", "app.py", "helpers/接收.py",
                    "at most 2 requests", "only those files", "may miss relevant files", "not function bodies"):
                self.assertIn(expected, text)
            self.assertNotIn("complete small Python catalogue", text)
            self.assertNotIn("ANSWERED", text)
            if ok:
                self.assertIn("12,000 user-context characters", text)
                self.assertNotIn("12,000 prompt characters", text)

    def test_supported_argument_shapes_remain_parseable(self) -> None:
        parser = build_parser()
        fix = parser.parse_args(
            [
                "fix",
                "repo",
                "--goal",
                "repair the failing test",
                "--allow-write",
                "src/app.py",
                "--check",
                "python_unittest",
            ]
        )
        pytest_fix = parser.parse_args(
            [
                "fix",
                "repo",
                "--goal",
                "repair the failing test",
                "--allow-write",
                "src/app.py",
                "--check",
                "python_pytest",
            ]
        )
        runtime = parser.parse_args(["runtime", "verify"])
        model = parser.parse_args(["model", "verify"])
        explain = parser.parse_args(
            ["explain", "repo", "--question", "How does request routing work?"]
        )

        self.assertEqual(fix.repo, Path("repo"))
        self.assertEqual(fix.allow_write, ["src/app.py"])
        self.assertEqual(fix.check, ["python_unittest"])
        self.assertEqual(pytest_fix.check, ["python_pytest"])
        self.assertEqual(explain.repo, Path("repo"))
        self.assertEqual(explain.question, "How does request routing work?")
        self.assertEqual((runtime.command, runtime.action), ("runtime", "verify"))
        self.assertEqual((model.command, model.action), ("model", "verify"))

    def test_version_is_available_without_entering_a_workflow(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), f"forge8 {__version__}\n")

    def test_fix_dispatches_to_the_single_repair_entrypoint(self) -> None:
        argv = [
            "fix",
            "repo",
            "--goal",
            "repair the failing test",
            "--allow-write",
            "src/app.py",
            "--check",
            "python_unittest",
        ]
        with patch("forge8.cli._run_fix_cli", return_value=0) as run_fix:
            exit_code = main(argv)

        self.assertEqual(exit_code, 0)
        run_fix.assert_called_once()
        self.assertEqual(run_fix.call_args.args[0].command, "fix")

    def test_explain_dispatches_to_the_read_only_entrypoint(self) -> None:
        argv = ["explain", "repo", "--question", "Explain the billing flow"]
        with patch("forge8.cli._run_explain_cli", return_value=0) as run_explain:
            exit_code = main(argv)

        self.assertEqual(exit_code, 0)
        run_explain.assert_called_once()
        self.assertEqual(run_explain.call_args.args[0].command, "explain")

    def test_gemma12b_is_explicit_and_existing_reader_defaults_do_not_change(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.parse_args(["read", "repo"]).reader, "qwen35")
        self.assertEqual(parser.parse_args(["explain", "repo", "--question", "Why?"]).reader, "gemma4")
        for command, extra in (("read", []), ("explain", ["--question", "Why?"])):
            self.assertEqual(parser.parse_args([command, "repo", *extra, "--reader", "gemma12b"]).reader, "gemma12b")
        with patch("forge8.desk.run_desk", return_value=0) as desk:
            self.assertEqual(main(["read", "repo", "--reader", "gemma12b"]), 0)
            desk.assert_called_once_with(Path("repo"), reader="gemma12b")
            desk.reset_mock()
            self.assertEqual(main(["read", "repo", "--reader", "gemma12b", "--observation", "report.json"]), 0)
            desk.assert_called_once_with(Path("repo"), reader="gemma12b", observation=Path("report.json"))

    def test_selected_reader_cli_preserves_real_identity_stream_and_shared_pipeline(self) -> None:
        from forge8.inference import ChatResponse
        from forge8.server import ServerPreparation
        from test_fix_cli import _supervisor_type

        class Transport:
            instances = []

            def __init__(self, endpoint, *, api_key, on_text=None, **_kwargs):
                self.api_key, self.on_text, self.requests = api_key, on_text, []
                self.instances.append(self)

            def chat(self, request):
                self.requests.append(request)
                text = (json.dumps(answer_document("It returns 7.")) if reader == "qwen35"
                    else "It returns 7. [E1:L3-L4]")
                self.on_text(text)
                return ChatResponse(text, "stop", {}, {})

        for reader, model, temperature, top_k in (("qwen35", "qwen35-9b-q4-k-m", 0.6, 20), ("gemma12b", "gemma4-12b-qat-q4", 1.0, 64)):
            with self.subTest(reader=reader), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                source, assets, state = root / "source", root / "assets", root / "state"
                source.mkdir(); assets.mkdir()
                content = 'raise RuntimeError("never execute source")\n\ndef answer():\n    return 7\n'
                (source / "app.py").write_text(content, encoding="utf-8")
                manifest = assets / "model.json"
                manifest.write_text(json.dumps({"id": model}), encoding="utf-8")
                plan = SimpleNamespace(endpoint="http://127.0.0.1:18080", model_manifest_path=manifest,
                    as_dict=lambda: {"endpoint": "http://127.0.0.1:18080"})
                prepared_server = ServerPreparation("ready", (), plan=plan)
                supervisor = _supervisor_type(); chunks, results = [], []
                with (patch.object(cli_module, "_load_deployment", return_value=None),
                      patch.dict(os.environ, {"FORGE8_STATE_HOME": str(state)}),
                      patch.object(cli_module, "_resolve_fix_asset_root", return_value=assets) as resolve,
                      patch.object(cli_module, "prepare_server", return_value=prepared_server) as server,
                      patch.object(cli_module, "LocalServerSupervisor", supervisor),
                      patch.object(cli_module, "OpenAITransport", Transport)):
                    args = build_parser().parse_args(["explain", str(source), "--question", "Explain answer().", "--focus", "app.py:3-4", "--reader", reader, "--json"])
                    self.assertEqual(cli_module._run_explain_cli(args, on_result=results.append, on_text=chunks.append, on_progress=lambda _: None), 0)
                    runtime, model_path, profile = cli_module._fix_asset_anchors(Path(), reader)[:3]
                resolve.assert_called_once_with(reader)
                server.assert_called_once_with(assets, runtime_manifest_path=runtime, profile_path=profile, model_manifest_path=model_path)
                transport = Transport.instances[-1]
                self.assertEqual(len(transport.requests), 1)
                request = transport.requests[0]
                self.assertEqual((request.model, request.temperature, request.top_k), (model, temperature, top_k))
                self.assertEqual(chunks, ["It returns 7." if reader == "qwen35" else "It returns 7. [E1:L3-L4]"])
                self.assertEqual(request.response_format is not None, reader == "qwen35")
                self.assertIsNone(request.reasoning_budget_tokens)
                self.assertIsNone(request.enable_thinking)
                self.assertTrue(results[0]["ok"])
                self.assertTrue(results[0]["outcome"]["source_unchanged"])
                self.assertEqual((source / "app.py").read_text(encoding="utf-8"), content)
                self.assertEqual(cli_module._explain_gpu_state(results[0]["server"]), "released")
                self.assertIsNone(transport.api_key); self.assertIsNone(supervisor.instances[0].api_key)

    def test_selected_readers_require_focus_before_assets_or_server_preparation(self) -> None:
        for reader in ("qwen35", "gemma12b"):
            with self.subTest(reader=reader), patch.object(cli_module, "_resolve_fix_asset_root") as assets, patch.object(cli_module, "_fix_runs_parent") as runs, patch.object(cli_module, "prepare_server") as prepare, patch.object(cli_module, "LocalServerSupervisor") as supervisor:
                results = []
                args = build_parser().parse_args(["explain", "missing-repo", "--question", "Why?", "--reader", reader])
                self.assertEqual(cli_module._run_explain_cli(args, on_result=results.append), 2)
                self.assertIn(f"{reader} preview reader requires --focus", results[0]["error"])
                for operation in (assets, runs, prepare, supervisor):
                    operation.assert_not_called()

    def test_locate_uses_real_preflight_nonstream_request_and_cleanup_gate(self) -> None:
        from forge8.inference import ChatResponse
        from forge8.server import ServerPreparation
        from test_fix_cli import _supervisor_type

        class Transport:
            instances = []

            def __init__(self, endpoint, *, api_key, **kwargs):
                self.api_key, self.requests, self.options = api_key, [], kwargs
                self.instances.append(self)

            def chat(self, request):
                self.requests.append(request)
                return ChatResponse(responses[len(self.requests) - 1], "stop", {}, {})

        for reader, model, shutdown, large in (("qwen35", "qwen35-9b-q4-k-m", True, False),
                ("gemma12b", "gemma4-12b-qat-q4", True, False), ("qwen35", "qwen35-9b-q4-k-m", False, False),
                ("qwen35", "qwen35-9b-q4-k-m", True, True)):
            with self.subTest(reader=reader, shutdown=shutdown, large=large), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                source, assets, state = root / "source", root / "assets", root / "state"
                source.mkdir(); assets.mkdir()
                text = 'raise RuntimeError("never execute")\n\ndef begin(): return 7\n'
                if large:
                    text = 'raise RuntimeError("never execute")\n' + '\n'.join(
                        f"def begin_{i}_{'x' * 50}(): return 7" for i in range(100))
                    (source / "other.py").write_text(text, encoding="utf-8")
                (source / "app.py").write_text(text, encoding="utf-8")
                responses = (["{\"files\":[\"F0001\"]}"] if large else []) + ['{"candidates":["D0001"]}']
                manifest = assets / "model.json"
                manifest.write_text(json.dumps({"id": model}), encoding="utf-8")
                plan = SimpleNamespace(endpoint="http://127.0.0.1:18080", model_manifest_path=manifest,
                    as_dict=lambda: {"endpoint": "http://127.0.0.1:18080"})
                supervisor = _supervisor_type(shutdown_ok=shutdown)
                results, progress = [], []
                with (patch.object(cli_module, "_load_deployment", return_value=None),
                      patch.dict(os.environ, {"FORGE8_STATE_HOME": str(state)}),
                      patch.object(cli_module, "_resolve_fix_asset_root", return_value=assets),
                      patch.object(cli_module, "prepare_server", return_value=ServerPreparation("ready", (), plan=plan)) as server,
                      patch.object(cli_module, "LocalServerSupervisor", supervisor),
                      patch.object(cli_module, "OpenAITransport", Transport),
                      patch.object(cli_module, "run_explanation") as explain):
                    args = build_parser().parse_args(["locate", str(source), "--question", "Where to begin?", "--reader", reader, "--json"])
                    code = cli_module._run_locate_cli(args, on_result=results.append, on_progress=progress.append)
                self.assertEqual(code, 0 if shutdown else 2)
                server.assert_called_once(); explain.assert_not_called()
                transport = Transport.instances[-1]
                self.assertNotIn("on_text", transport.options)
                self.assertEqual(len(transport.requests), 2 if large else 1)
                self.assertEqual(len(supervisor.instances), 1)
                self.assertEqual(transport.requests[0].model, model)
                self.assertIsNotNone(transport.requests[0].response_format)
                self.assertNotIn("never execute", transport.requests[0].messages[1].content)
                self.assertIsNone(transport.api_key)
                self.assertEqual(results[0]["kind"], "forge8.locate")
                if large:
                    self.assertEqual(results[0]["outcome"]["scope"], {"mode": "files_then_definitions",
                        "files": ["app.py"], "total_files": 2, "total_functions": 200, "selected_functions": 100})
                    notice = "Large catalogue: at most 2 requests; choose up to 3 files, then definitions only within those files; relevant files may be missed."
                    self.assertLess(progress.index(notice), progress.index("hashing the pinned local runtime and model"))
                else:
                    self.assertNotIn("scope", results[0]["outcome"])
                    self.assertFalse(any(message.startswith("Large catalogue:") for message in progress))
                self.assertEqual(results[0]["ok"], shutdown)
                self.assertEqual(bool(results[0]["outcome"]["candidates"]), shutdown)
                self.assertFalse(results[0]["repository_code_executed"])
                self.assertFalse(results[0]["semantic_claims_verified"])
                self.assertEqual((source / "app.py").read_text(encoding="utf-8"), text)
                output, error = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    cli_module._emit_locate_human(results[0])
                self.assertNotIn("ANSWERED", output.getvalue())
                if shutdown:
                    self.assertIn("app.py", output.getvalue())
                    self.assertIn("begin", output.getvalue())
                    self.assertIn("Selected-file scope" if large else "complete small Python catalogue", output.getvalue())
                    self.assertNotIn("complete small Python catalogue" if large else "Selected-file scope", output.getvalue())
                    self.assertIsNone(supervisor.instances[0].api_key)

    def test_locate_full_file_map_rejection_precedes_server_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source, assets = root / "source", root / "assets"
            source.mkdir(); assets.mkdir()
            for index in range(220):
                (source / f"{index:03}_{'x' * 60}.py").write_text("def begin(): pass\n", encoding="utf-8")
            results = []
            with (patch.object(cli_module, "_load_deployment", return_value=None),
                  patch.dict(os.environ, {"FORGE8_STATE_HOME": str(root / "state")}),
                  patch.object(cli_module, "_resolve_fix_asset_root", return_value=assets),
                  patch.object(cli_module, "prepare_server") as server,
                  patch.object(cli_module, "LocalServerSupervisor") as supervisor):
                args = build_parser().parse_args(["locate", str(source), "--question", "Where to begin?"])
                self.assertEqual(cli_module._run_locate_cli(args, on_result=results.append, on_progress=lambda _: None), 2)
            server.assert_not_called(); supervisor.assert_not_called()
            self.assertFalse(results[0]["ok"])
            self.assertRegex(results[0]["error"], "catalogue|file[ -]map")

    def test_missing_selected_weights_explain_exact_native_verification_without_start_or_fallback(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        for reader in ("qwen35", "gemma12b"):
            with self.subTest(reader=reader), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                source, assets = root / "source", root / "assets"
                source.mkdir()
                (source / "app.py").write_text("value = 7\n", encoding="utf-8")
                runtime, model, profile = cli_module._fix_asset_anchors(Path(), reader)[:3]
                for relative in (runtime, model, profile):
                    target = assets / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((repository / relative).read_bytes())
                (assets / "runtime").mkdir(); (assets / "models").mkdir()
                expected = [entry["filename"] for entry in json.loads((assets / model).read_text(encoding="utf-8"))["files"]]
                # CUDA integrity is scripted; model verification and preparation
                # remain real, without reading cached weights or starting a process.
                with (patch.object(cli_module, "_load_deployment", return_value=None),
                      patch.dict(os.environ, {"FORGE8_HOME": str(assets), "FORGE8_STATE_HOME": str(root / "state")}, clear=True),
                      patch("forge8.server.verify_runtime", return_value=_PassingIntegrity()),
                      patch.object(cli_module, "prepare_server", wraps=cli_module.prepare_server) as prepare,
                      patch.object(cli_module, "LocalServerSupervisor") as supervisor,
                      patch.object(cli_module, "OpenAITransport") as transport):
                    results = []
                    args = build_parser().parse_args(["explain", str(source), "--question", "What is value?", "--focus", "app.py:1-1", "--reader", reader])
                    self.assertEqual(cli_module._run_explain_cli(args, on_result=results.append, on_progress=lambda _: None), 2)
                self.assertEqual(results[0]["status"], "integrity_failed", results[0].get("error"))
                self.assertEqual(results[0]["server"]["preparation"]["model_integrity"]["missing"], expected)
                self.assertIn(f"reader {reader}", results[0]["error"])
                for value in (*expected, str(assets / model), str(assets / "models"), "--manifest", "--root"):
                    self.assertIn(value, results[0]["error"])
                self.assertNotIn("gemma4_e4b_qat_q4.json", results[0]["error"])
                prepare.assert_called_once_with(assets, runtime_manifest_path=runtime, model_manifest_path=model, profile_path=profile)
                supervisor.assert_not_called(); transport.assert_not_called()
                self.assertEqual(list((assets / "models").iterdir()), [])

    def test_integrity_diagnostics_are_bounded_inert_and_use_no_new_reads(self) -> None:
        integrity = SimpleNamespace(ok=False, missing=("first\n\x1b\u202efile.gguf", "x" * 800, "third", "OMITTED"), mismatched=("wrong.gguf: expected 8 bytes, got 4",))
        preparation = SimpleNamespace(runtime_integrity=None, model_integrity=integrity, errors=("PRIVATE_EXCEPTION",))
        with (patch.object(Path, "read_bytes", side_effect=AssertionError("unexpected read")),
              patch.object(Path, "read_text", side_effect=AssertionError("unexpected read")),
              patch.object(Path, "resolve", side_effect=AssertionError("venv must not resolve"))):
            message = cli_module._explain_integrity_error(preparation, "gemma12b", Path("/assets"), Path("runtime.json"), Path("model.json"))
        self.assertIn("4 missing, 1 mismatched", message)
        self.assertIn(r"first\u000a\u001b\u202efile.gguf", message)
        self.assertIn("[truncated]", message)
        self.assertIn("2 more", message)
        self.assertNotIn("OMITTED", message); self.assertNotIn("PRIVATE_EXCEPTION", message)
        self.assertNotIn("x" * 241, message); self.assertLess(len(message), 3000)

    def test_integrity_commands_preserve_native_argv_and_are_copyable_human_lines(self) -> None:
        bad = SimpleNamespace(ok=False, missing=(), mismatched=("file: expected good hash, got other hash",))
        preparation = SimpleNamespace(runtime_integrity=bad, model_integrity=bad)
        cases = (("Linux", Path("/assets/O'Brien $(noop); cache"), "/home/user/.venv/bin/python"),
                 ("Windows", PureWindowsPath(r"C:\assets\O'Brien $(noop); cache"), r"C:\user's venv\Scripts\python.exe"))
        for system, assets, executable in cases:
            with self.subTest(system=system), patch.object(cli_module.platform, "system", return_value=system), patch.object(sys, "executable", executable), redirect_stderr(io.StringIO()):
                message = cli_module._explain_integrity_error(preparation, "qwen35", assets, Path("runtime.json"), Path("model.json"))
            commands = [line for line in message.splitlines() if line.startswith("& ") or line.startswith("/home/")]
            self.assertEqual(len(commands), 2)
            for kind, manifest, command in zip(("runtime", "model"), ("runtime.json", "model.json"), commands):
                argv = [executable, "-I", "-m", "forge8", kind, "verify", "--manifest", str(assets / manifest), "--root", str(assets / ("models" if kind == "model" else "runtime"))]
                if system == "Linux":
                    self.assertEqual(shlex.split(command), argv)
                else:
                    self.assertEqual(command, "& " + " ".join("'" + value.replace("'", "''") + "'" for value in argv))
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    cli_module._emit_explain_human({"ok": False, "status": "integrity_failed", "question": "Why?", "run_root": None, "outcome": None, "server": {"preparation": {}}, "error": message})
                self.assertIn("\n  " + command + "\n", stderr.getvalue())
                self.assertIn("GPU/server: not acquired.", stderr.getvalue())

    def test_integrity_commands_omit_unsafe_long_or_unencodable_paths(self) -> None:
        bad = SimpleNamespace(ok=False, missing=("model.gguf",), mismatched=())
        preparation = SimpleNamespace(runtime_integrity=None, model_integrity=bad)
        for suffix, encoding in (("\nforged", "utf-8"), ("\u202e", "utf-8"), ("\u2019", "utf-8"), ("x" * 3000, "utf-8"), ("\U0001f680", "cp950")):
            with self.subTest(suffix=suffix[:20], encoding=encoding):
                buffer = io.BytesIO()
                stream = io.TextIOWrapper(buffer, encoding=encoding)
                with redirect_stderr(stream), patch.object(cli_module.platform, "system", return_value="Windows"):
                    message = cli_module._explain_integrity_error(preparation, "gemma12b", Path("/assets" + suffix), Path("runtime.json"), Path("model.json"))
                    cli_module._emit_explain_human({"ok": False, "status": "integrity_failed", "question": "Why?", "run_root": None, "outcome": None, "server": None, "error": message})
                stream.flush()
                self.assertIn("Copyable command omitted", message)
                self.assertNotIn("\n& ", message); self.assertNotIn("--manifest", message)
                self.assertNotIn("forged", message); self.assertLess(len(buffer.getvalue()), 1500)
                stream.close()

    def test_generated_verification_arguments_check_real_synthetic_artifact_roots(self) -> None:
        from test_server import ServerFixture

        fixture = ServerFixture().make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        bad = SimpleNamespace(ok=False, missing=("example",), mismatched=())
        preparation = SimpleNamespace(runtime_integrity=bad, model_integrity=bad)
        # Parse the displayed Bash argv even on Windows, then invoke the actual
        # verification branch in-process. No installed-package or shell assumption.
        with patch.object(cli_module.platform, "system", return_value="Linux"), redirect_stderr(io.StringIO()):
            message = cli_module._explain_integrity_error(preparation, "gemma12b", fixture["root"],
                Path("config/runtimes/runtime.json"), Path("config/models/model.json"))
        commands = [shlex.split(line) for previous, line in zip(message.splitlines(), message.splitlines()[1:]) if previous.startswith("Verify manually")]
        self.assertEqual(len(commands), 2)
        with patch.object(cli_module, "_resolve_fix_asset_root", side_effect=AssertionError("implicit defaults")), patch.object(cli_module, "LocalServerSupervisor") as supervisor:
            for argv, checked in zip(commands, (2, 1)):
                self.assertEqual(argv[:4], [sys.executable, "-I", "-m", "forge8"])
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main([*argv[4:], "--json"]), 0)
                self.assertEqual(len(json.loads(output.getvalue())["checked"]), checked)
            fixture["model"].write_bytes(b"wrong size")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([*commands[1][4:], "--json"]), 1)
            self.assertIn("expected", json.loads(output.getvalue())["mismatched"][0])
            supervisor.assert_not_called()

    def test_explain_human_output_shows_grounded_answer_and_inerts_failures(self) -> None:
        success = {
            "ok": True,
            "status": "answered",
            "question": "How does the billing flow return a total?",
            "run_root": "run",
            "server": {
                "start": {"ok": True, "status": "ready"},
                "shutdown": {"ok": True, "status": "terminated", "return_code": 0},
                "supervisor_secret_cleared": True,
                "transport_secret_cleared": True,
            },
            "outcome": {
                "answer": {
                    "claims": [
                        {
                            "type": "source_quote",
                            "text": "\treturn total",
                            "citations": [
                                {
                                    "evidence_id": "E1",
                                    "path": "billing.py",
                                    "start_line": 4,
                                    "end_line": 4,
                                }
                            ],
                        },
                        {
                            "type": "inference",
                            "text": "The function returns the computed total.",
                            "citations": [
                                {
                                    "evidence_id": "E1",
                                    "path": "billing.py",
                                    "start_line": 3,
                                    "end_line": 4,
                                }
                            ],
                        },
                    ]
                },
                "answer_path": "run/ANSWER.txt",
                "explanation_path": "run/explanation.json",
                "manifest_path": "run/manifest.json",
                "coverage": {
                    "observed": {"files": 1, "lines": 4},
                    "cited": {"files": 1, "lines": 2},
                    "admitted": {"files": 2, "lines": 20},
                },
            },
        }
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            cli_module._emit_explain_human(success)
        rendered = stdout.getvalue()
        self.assertIn(
            "Question:\n  How does the billing flow return a total?", rendered
        )
        self.assertIn(
            "2. Source quote\n   Source excerpt (terminal-safe representation):"
            "\n   | \\u0009return total",
            rendered,
        )
        self.assertIn("Source: billing.py:L4 (E1)", rendered)
        self.assertIn("1. Inference\n   The function", rendered)
        self.assertIn("Based on: billing.py:L3-L4 (E1)", rendered)
        self.assertIn("Coverage: 1/2 files and 4/20 lines observed", rendered)
        self.assertIn("GPU/server: released (terminated; secrets cleared).", rendered)
        self.assertNotIn("file_sha256", rendered)

        failure = {
            "ok": False,
            "status": "backend_error",
            "question": "Why does billing fail?",
            "run_root": "run\nforged",
            "server": {
                "start": {"ok": True, "status": "ready"},
                "shutdown": {"ok": True, "status": "terminated", "return_code": 1},
                "supervisor_secret_cleared": True,
                "transport_secret_cleared": True,
            },
            "outcome": {"failure_reason": "boom\x1b[31m\nANSWERED", "answer_path": "a"},
            "error": None,
        }
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            cli_module._emit_explain_human(failure)
        rendered = stderr.getvalue()
        self.assertIn("Diagnostic bundle", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("run\nforged", rendered)
        self.assertIn("\\u001b", rendered)
        self.assertIn("Question:\n  Why does billing fail?", rendered)
        self.assertIn("Next: Verify the pinned runtime/model assets", rendered)
        self.assertIn("GPU/server: released (terminated; secrets cleared).", rendered)

    def test_complete_unverified_prose_is_visible_but_keeps_failure_wire_exit_and_labels(self) -> None:
        text = "First generated line.\n  第二行；service.py L3-L4 不是引用。"
        detail = {"ok": False, "status": "stalled", "answer": None, "unverified_prose": text,
            "source_unchanged": True, "snapshot_unchanged": True, "acceptance": {"ok": True},
            "coverage": {"observed": {"ranges": [{"path": "service.py", "ranges": [{"start_line": 3, "end_line": 4}]}]}}}
        server = {"start": {"ok": True, "pid": 123},
            "shutdown": {"ok": True, "status": "terminated", "return_code": 0},
            "supervisor_secret_cleared": True, "transport_secret_cleared": True}
        outcome = SimpleNamespace(ok=False, as_dict=lambda: detail)
        args = argparse.Namespace(question="Explain this.", as_json=False)
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = cli_module._finish_explain(args, "stalled", Path("source"), "reading", Path("run"),
                server=server, outcome=outcome)
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("INCOMPLETE (stalled)", error.getvalue())
        self.assertIn("Model text generated; references UNVERIFIED; semantics UNVERIFIED.", error.getvalue())
        self.assertIn(text, error.getvalue())
        self.assertIn("service.py:3-4", error.getvalue())
        self.assertIn("not answer citations", error.getvalue())
        self.assertNotIn("ANSWERED", error.getvalue())
        args.as_json = True
        with redirect_stdout(output):
            self.assertEqual(cli_module._finish_explain(args, "stalled", Path("source"), "reading", Path("run"),
                server=server, outcome=outcome), 1)
        result = json.loads(output.getvalue())
        self.assertEqual((result["status"], result["ok"], result["outcome"]["answer"]), ("stalled", False, None))
        self.assertEqual(result["outcome"]["unverified_prose"], text)
        self.assertFalse(result["semantic_claims_verified"])
        for fault in ("source", "acceptance", "release", "unsafe", "error"):
            with self.subTest(fault=fault):
                current, lifecycle = json.loads(json.dumps(detail)), json.loads(json.dumps(server))
                if fault == "source": current["source_unchanged"] = False
                if fault == "acceptance": current["acceptance"]["ok"] = False
                if fault == "release": lifecycle["shutdown"]["return_code"] = None
                if fault == "unsafe": current["unverified_prose"] = "unsafe\x1btext"
                values = []
                cli_module._finish_explain(args, "stalled", Path("source"), "reading", Path("run"),
                    server=lifecycle, outcome=SimpleNamespace(ok=False, as_dict=lambda: current),
                    error="late failure" if fault == "error" else None, emit=values.append)
                self.assertNotIn("unverified_prose", values[0]["outcome"])
                self.assertEqual(current["unverified_prose"], "unsafe\x1btext" if fault == "unsafe" else text)

    def test_explain_human_output_never_overclaims_gpu_release(self) -> None:
        base = {
            "ok": False,
            "status": "configuration_error",
            "question": "What calls normalize?",
            "run_root": None,
            "outcome": None,
            "error": "runtime manifest is missing",
        }
        cases = (
            (None, "GPU/server: not acquired."),
            (
                {
                    "start": None,
                    "shutdown": {
                        "ok": False,
                        "status": "shutdown_error",
                        "return_code": None,
                    },
                    "supervisor_secret_cleared": False,
                    "transport_secret_cleared": True,
                },
                "GPU/server: release NOT PROVEN",
            ),
            (
                {
                    "start": {
                        "ok": False,
                        "status": "startup_timeout",
                        "pid": 4242,
                    },
                    "shutdown": {
                        "ok": False,
                        "status": "shutdown_error",
                        "return_code": None,
                    },
                    "supervisor_secret_cleared": False,
                    "transport_secret_cleared": True,
                },
                "GPU/server: release NOT PROVEN",
            ),
            (
                {
                    "start": {"ok": True, "status": "ready"},
                    "shutdown": None,
                    "supervisor_secret_cleared": False,
                    "transport_secret_cleared": True,
                },
                "GPU/server: release NOT PROVEN",
            ),
            (
                {
                    "start": {"ok": True, "status": "ready", "pid": 4242},
                    "shutdown": {
                        "ok": True,
                        "status": "terminated",
                        "return_code": 0,
                    },
                    "supervisor_secret_cleared": False,
                    "transport_secret_cleared": True,
                },
                "GPU/server: release NOT PROVEN",
            ),
            (
                {
                    "start": {"ok": True, "status": "ready", "pid": 4242},
                    "shutdown": {
                        "ok": True,
                        "status": "terminated",
                        "return_code": 0,
                    },
                    "supervisor_secret_cleared": True,
                    "transport_secret_cleared": False,
                },
                "GPU/server: release NOT PROVEN",
            ),
        )
        for server, expected in cases:
            with self.subTest(expected=expected):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    cli_module._emit_explain_human({**base, "server": server})
                self.assertIn(expected, stderr.getvalue())

    def test_explain_human_output_is_cp950_safe_before_the_first_write(self) -> None:
        success = {
            "ok": True,
            "status": "answered",
            "question": "帳務流程🚀如何運作？",
            "run_root": "執行🚀",
            "server": None,
            "outcome": {
                "answer": {
                    "claims": [
                        {
                            "type": "source_quote",
                            "text": "return total  # 🚀",
                            "citations": [
                                {"path": "帳務🚀.py", "start_line": 4, "end_line": 4}
                            ],
                        },
                        {
                            "type": "inference",
                            "text": "總計會傳回🚀結果。",
                            "citations": [
                                {"path": "帳務🚀.py", "start_line": 3, "end_line": 4}
                            ],
                        },
                    ]
                },
                "answer_path": "執行🚀/ANSWER.txt",
                "explanation_path": "執行🚀/explanation.json",
                "manifest_path": "執行🚀/manifest.json",
                "coverage": {"observed": {"files": 1}, "admitted": {"files": 2}},
            },
        }
        failure = {
            "ok": False,
            "status": "backend_error🚀",
            "question": "帳務流程🚀為何失敗？",
            "run_root": "執行🚀",
            "server": None,
            "outcome": {
                "failure_reason": "模型失敗🚀",
                "answer_path": "執行🚀/ANSWER.txt",
                "manifest_path": "執行🚀/manifest.json",
            },
            "error": None,
        }

        for stream_name, result in (("stdout", success), ("stderr", failure)):
            with self.subTest(stream=stream_name):
                raw = io.BytesIO()
                stream = io.TextIOWrapper(
                    raw,
                    encoding="cp950",
                    errors="strict",
                    newline="\n",
                    write_through=True,
                )
                try:
                    with patch.object(cli_module.sys, stream_name, stream):
                        cli_module._emit_explain_human(result)
                    rendered = raw.getvalue().decode("cp950")
                finally:
                    stream.detach()

                self.assertNotIn("🚀", rendered)
                self.assertIn("\\U0001f680", rendered)
                self.assertIn("執行", rendered)

    def test_dynamic_progress_is_cp950_safe_and_json_mode_stays_silent(self) -> None:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(
            raw,
            encoding="cp950",
            errors="strict",
            newline="\n",
            write_through=True,
        )
        try:
            with patch.object(cli_module.sys, "stderr", stream):
                cli_module._progress(
                    SimpleNamespace(as_json=False),
                    "retained E1: 帳務🚀.py L1-L20",
                )
            rendered = raw.getvalue().decode("cp950")
        finally:
            stream.detach()

        self.assertIn("帳務", rendered)
        self.assertIn("\\U0001f680", rendered)

        silent = io.StringIO()
        with patch.object(cli_module.sys, "stderr", silent):
            cli_module._progress(
                SimpleNamespace(as_json=True),
                "retained E1: 帳務🚀.py L1-L20",
            )
        self.assertEqual(silent.getvalue(), "")

    def test_explain_failures_offer_status_specific_recovery(self) -> None:
        cases = (
            ("insufficient_evidence", "exact file or symbol"),
            ("parse_budget_exhausted", "invalid read-only action or citation"),
            ("stalled", "recorded failure reason and observed/unread coverage"),
            ("source_drift", "concurrent repository changes"),
            ("acceptance_gate_failed", "lingering llama-server"),
            ("backend_error", "pinned runtime/model assets"),
            ("port_collision", "127.0.0.1:18080"),
            ("startup_timeout", "server logs"),
            ("unsupported_platform", "native Linux Python in WSL"),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertIn(expected, cli_module._next_explain_step(status))
        self.assertNotIn(
            "Ask a narrower question",
            cli_module._next_explain_step("parse_budget_exhausted"),
        )
        self.assertIn(
            "unread coverage",
            cli_module._next_explain_step("stalled"),
        )
        self.assertNotIn(
            "narrower question",
            cli_module._next_explain_step("stalled"),
        )

    def test_explain_exit_statuses_distinguish_quality_interrupt_and_infrastructure(self) -> None:
        args = SimpleNamespace(as_json=True)

        def finish(status: str, *, outcome: object = None, code: int | None = None) -> int:
            with redirect_stdout(io.StringIO()):
                return cli_module._finish_explain(
                    args, status, Path("repo"), None, None,
                    outcome=outcome, code=code,
                )

        answered = SimpleNamespace(ok=True, as_dict=lambda: {})
        self.assertEqual(finish("answered", outcome=answered), 0)
        self.assertEqual(finish("interrupted"), 130)
        for status in ("insufficient_evidence", "stalled", "action_budget_exhausted"):
            with self.subTest(status=status):
                self.assertEqual(finish(status), 1)
        for status in (
            "acceptance_gate_failed",
            "artifact_drift",
            "backend_error",
            "evidence_drift",
            "ingress_drift",
            "source_drift",
            "snapshot_drift",
            "tool_error",
        ):
            with self.subTest(status=status):
                self.assertEqual(finish(status), 2)

    def test_explain_json_orchestration_binds_cleanup_evidence_and_clears_secrets(self) -> None:
        log_bytes = b"verified local server output\n"
        outcome_payload = {
            "status": "answered",
            "ok": True,
            "answer": {"claims": []},
            "answer_path": "ANSWER.txt",
            "explanation_path": "explanation.json",
            "manifest_path": "manifest.json",
            "coverage": {"observed": {"files": 1}, "admitted": {"files": 1}},
        }
        outcome = SimpleNamespace(
            status="answered", ok=True, as_dict=lambda: outcome_payload,
        )
        captured: dict[str, object] = {}

        class FakeSupervisor:
            instance: FakeSupervisor | None = None

            def __init__(self, plan: object, *, log_directory: Path, log_root: Path) -> None:
                self.plan = plan
                captured["log_root"] = log_root
                self.api_key = "ephemeral-test-secret"
                self.start_result = None
                self.shutdown_result = None
                self.stop_calls = 0
                log_directory.mkdir(parents=True)
                (log_directory / "stdout.log").write_bytes(log_bytes)
                FakeSupervisor.instance = self

            def __enter__(self) -> FakeSupervisor:
                self.start_result = SimpleNamespace(
                    ok=True, status="ready", error=None, health_last_error=None,
                    as_dict=lambda: {"ok": True, "status": "ready"},
                )
                return self

            def stop(self) -> object:
                self.stop_calls += 1
                self.api_key = None
                if self.shutdown_result is None:
                    self.shutdown_result = SimpleNamespace(
                        ok=True, status="terminated", return_code=0,
                        as_dict=lambda: {
                            "ok": True, "status": "terminated", "return_code": 0,
                        },
                    )
                return self.shutdown_result

            def __exit__(self, *_args: object) -> None:
                self.stop()

        class FakeTransport:
            instances: list[FakeTransport] = []

            def __init__(self, endpoint: str, *, api_key: str, timeout_seconds: float) -> None:
                self.endpoint = endpoint
                self.api_key: str | None = api_key
                self.timeout_seconds = timeout_seconds
                self.instances.append(self)

        def run_explanation(
            prepared: object, transport: object, *, model: str,
            acceptance_gate: object, progress: object,
        ) -> object:
            captured.update(
                prepared=prepared, transport=transport, model=model, progress=progress
            )
            progress("retained E1: 帳務🚀.py L1-L20")
            captured["gate"] = acceptance_gate()
            return outcome

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = root / "source"
            assets = root / "assets"
            source.mkdir()
            assets.mkdir()
            plan = SimpleNamespace(
                endpoint="http://127.0.0.1:8042/v1",
                model_manifest_path=assets / "model.json",
            )
            preparation = SimpleNamespace(
                ok=True, status="ready", errors=(), plan=plan,
                as_dict=lambda: {"ok": True, "status": "ready"},
            )
            state = root / "private-state"
            prepared = SimpleNamespace(run_root=state / "runs" / "explain-test")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, {"FORGE8_STATE_HOME": str(state)}),
                patch.object(cli_module, "_resolve_fix_asset_root", return_value=assets),
                patch.object(cli_module, "_new_task_id", return_value="explain-test"),
                patch.object(cli_module, "prepare_explanation", return_value=prepared),
                patch.object(cli_module, "prepare_server", return_value=preparation),
                patch.object(cli_module, "_fix_model_id", return_value="pinned-model"),
                patch.object(cli_module, "LocalServerSupervisor", FakeSupervisor),
                patch.object(cli_module, "OpenAITransport", FakeTransport),
                patch.object(cli_module, "run_explanation", side_effect=run_explanation),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main([
                    "explain", str(source), "--question", "追蹤🚀 billing", "--json",
                ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(stdout.getvalue().isascii())
        payload = json.loads(stdout.getvalue())
        self.assertEqual((payload["status"], payload["ok"]), ("answered", True))
        self.assertEqual(payload["question"], "追蹤🚀 billing")
        self.assertEqual(payload["outcome"], outcome_payload)
        self.assertEqual(Path(payload["run_root"]), prepared.run_root)
        self.assertEqual(captured["log_root"], prepared.run_root)
        self.assertTrue(callable(captured["progress"]))
        gate = captured["gate"]
        self.assertTrue(gate.ok)
        self.assertEqual(gate.evidence["lifecycle"]["shutdown"]["status"], "terminated")
        self.assertTrue(gate.evidence["lifecycle"]["supervisor_secret_cleared"])
        self.assertTrue(gate.evidence["lifecycle"]["transport_secret_cleared"])
        self.assertEqual(gate.evidence["server_logs"], [{
            "path": "server-logs/stdout.log",
            "size_bytes": len(log_bytes),
            "sha256": hashlib.sha256(log_bytes).hexdigest(),
        }])
        self.assertIsNone(FakeSupervisor.instance.api_key)
        self.assertIsNone(FakeTransport.instances[0].api_key)

    def test_asset_verifier_commands_dispatch_with_explicit_paths(self) -> None:
        cases = (
            ("runtime", "verify_runtime"),
            ("model", "verify_model"),
        )
        for command, verifier in cases:
            stdout = io.StringIO()
            with (
                self.subTest(command=command),
                patch("forge8.cli._resolve_fix_asset_root") as resolve_assets,
                patch(f"forge8.cli.{verifier}", return_value=_PassingIntegrity()) as verify,
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        command,
                        "verify",
                        "--root",
                        "asset-root",
                        "--manifest",
                        "asset-manifest.json",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            resolve_assets.assert_not_called()
            verify.assert_called_once_with(
                Path("asset-manifest.json"), Path("asset-root")
            )
            self.assertEqual(json.loads(stdout.getvalue()), _PassingIntegrity.as_dict())

    def test_asset_verifier_defaults_share_the_configured_asset_root(self) -> None:
        asset_root = Path("trusted-forge8-home")
        cases = (
            (
                "runtime",
                "verify_runtime",
                Path("config/runtimes/llama_cpp_b10621.json"),
                Path("runtime"),
            ),
            (
                "model",
                "verify_model",
                Path("config/models/gemma4_e4b_qat_q4.json"),
                Path("models"),
            ),
        )
        for system in ("Windows", "Linux"):
            for command, verifier, manifest, root in cases:
                if command == "runtime" and system == "Linux":
                    manifest = cli_module._LINUX_RUNTIME_MANIFEST
                stdout = io.StringIO()
                with (
                    self.subTest(command=command, system=system),
                    patch.object(cli_module.platform, "system", return_value=system),
                    patch(
                        "forge8.cli._resolve_fix_asset_root",
                        return_value=asset_root,
                    ) as resolve_assets,
                    patch(f"forge8.cli.{verifier}", return_value=_PassingIntegrity()) as verify,
                    redirect_stdout(stdout),
                ):
                    exit_code = main([command, "verify", "--json"])

                self.assertEqual(exit_code, 0)
                resolve_assets.assert_called_once_with()
                verify.assert_called_once_with(asset_root / manifest, asset_root / root)
                self.assertEqual(json.loads(stdout.getvalue()), _PassingIntegrity.as_dict())

    def test_native_asset_selection_is_independent_of_the_parent_shell(self) -> None:
        for system, expected_runtime, expected_profile in (
            ("Windows", cli_module._FIX_RUNTIME_MANIFEST, cli_module._FIX_PROFILE),
            ("Linux", cli_module._LINUX_RUNTIME_MANIFEST, cli_module._LINUX_PROFILE),
        ):
            with self.subTest(system=system), patch.object(
                cli_module.platform, "system", return_value=system
            ):
                self.assertEqual(
                    cli_module._native_asset_paths(), (expected_runtime, expected_profile)
                )
                self.assertEqual(
                    cli_module._fix_asset_anchors(Path("assets"))[:3],
                    (
                        Path("assets") / expected_runtime,
                        Path("assets") / cli_module._FIX_MODEL_MANIFEST,
                        Path("assets") / expected_profile,
                    ),
                )

    def test_both_workflows_pass_native_pins_to_the_shared_server(self) -> None:
        for system in ("Windows", "Linux"):
            for command in ("fix", "explain"):
                with self.subTest(system=system, command=command), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    source, assets = root / "source", root / "assets"
                    source.mkdir()
                    assets.mkdir()
                    preflight = SimpleNamespace(
                        repairable=True, status="failed", reason="fixture failure", checks=(),
                    )
                    prepared = SimpleNamespace(baseline_preflight=lambda: preflight)
                    argv = [command, str(source), "--json"]
                    if command == "fix":
                        argv += ["--goal", "fix it", "--allow-write", "app.py", "--check", "python_unittest"]
                    else:
                        argv += ["--question", "explain this"]
                    with (
                        patch.object(cli_module.platform, "system", return_value=system),
                        patch.object(cli_module, "_load_deployment", return_value=None),
                        patch.object(cli_module, "_resolve_fix_asset_root", return_value=assets),
                        patch.object(cli_module, "prepare_repository_repair", return_value=prepared),
                        patch.object(cli_module, "prepare_explanation", return_value=prepared),
                        patch.object(cli_module, "prepare_server", side_effect=ValueError("stop before inference")) as server,
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(main(argv), 2)
                        runtime_manifest, profile = cli_module._native_asset_paths()
                    server.assert_called_once_with(
                        assets, runtime_manifest_path=runtime_manifest, profile_path=profile,
                    )

    def test_unsupported_native_platform_is_actionable_without_breaking_help(self) -> None:
        with patch.object(cli_module.platform, "system", return_value="Darwin"):
            with self.assertRaisesRegex(ValueError, "no pinned native runtime.*Darwin"):
                cli_module._native_asset_paths()
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                main(["--help"])
            self.assertEqual(raised.exception.code, 0)
        self.assertIn("{configure,doctor,experiment,fix,explain,locate,ask,read,observe,runtime,model}", output.getvalue())

    def test_asset_verifier_help_explains_home_defaults(self) -> None:
        for command, expected_manifest, expected_root in (
            (
                "runtime",
                "FORGE8_HOME/config/runtimes/llama_cpp_b10621.json",
                "FORGE8_HOME/runtime",
            ),
            (
                "model",
                "FORGE8_HOME/config/models/gemma4_e4b_qat_q4.json",
                "FORGE8_HOME/models",
            ),
        ):
            stdout = io.StringIO()
            with self.subTest(command=command), redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as raised:
                    main([command, "--help"])

            self.assertEqual(raised.exception.code, 0)
            normalized = " ".join(stdout.getvalue().split())
            self.assertIn(expected_manifest, normalized)
            self.assertIn(expected_root, normalized)

    def test_asset_verifier_configuration_errors_are_actionable(self) -> None:
        for command, verifier, label in (
            ("runtime", "verify_runtime", "Runtime"),
            ("model", "verify_model", "Model"),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                self.subTest(command=command),
                patch(
                    f"forge8.cli.{verifier}",
                    side_effect=FileNotFoundError("manifest is missing"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        command,
                        "verify",
                        "--root",
                        "asset-root",
                        "--manifest",
                        "asset-manifest.json",
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(
                stderr.getvalue(),
                f"{label} verification ERROR: "
                "FileNotFoundError: manifest is missing\n",
            )

    def test_asset_verifier_configuration_error_json_stays_machine_readable(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch(
                "forge8.cli.verify_runtime",
                side_effect=ValueError("unsupported manifest schema"),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = main(
                [
                    "runtime",
                    "verify",
                    "--root",
                    "asset-root",
                    "--manifest",
                    "asset-manifest.json",
                    "--json",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "schema_version": 1,
                "kind": "forge8.runtime.verify",
                "status": "configuration_error",
                "ok": False,
                "error": "ValueError: unsupported manifest schema",
            },
        )


class ProjectQuestionCLITests(unittest.TestCase):
    def run_question(self, *, reader="qwen35", large=False, capacity=False, many_files=False, empty=False,
            fault=None, phase=None, answer=None, finish="stop", emit=True, source_text=None):
        from forge8.inference import ChatResponse
        from forge8.server import ServerPreparation
        from test_fix_cli import _supervisor_type

        # Defaults follow the selected wire contract. Explicit responses are raw
        # supplied bytes/text, including malformed cases; never auto-repair them.
        if answer is None:
            answer = (json.dumps(answer_document("It returns 7.")) if reader == "qwen35"
                else "It returns 7. [E1:L3-L4]")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        source, assets, state = root / "source", root / "assets", root / "state"
        source.mkdir(); assets.mkdir()
        content = 'raise RuntimeError("never execute source")\n\ndef answer():\n    return 7\n'
        if source_text is not None:
            content = source_text
        if capacity:
            content = 'raise RuntimeError("never execute source")\n\ndef answer():\n' + "    # body\n" * 59 + "    return 7\n"
        if large:
            content += "\n".join(f"def other_{i}_{'x' * 60}(): return 7" for i in range(90))
            (source / "z.py").write_text(content, encoding="utf-8")
        (source / "app.py").write_text(content, encoding="utf-8")
        if capacity or many_files:
            for name in ("b.py", "c.py", "d.py"):
                (source / name).write_text(content, encoding="utf-8")
        model = "qwen35-9b-q4-k-m" if reader == "qwen35" else "gemma4-12b-qat-q4"
        manifest = assets / "model.json"
        manifest.write_text(json.dumps({"id": model}), encoding="utf-8")
        plan = SimpleNamespace(endpoint="http://127.0.0.1:18080", model_manifest_path=manifest,
            as_dict=lambda: {"endpoint": "http://127.0.0.1:18080"})
        supervisor = _supervisor_type(shutdown_ok=fault != "cleanup")
        cancel = threading.Event()
        results, phases, chunks, transports = [], [], [], []
        ids = [] if empty else ["D0001", "D0002", "D0003", "D0004"] if capacity or many_files else ["D0001"]
        discovery_responses = ([json.dumps({"files": ["F0001"]})] if large else []) + [json.dumps({"candidates": ids})]
        test = self

        class Transport:
            def __init__(self, endpoint, *, api_key, on_text=None, cancel_event=None, **kwargs):
                # A second server may never start while the first still owns a key.
                if transports:
                    test.assertIsNone(transports[0].api_key)
                    test.assertIsNone(supervisor.instances[0].api_key)
                    test.assertTrue(supervisor.instances[0].shutdown_result.ok)
                self.api_key, self.on_text, self.cancel_event = api_key, on_text, cancel_event
                self.requests = []
                self.index = len(transports)
                transports.append(self)

            def chat(self, request):
                self.requests.append(request)
                if self.index == 0:
                    text = '{"candidates":["UNKNOWN"]}' if fault == "invalid_ids" else discovery_responses[len(self.requests) - 1]
                    return ChatResponse(text, "stop", {}, {})
                if self.on_text:
                    self.on_text(answer)
                return ChatResponse(answer, finish, {}, {})

        def progress(message):
            phases.append(message)
            if message == phase:
                if fault == "cancel":
                    cancel.set()
                elif fault == "source":
                    (source / "app.py").write_text(content + "# changed\n", encoding="utf-8")
                elif fault == "catalogue":
                    path = next(state.glob("runs/project-*/discovery-input.json"))
                    path.write_text("{}", encoding="utf-8")

        original_planner = cli_module.plan_project_focus
        def planner(*args):
            try:
                return original_planner(*args)
            finally:
                if fault == "planner_cancel":
                    cancel.set()

        with (patch.object(cli_module, "_load_deployment", return_value=None),
              patch.dict(os.environ, {"FORGE8_STATE_HOME": str(state)}),
              patch.object(cli_module, "_resolve_fix_asset_root", return_value=assets),
              patch.object(cli_module, "prepare_server", return_value=ServerPreparation("ready", (), plan=plan)) as prepare,
              patch.object(cli_module, "LocalServerSupervisor", supervisor),
              patch.object(cli_module, "plan_project_focus", side_effect=planner),
              patch.object(cli_module, "CancellableTransport", Transport)):
            args = build_parser().parse_args(["ask", str(source), "--question", "  What does answer return?\n", "--reader", reader, "--json"])
            code = cli_module._run_project_cli(args, on_result=results.append if emit else None,
                on_progress=progress, on_text=chunks.append, cancel_event=cancel)
        return SimpleNamespace(code=code, results=results, phases=phases, chunks=chunks,
            transports=transports, supervisors=supervisor.instances, prepares=prepare.call_count,
            source=source, content=content, state=state, args=args)

    def test_project_uses_real_complete_source_in_second_session_and_one_final_result(self):
        for reader, large in (("qwen35", False), ("gemma12b", False), ("qwen35", True)):
            with self.subTest(reader=reader, large=large):
                run = self.run_question(reader=reader, large=large)
                self.assertEqual(run.code, 0, run.results)
                self.assertEqual(len(run.results), 1)
                result = run.results[0]
                self.assertEqual(result["kind"], "forge8.explain")
                self.assertTrue(result["ok"])
                self.assertEqual(result["question"], run.args.question)
                project = result["project_reading"]
                self.assertTrue(project["answer_attempted"])
                self.assertEqual(project["focus"], [{"path": "app.py", "start_line": 3, "end_line": 4}])
                self.assertNotEqual(result["run_root"], project["discovery"]["run_root"])
                self.assertEqual([len(t.requests) for t in run.transports], [2 if large else 1, 1])
                self.assertEqual(run.prepares, 2)
                self.assertIsNone(run.transports[0].on_text)
                self.assertEqual(run.chunks, ["It returns 7." if reader == "qwen35" else "It returns 7. [E1:L3-L4]"])
                request = run.transports[1].requests[0]
                self.assertEqual(request.response_format is not None, reader == "qwen35")
                self.assertIsNone(request.reasoning_budget_tokens)
                self.assertIsNone(request.enable_thinking)
                self.assertIn("     3|def answer():\n     4|    return 7", request.messages[1].content)
                self.assertTrue(request.messages[1].content.endswith(run.args.question))
                self.assertEqual(request.max_tokens, 4096)
                self.assertEqual(request.temperature, 0.6 if reader == "qwen35" else 1.0)
                self.assertTrue(all(t.api_key is None for t in run.transports))
                self.assertTrue(all(s.api_key is None and s.shutdown_result.ok for s in run.supervisors))
                self.assertEqual((run.source / "app.py").read_text(encoding="utf-8"), run.content)
                trace = [json.loads(line) for line in (Path(result["run_root"]) / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
                reads = [row for row in trace if row.get("payload", {}).get("tool") == "read_text"]
                self.assertTrue(reads, trace)
                self.assertTrue(all(row["payload"]["origin"] == "project_candidates" for row in reads))

    def test_same_file_constant_and_helper_reach_real_second_stage_without_extra_calls(self):
        content = ('LIMIT = 7\nraise RuntimeError("never execute source")\n\n'
            'def answer():\n    return helper(LIMIT)\n\n'
            'def helper(value):\n    return value + 1\n')
        run = self.run_question(source_text=content,
            answer=json.dumps(answer_document("It returns 8.", [citation(start_line=1, end_line=8)])))
        self.assertEqual(run.code, 0, run.results)
        result = run.results[0]
        self.assertEqual([len(t.requests) for t in run.transports], [1, 1])
        self.assertEqual(run.prepares, 2)
        meta = result["project_reading"]["context"]
        self.assertEqual(meta, {"added": [
            {"path": "app.py", "start_line": 1, "end_line": 1},
            {"path": "app.py", "start_line": 7, "end_line": 8}], "skipped": {}})
        context = run.transports[1].requests[0].messages[1].content
        self.assertIn("     1|LIMIT = 7", context)
        self.assertIn("     5|    return helper(LIMIT)", context)
        self.assertIn("     8|    return value + 1", context)
        self.assertIn("app.py:1-1", context)
        self.assertIn("app.py:7-8", context)
        self.assertIn("lexical", context)
        self.assertTrue(context.endswith(run.args.question))
        ingress = json.loads((Path(result["run_root"]) / "input/ingress.json").read_text(encoding="utf-8"))
        self.assertEqual(ingress["context_supplements"], ["app.py:1-1", "app.py:7-8"])
        self.assertEqual((run.source / "app.py").read_text(encoding="utf-8"), content)
        self.assertTrue(all(t.api_key is None for t in run.transports))
        self.assertTrue(all(s.api_key is None and s.shutdown_result.ok for s in run.supervisors))

    def test_capacity_or_empty_discovery_preserves_every_candidate_without_answer_model(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                run = self.run_question(capacity=not empty, empty=empty)
                self.assertEqual(run.code, 1, run.results)
                result = run.results[0]
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "selection_required")
                self.assertIsNone(result["outcome"])
                project = result["project_reading"]
                self.assertEqual(len(project["discovery"]["candidates"]), 0 if empty else 4)
                self.assertEqual(project["focus"], [])
                self.assertFalse(project["answer_attempted"])
                self.assertEqual(run.prepares, 1)
                self.assertEqual(len(run.transports), 1)
                self.assertEqual(cli_module._explain_gpu_state(result["server"]), "released")

    def test_project_reads_four_small_source_files_without_expanding_manual_focus(self):
        run = self.run_question(many_files=True)
        self.assertEqual(run.code, 0, run.results)
        result = run.results[0]
        self.assertEqual(len(result["project_reading"]["focus"]), 4)
        self.assertEqual(result["outcome"]["evidence_count"], 4)
        self.assertEqual(result["outcome"]["inference"]["calls"], 1)
        ingress = json.loads((Path(result["run_root"]) / "input" / "ingress.json").read_text(encoding="utf-8"))
        self.assertEqual(ingress["focus_origin"], "project_candidates")
        with self.assertRaises(cli_module.ExplanationError):
            cli_module._focus_actions(tuple(f"{name}:3-4" for name in ("app.py", "b.py", "c.py", "d.py")))

    def test_cancel_during_capacity_check_does_not_publish_manual_fallback(self):
        run = self.run_question(capacity=True, fault="planner_cancel")
        self.assertEqual(run.code, 130)
        self.assertEqual(run.results[0]["status"], "interrupted")
        self.assertNotIn("project_reading", run.results[0])
        self.assertEqual(run.prepares, 1)

    def test_keyboard_interrupt_captured_by_postscan_stays_an_interruption(self):
        with patch.object(cli_module, "_discovery_integrity", return_value=(
                (False, "KeyboardInterrupt: source scan was interrupted"), (True, None), (True, None))):
            run = self.run_question()
        self.assertEqual(run.code, 130)
        self.assertEqual(run.results[0]["status"], "interrupted")
        self.assertNotIn("project_reading", run.results[0])
        self.assertEqual(run.prepares, 1)

    def test_cancel_drift_and_failed_locator_never_start_answer_or_offer_capacity_fallback(self):
        phases = ["Project question: preparing complete candidate source.",
            "Project question: answering from the automatically selected source."]
        cases = [(fault, phase) for fault in ("cancel", "source", "catalogue") for phase in phases]
        cases += [("invalid_ids", None), ("cleanup", None)]
        for fault, phase in cases:
            with self.subTest(fault=fault, phase=phase):
                run = self.run_question(fault=fault, phase=phase)
                self.assertNotEqual(run.code, 0)
                self.assertEqual(len(run.results), 1)
                self.assertFalse(run.results[0]["ok"])
                self.assertNotIn("project_reading", run.results[0])
                self.assertNotEqual(run.results[0]["status"], "selection_required")
                self.assertEqual(run.prepares, 1)
                self.assertEqual(len(run.transports), 1)
                self.assertEqual(run.chunks, [])

    def test_second_stage_truncation_and_unverified_references_keep_existing_acceptance_rules(self):
        for reader, finish in ((reader, finish) for reader in ("qwen35", "gemma12b") for finish in ("stop", "length")):
            with self.subTest(reader=reader, finish=finish):
                text = (json.dumps(answer_document("It returns 7.", [citation(start_line=99, end_line=99)]))
                    if reader == "qwen35" else "It returns 7. [E1:L99-L99]")
                run = self.run_question(reader=reader, answer=text, finish=finish)
                self.assertNotEqual(run.code, 0)
                result = run.results[0]
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "stalled")
                self.assertIsNone(result["outcome"]["answer"])
                self.assertEqual(len(run.transports), 2)
                self.assertEqual(bool(cli_module._unverified_explain_prose(result)), reader == "gemma12b" and finish == "stop")
                if reader == "qwen35":
                    self.assertNotIn("unverified_prose", result["outcome"])
                    self.assertEqual(run.chunks, ["It returns 7."])
                self.assertEqual(result["kind"], "forge8.explain")
                self.assertEqual(cli_module._explain_gpu_state(result["server"]), "released")

    def test_json_cli_emits_one_final_result_not_an_intermediate_locator_document(self):
        output = io.StringIO()
        with redirect_stdout(output):
            run = self.run_question(emit=False)
        self.assertEqual(run.code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["kind"], "forge8.explain")
        self.assertTrue(payload["project_reading"]["answer_attempted"])


if __name__ == "__main__":
    unittest.main()
