"""CLI dispatch and presentation only: no guest, worker or model executes."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge8 import cli, experiments


class ExperimentCLITests(unittest.TestCase):
    def test_watch_requires_explicit_trace_consent_and_valid_names_before_run_state(self):
        options = [
            ["--watch-local", "value"],
            ["--trace-lines", "--watch-local", "value", "--watch-local", "value"],
            ["--trace-lines", "--watch-local", "class"],
            ["--trace-lines", "--watch-local", "名稱"],
            ["--trace-lines", "--watch-local", "a" * 65],
            ["--trace-lines", "--watch-local", "a", "--watch-local", "b", "--watch-local", "c", "--watch-local", "d"],
            ["--trace-lines", "--watch-local", "value", "--generator-steps", "2"],
            ["--trace-lines", "--watch-local", "value", "--module-root", str(self.source.parent), "--module-file", "owned.py"],
        ]
        for flags in options:
            with self.subTest(flags=flags):
                self.assertEqual(self.invoke(self.run_args() + flags)[0], 2)
        self.assertEqual(self.invoke(self.run_args(consent=False) + ["--trace-lines", "--watch-local", "value"])[0], 2)
        self.parent.assert_not_called()
        self.run_call.assert_not_called()

    def test_watch_forwards_tuple_and_prints_json_text_without_losing_integer_precision(self):
        self.report["reported_trace"] = {"line_events": [1], "truncated": False, "hook_intact": True,
            "watch": {"names": ["value", "other"], "truncated": True, "events": [
                {"event": "line", "line": 1, "call_id": 1, "values": {
                    "value": {"state": "value", "json": "9007199254740993"},
                    "other": {"state": "unbound"}}},
                {"event": "return", "line": 1, "call_id": 1, "values": {
                    "value": {"state": "unsupported"}, "other": {"state": "limited"}}}]}}
        code, output, _ = self.invoke(self.run_args(as_json=False) +
            ["--trace-lines", "--watch-local", "value", "--watch-local", "other"])
        self.assertEqual(code, 0)
        self.assertEqual(self.run_call.call_args.kwargs,
            {"allow_execution": True, "trace_lines": True, "watch_names": ("value", "other")})
        for text in ("9007199254740993", "[unbound]", "[unsupported]", "[limited]",
                "Call #1, return", "untrusted snapshots before lines", "return events may be unwinding",
                "event or byte limit"):
            self.assertIn(text, output)


    def test_generator_switch_requires_consent_and_forwards_exact_limit(self):
        self.assertEqual(self.invoke(self.run_args(consent=False) + ["--generator-steps", "3"])[0], 2)
        self.run_call.assert_not_called()
        code, _, errors = self.invoke(self.run_args() + ["--generator-steps", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(self.run_call.call_args.kwargs, {"allow_execution": True, "generator_steps": 3})
        self.assertIn("at most 3", errors)
        self.assertIn("explicitly close", errors)
        self.assertIn("does not prove exhaustion", errors)

    def test_generator_rejects_incompatible_modes_before_allocating_run(self):
        for options in (["--trace-lines"], ["--module-root", str(self.source.parent), "--module-file", "owned.py"]):
            with self.subTest(options=options):
                self.assertEqual(self.invoke(self.run_args() + ["--generator-steps", "2"] + options)[0], 2)
        self.parent.assert_not_called()
        self.run_call.assert_not_called()

    def test_generator_missing_observation_or_failed_process_is_not_success(self):
        self.report["reported_result"] = None
        self.assertEqual(self.invoke(self.run_args() + ["--generator-steps", "2"])[0], 2)
        self.report["reported_result"] = {"generator": {}}
        self.report["process_status"] = "timed_out"
        self.assertEqual(self.invoke(self.run_args() + ["--generator-steps", "2"])[0], 2)

    def test_trace_switch_requires_consent_and_forwards_only_true(self):
        code, output, _ = self.invoke(self.run_args(consent=False) + ["--trace-lines"])
        self.assertEqual(code, 2)
        self.run_call.assert_not_called()
        self.assertEqual(self.invoke(self.run_args() + ["--trace-lines"])[0], 0)
        self.assertEqual(self.run_call.call_args.kwargs, {"allow_execution": True, "trace_lines": True})

    def test_trace_module_set_is_rejected_before_run_state(self):
        code, output, _ = self.invoke(self.run_args() + ["--trace-lines", "--module-root",
            str(self.source.parent), "--module-file", "owned.py"])
        self.assertEqual(code, 2)
        self.assertIn("single-file", json.loads(output)["error"])
        self.parent.assert_not_called()
        self.run_call.assert_not_called()

    def test_trace_cli_prints_untrusted_sequence_and_explicit_incomplete_states(self):
        self.report["reported_trace"] = {"line_events": [1] + [2] * 999, "truncated": True, "hook_intact": False}
        self.report["execution"]["guest_output"]["stdout"] = (
            "FORGE8_GUEST_TRACE=" + json.dumps(self.report["reported_trace"]) +
            "\nFORGE8_GUEST_RESULT=" + json.dumps(self.report["reported_result"]))
        code, output, _ = self.invoke(self.run_args(as_json=False) + ["--trace-lines"])
        self.assertEqual(code, 0)
        self.assertIn("[1, 2, 2,", output)
        self.assertIn("untrusted; lines precede execution", output)
        self.assertIn("first 1000", output)
        self.assertIn("hook changed", output)
        self.assertNotIn("FORGE8_GUEST_TRACE=", output)
        self.report["reported_trace"] = None
        self.assertIn("not an empty or complete execution path",
            self.invoke(self.run_args(as_json=False) + ["--trace-lines"])[1])

    def test_module_mode_forwards_only_explicit_import_root_and_complete_file_list(self):
        names = ["exporter/__init__.py", "exporter/escaping.py", "exporter/rows.py"]
        args = self.run_args() + ["--module-root", str(self.source.parent)]
        for name in names:
            args += ["--module-file", name]
        self.assertEqual(self.invoke(args)[0], 0)
        self.assertEqual(self.run_call.call_args.kwargs,
            {"allow_execution": True, "module_root": self.source.parent, "module_files": names})

    def test_half_selected_module_mode_is_rejected_before_run_state_or_worker(self):
        for options in (["--module-root", str(self.source.parent)], ["--module-file", "owned.py"]):
            with self.subTest(options=options):
                self.assertEqual(self.invoke(self.run_args() + options)[0], 2)
        self.parent.assert_not_called()
        self.run_call.assert_not_called()

    def test_read_requires_explicit_experiment_opt_in_without_changing_default_dispatch(self):
        with patch("forge8.desk.run_desk", return_value=0) as desk:
            self.assertEqual(self.invoke(["read", str(self.source.parent)])[0], 0)
            desk.assert_called_once_with(self.source.parent)
            desk.reset_mock()
            self.assertEqual(self.invoke(["read", str(self.source.parent), "--allow-experiments"])[0], 0)
            desk.assert_called_once_with(self.source.parent, allow_experiments=True)

    def test_read_exit_warns_and_returns_failure_when_experiment_cleanup_is_unknown(self):
        native_desk = Mock(experiment={"cleanup_unknown": True})
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch("forge8.desk.ReadingDesk", return_value=native_desk), \
                patch("forge8.desk.make_server", return_value=(server, "owned-placeholder-not-a-private-url")):
            code, _, errors = self.invoke(["read", str(self.source.parent), "--allow-experiments"])
        self.assertEqual(code, 2)
        self.assertIn("程序清理未確認", errors)
        native_desk.close.assert_called_once_with()
        server.server_close.assert_called_once_with()

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="forge8-experiment-cli-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.runtime = self.base / "standalone-runtime"
        self.source = self.base / "project" / "owned.py"
        self.inputs = self.base / "inputs.json"
        self.archives = self.base / "local-archives"
        self.runs = self.base / "private-state" / "runs"
        self.report = {
            "schema_version": 1, "kind": "forge8.experiment",
            "source_unchanged": True, "runtime_unchanged": True,
            "process_status": "passed", "duration_seconds": 0.25, "elapsed_seconds": 0.5,
            "execution": {
                "host_status": "exited", "detail": None, "output_limit": False,
                "guest_output": {"stdout": "", "stderr": ""},
            },
            "reported_result": {"return": {"count": 3}},
        }
        self.setup_result = {"runtime": str(self.runtime), "network_used": False}
        self.prepare = self.start(patch.object(experiments, "prepare_request", return_value=({}, {})))
        self.run_call = self.start(patch.object(experiments, "run_experiment", return_value=self.report))
        self.setup = self.start(patch.object(experiments, "setup_runtime", return_value=self.setup_result))
        self.saved = self.start(patch.object(cli, "_load_deployment", return_value=None))
        self.parent = self.start(patch.object(cli, "_fix_runs_parent", return_value=self.runs))
        self.task_id = self.start(patch.object(cli, "_new_task_id", return_value="owned-experiment"))
        self.start(patch.object(experiments, "native_platform", return_value="linux"))
        self.start(patch.dict(os.environ, {}, clear=True))
        for owner, name in (
            (cli, "_resolve_fix_asset_root"), (cli, "_load_json_object"),
            (cli, "prepare_server"), (experiments, "_worker"), (Path, "cwd"),
        ):
            self.start(patch.object(owner, name, side_effect=AssertionError(f"unexpected {name}")))

    def start(self, manager):
        result = manager.start()
        self.addCleanup(manager.stop)
        return result

    def run_args(self, *, consent=True, as_json=True, runtime=True):
        args = ["experiment", "run", str(self.source), "--entry", "entry", "--input", str(self.inputs)]
        if runtime:
            args += ["--runtime", str(self.runtime)]
        if consent:
            args.append("--allow-execution")
        if as_json:
            args.append("--json")
        return args

    def invoke(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(args)
        return code, output.getvalue(), errors.getvalue()

    def test_parser_dispatch_preserves_explicit_actions_paths_and_flags(self) -> None:
        with patch.object(cli, "_run_experiment_cli", return_value=17) as dispatch:
            self.assertEqual(self.invoke(self.run_args())[0], 17)
            args = dispatch.call_args.args[0]
            self.assertEqual((args.command, args.experiment_action), ("experiment", "run"))
            self.assertEqual((args.source, args.entry, args.input), (self.source, "entry", self.inputs))
            self.assertEqual(args.runtime, self.runtime)
            self.assertIs(args.allow_execution, True)
            self.assertIs(args.as_json, True)
            self.assertEqual(self.invoke([
                "experiment", "setup", "--archives", str(self.archives),
                "--runtime", str(self.runtime), "--json",
            ])[0], 17)
            args = dispatch.call_args.args[0]
            self.assertEqual(args.experiment_action, "setup")
            self.assertEqual(args.archives, self.archives)
            self.assertEqual(dispatch.call_count, 2)
        self.run_call.assert_not_called()
        self.setup.assert_not_called()

    def test_missing_consent_precedes_path_settings_admission_and_run_allocation(self) -> None:
        self.saved.side_effect = AssertionError("settings read before consent")
        with patch.object(cli, "_absolute_deployment_path") as absolute:
            code, output, _ = self.invoke(self.run_args(consent=False, runtime=False))
        result = json.loads(output)
        self.assertEqual(code, 2)
        self.assertIn("--allow-execution", result["error"])
        self.assertIn("WHOLE module", result["error"])
        self.assertIsNone(result["run_root"])
        for operation in (absolute, self.saved, self.prepare, self.parent, self.task_id, self.run_call, self.setup):
            operation.assert_not_called()

    def test_explicit_runtime_runs_without_llm_configuration_or_cwd_inference(self) -> None:
        self.saved.side_effect = AssertionError("explicit runtime must not discover assets")
        code, output, errors = self.invoke(self.run_args())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), self.report)
        self.prepare.assert_called_once_with(self.source, "entry", self.inputs)
        self.parent.assert_called_once_with(self.runtime.parent, source_root=self.source.parent.resolve())
        self.task_id.assert_called_once_with("experiment")
        self.run_call.assert_called_once_with(
            self.runtime, self.source, "entry", self.inputs,
            self.runs / "owned-experiment", allow_execution=True,
        )
        self.saved.assert_not_called()
        self.setup.assert_not_called()
        self.assertIn("no GPU", errors)
        self.assertFalse(self.runs.exists())  # The controller is mocked; CLI did not create evidence itself.

    def test_default_runtime_uses_saved_assets_or_explicit_environment_never_cwd(self) -> None:
        assets = self.base / "saved-assets"
        args = ["experiment", "setup", "--archives", str(self.archives), "--json"]
        self.saved.return_value = {"assets": str(assets), "state": str(self.base / "saved-state")}
        code, _, _ = self.invoke(args)
        self.assertEqual(code, 0)
        self.setup.assert_called_once_with(
            assets / f"experiment-{experiments.RUNTIME_NAME}-linux", self.archives,
        )
        self.saved.assert_called_once_with()
        self.saved.reset_mock()
        self.setup.reset_mock()
        self.saved.side_effect = AssertionError("explicit assets must precede saved assets")
        override = self.base / "explicit-assets"
        with patch.dict(os.environ, {"FORGE8_HOME": str(override)}):
            self.assertEqual(self.invoke(args)[0], 0)
        self.setup.assert_called_once_with(
            override / f"experiment-{experiments.RUNTIME_NAME}-linux", self.archives,
        )
        self.saved.assert_not_called()
        self.saved.side_effect = None
        self.saved.return_value = None
        self.setup.reset_mock()
        code, output, _ = self.invoke(args)
        self.assertEqual(code, 2)
        self.assertIn("absolute --runtime", json.loads(output)["error"])
        self.setup.assert_not_called()

    def test_setup_delegates_only_offline_archives_without_source_or_run_work(self) -> None:
        code, output, errors = self.invoke([
            "experiment", "setup", "--archives", str(self.archives),
            "--runtime", str(self.runtime), "--json",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), self.setup_result)
        self.setup.assert_called_once_with(self.runtime, self.archives)
        self.assertIn("no network or GPU", errors)
        for operation in (self.saved, self.prepare, self.parent, self.task_id, self.run_call):
            operation.assert_not_called()

    def test_invalid_input_preflight_fails_before_evidence_allocation_or_worker(self) -> None:
        self.prepare.side_effect = ValueError("invalid args/kwargs")
        code, output, _ = self.invoke(self.run_args())
        result = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "invalid args/kwargs")
        self.assertIsNone(result["run_root"])
        for operation in (self.parent, self.task_id, self.run_call, self.setup):
            operation.assert_not_called()

    def test_human_guest_output_is_inert_and_reported_result_is_not_duplicated(self) -> None:
        self.report["reported_result"] = {"return": {"label": "value\u202e"}}
        self.report["execution"]["guest_output"] = {
            "stdout": 'log\x1b[2J\x07\x00\t\u202e\nFORGE8_GUEST_RESULT={"return":{"label":"value"}}',
            "stderr": "warning\x1b]0;owned\x07",
        }
        code, output, _ = self.invoke(self.run_args(as_json=False))
        self.assertEqual(code, 0)
        for character in ("\x1b", "\x07", "\x00", "\t", "\u202e"):
            self.assertNotIn(character, output)
        for escaped in ("\\u001b", "\\u0007", "\\u0000", "\\u0009", "\\u202e"):
            self.assertIn(escaped, output)
        self.assertIn("Guest-reported result", output)
        self.assertIn("worker 0.25s; including verification 0.50s", output)
        self.assertIn("not a verified explanation", output)
        self.assertIn("Guest stderr (untrusted", output)
        self.assertNotIn("FORGE8_GUEST_RESULT=", output)
        self.assertIn(str(self.runs / "owned-experiment" / "experiment.json"), output)

    def test_exit_codes_separate_completed_exceptions_from_host_failure_and_drift(self) -> None:
        cases = [
            ("return", {}, {}, 0),
            ("completed exception", {"reported_result": {
                "exception": "KeyError", "message": "missing", "phase": "call",
            }}, {}, 0),
            ("zero guest exit", {}, {"host_status": "guest_exit", "detail": 0}, 0),
            ("nonzero guest exit", {}, {"host_status": "guest_exit", "detail": 7}, 2),
            ("trap", {}, {"host_status": "trap", "detail": "INTERRUPT"}, 2),
            ("output limit", {}, {"output_limit": True}, 2),
            ("source drift", {"source_unchanged": False}, {}, 2),
            ("runtime drift", {"runtime_unchanged": False}, {}, 2),
            ("no completed output", {"execution": None, "process_status": "timed_out"}, {}, 2),
        ]
        for name, changes, execution_changes, expected in cases:
            with self.subTest(name=name):
                result = deepcopy(self.report)
                result.update(changes)
                if result["execution"] is not None:
                    result["execution"].update(execution_changes)
                self.run_call.return_value = result
                code, output, _ = self.invoke(self.run_args())
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(output), result)


if __name__ == "__main__":
    unittest.main()
