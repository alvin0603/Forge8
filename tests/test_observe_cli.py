"""Only synthetic own unittest fixtures execute, with native child isolation."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge8 import cli
from forge8.checks import CheckRunner


class ObserveCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="forge8-observe-cli-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.assets = self.base / "assets"
        self.assets.mkdir()
        self.state = self.base / "state"
        self.source = self.project / "test_sample.py"
        self.source.write_text("import unittest\nclass Case(unittest.TestCase):\n    def test_case(self):\n        self.assertEqual(1 + 1, 2)\n", encoding="utf-8")
        self.selector = "test_sample.Case.test_case"
        for manager in (patch.object(cli, "_resolve_fix_asset_root", return_value=self.assets),
                patch.object(cli, "_load_deployment", return_value=None),
                patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.state)})):
            manager.start()
            self.addCleanup(manager.stop)
        self.gpu = patch.object(cli, "prepare_server", side_effect=AssertionError("observe cannot start a model"))
        self.gpu.start()
        self.addCleanup(self.gpu.stop)

    def invoke(self, *, trust=True, selector=None, extra=()):
        argv = ["observe", str(self.project), "--test", selector or self.selector,
            "--source", "test_sample.py", "--json", *extra]
        if trust:
            argv.append("--trust-project-execution")
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(argv)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        return code, json.loads(output.getvalue()), errors.getvalue()

    def fake_result(self, **changes):
        values = {"status": "passed", "return_code": 0, "timed_out": False,
            "output_truncated": False, "ok": True, "capture_errors": (),
            "as_dict": lambda: {"status": "passed", "private_stdout": "PRIVATE_LOG"}}
        values.update(changes)
        return SimpleNamespace(**values)

    def fake_capture(self, definition, *, change=None, raw=None):
        self.assertEqual(definition.argv[:6], (os.path.abspath(sys.executable), "-I", "-B", "-X", "utf8", "-c"))
        self.assertEqual(definition.timeout_seconds, 60)
        self.assertEqual(definition.id, "trusted_observation")
        request = json.loads(Path(definition.argv[-1]).read_text(encoding="utf-8"))
        self.assertEqual(set(request), {"root", "test", "sources", "output"})
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        value = {"schema_version": 1, "kind": "forge8.observation", "test": request["test"],
            "source_before": {"test_sample.py": digest}, "source_after": {"test_sample.py": digest},
            "complete": True, "test_passed": True, "tests_run": 1, "counts": {"passed": 1},
            "events": [], "error": None}
        if change:
            value.update(change)
        Path(request["output"]).write_bytes(raw if raw is not None else json.dumps(value).encode())
        return self.fake_result()

    def test_explicit_trust_precedes_project_validation_import_and_run_writes(self):
        with patch("forge8.observation._source_pins") as pins, patch.object(CheckRunner, "_run_definition") as run:
            code, result, _ = self.invoke(trust=False)
        self.assertEqual(code, 2)
        self.assertIn("--trust-project-execution", result["error"])
        self.assertIsNone(result["run_root"])
        self.assertFalse(self.state.exists())
        pins.assert_not_called()
        run.assert_not_called()

    def test_real_native_child_success_is_private_and_separates_process_trace_test(self):
        original = self.source.read_bytes()
        self.source.write_text(original.decode() + "        print('PRIVATE_TEST_STDOUT')\n", encoding="utf-8")
        code, summary, errors = self.invoke()
        self.assertEqual(code, 0, summary)
        self.assertTrue(summary["complete"])
        self.assertTrue(summary["test_passed"])
        self.assertEqual(summary["capture_process"]["status"], "passed")
        self.assertEqual(summary["capture_process"]["return_code"], 0)
        self.assertIn("process_only", errors)
        self.assertNotIn("PRIVATE_TEST_STDOUT", errors + json.dumps(summary))
        run = Path(summary["run_root"])
        self.assertTrue(run.is_relative_to(self.state))
        self.assertTrue((run / "input.json").is_file())
        self.assertTrue((run / "check-result.json").is_file())
        final = json.loads(Path(summary["observation"]).read_text())
        self.assertEqual(final["source_before"], final["source_after"])
        self.assertTrue(final["events"])
        self.assertNotIn("PRIVATE_TEST_STDOUT", json.dumps(final))
        child = json.loads((run / "capture.json").read_text())
        self.assertNotIn("capture_process", child)
        self.assertEqual([p.name for p in self.project.iterdir()], ["test_sample.py"])

    def test_invalid_or_unbounded_selector_is_rejected_before_run_writes(self):
        for selector in ("bad", "module.Case.test_case;exec()", "a" * 513 + ".Case.test_case"):
            with self.subTest(selector_size=len(selector)), patch.object(CheckRunner, "_run_definition") as run:
                code, summary, _ = self.invoke(selector=selector)
            self.assertEqual(code, 2)
            self.assertIsNone(summary["run_root"])
            self.assertFalse(self.state.exists())
            run.assert_not_called()

    def test_active_interpreter_path_is_preserved_without_resolving_venv_symlink(self):
        interpreter = self.base / "native-venv-python"
        try:
            interpreter.symlink_to(Path(sys.executable).resolve())
        except OSError:
            self.skipTest("executable symlinks unavailable")
        with patch.object(sys, "executable", str(interpreter)), patch.object(CheckRunner, "_run_definition", side_effect=self.fake_capture) as runner:
            code, summary, _ = self.invoke()
        self.assertEqual(code, 0, summary)
        self.assertEqual(runner.call_args.args[0].argv[0], str(interpreter))
        self.assertNotEqual(runner.call_args.args[0].argv[0], str(interpreter.resolve()))

    def test_real_failed_unittest_can_have_complete_trace_without_test_pass(self):
        self.source.write_text("import unittest\nclass Case(unittest.TestCase):\n    def test_case(self):\n        self.fail('PRIVATE_ASSERTION_PAYLOAD')\n", encoding="utf-8")
        code, summary, errors = self.invoke()
        self.assertEqual(code, 0, summary)
        self.assertTrue(summary["complete"])
        self.assertFalse(summary["test_passed"])
        self.assertEqual(summary["counts"]["failed"], 1)
        self.assertEqual(summary["capture_process"]["return_code"], 0)
        self.assertNotIn("PRIVATE_ASSERTION_PAYLOAD", json.dumps(summary) + errors)

    def test_real_atexit_mutation_is_checked_after_child_exit(self):
        self.source.write_text("import unittest, atexit\nfrom pathlib import Path\n"
            "def change_source():\n    p = Path(__file__)\n    p.write_text(p.read_text() + '\\n# changed at exit\\n')\n"
            "atexit.register(change_source)\nclass Case(unittest.TestCase):\n    def test_case(self):\n        pass\n", encoding="utf-8")
        code, summary, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["capture_process"]["return_code"], 0)
        child = json.loads((Path(summary["run_root"]) / "capture.json").read_text())
        final = json.loads(Path(summary["observation"]).read_text())
        self.assertTrue(child["complete"])
        self.assertFalse(final["source_unchanged"])
        self.assertNotEqual(final["source_before"], final["source_after"])

    def test_timeout_and_missing_report_never_create_complete_observation(self):
        for result in [self.fake_result(status="timed_out", timed_out=True, return_code=None, ok=False), self.fake_result()]:
            with self.subTest(status=result.status), patch.object(CheckRunner, "_run_definition", return_value=result):
                code, summary, _ = self.invoke()
            self.assertEqual(code, 2)
            self.assertFalse(summary["complete"])
            final = json.loads(Path(summary["observation"]).read_text())
            self.assertFalse(final["complete"])
            self.assertEqual(final["events"], [])
            self.assertEqual(final["capture_process"]["status"], result.status)

    def test_report_kind_schema_and_size_are_checked(self):
        for change, raw in [({"kind": "not-observation"}, None), ({"schema_version": True}, None),
                ({"schema_version": 2}, None), ({"test": "different.Case.test_case"}, None),
                (None, b'{"kind":"forge8.observation","kind":"forge8.observation"}'),
                (None, b'{"value":NaN}'), (None, b'{"value":Infinity}'),
                (None, b" " * (128 * 1024 + 1)), (None, b"[" * 2000 + b"]" * 2000)]:
            with self.subTest(change=change, raw_size=len(raw) if raw else None), patch.object(CheckRunner, "_run_definition",
                    side_effect=lambda definition: self.fake_capture(definition, change=change, raw=raw)):
                code, summary, _ = self.invoke()
            self.assertEqual(code, 2)
            self.assertFalse(summary["complete"])

    def test_hardlinked_child_report_is_rejected_without_replacing_it(self):
        target = self.base / "unrelated.json"
        target.write_bytes(b"{}")
        def capture(definition):
            request = json.loads(Path(definition.argv[-1]).read_text())
            try:
                Path(request["output"]).hardlink_to(target)
            except OSError:
                self.skipTest("hard links unavailable")
            return self.fake_result()
        with patch.object(CheckRunner, "_run_definition", side_effect=capture):
            code, summary, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertFalse(summary["complete"])
        self.assertEqual(target.read_bytes(), b"{}")
        self.assertIn("links", summary["error"])

    def test_capture_errors_block_completeness_but_private_output_truncation_does_not(self):
        for errors, complete in [((), True), (("capture error",), False)]:
            def capture(definition):
                self.fake_capture(definition)
                return self.fake_result(capture_errors=errors, output_truncated=True)
            with self.subTest(errors=errors), patch.object(CheckRunner, "_run_definition", side_effect=capture):
                code, summary, _ = self.invoke()
            self.assertEqual(code, 0 if complete else 2)
            self.assertEqual(summary["complete"], complete)
            self.assertTrue(summary["capture_process"]["output_truncated"])

    def test_interrupted_runner_is_reported_incomplete_and_exits_130(self):
        with patch.object(CheckRunner, "_run_definition", side_effect=KeyboardInterrupt):
            code, summary, _ = self.invoke()
        self.assertEqual(code, 130)
        self.assertEqual(summary["status"], "interrupted")
        self.assertEqual(summary["capture_process"]["status"], "interrupted")
        self.assertFalse(json.loads(Path(summary["observation"]).read_text())["complete"])

    def test_state_inside_project_is_rejected_without_writes_or_child(self):
        state = self.project / "private-state"
        with patch.dict(os.environ, {"FORGE8_STATE_HOME": str(state)}), patch.object(CheckRunner, "_run_definition") as run:
            code, summary, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("overlap", summary["error"])
        self.assertFalse(state.exists())
        run.assert_not_called()

    def test_assets_overlap_and_excess_sources_are_rejected_before_run_writes(self):
        with patch.object(cli, "_resolve_fix_asset_root", return_value=self.project), patch.object(CheckRunner, "_run_definition") as run:
            code, summary, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("overlap", summary["error"])
        self.assertFalse(self.state.exists())
        run.assert_not_called()
        with patch.object(CheckRunner, "_run_definition") as run:
            code, summary, _ = self.invoke(extra=("--source", "test_sample.py") * 4)
        self.assertEqual(code, 2)
        self.assertFalse(self.state.exists())
        run.assert_not_called()

    def test_rejected_source_preflight_returns_bounded_error_without_child(self):
        for path in ("../outside.py", "missing.py"):
            with self.subTest(path=path), patch.object(CheckRunner, "_run_definition") as run:
                code, summary, _ = self.invoke(extra=("--source", path))
            self.assertEqual(code, 2)
            self.assertLess(len(summary["error"]), 1100)
            self.assertFalse(self.state.exists())
            run.assert_not_called()
    def test_read_observation_is_only_readonly_keyword_wiring(self):
        report = self.base / "observation.json"
        with patch("forge8.desk.run_desk", return_value=0) as desk, patch.object(CheckRunner, "_run_definition") as run:
            self.assertEqual(cli.main(["read", str(self.project), "--observation", str(report)]), 0)
            desk.assert_called_once_with(self.project, observation=report)
            desk.reset_mock()
            self.assertEqual(cli.main(["read", str(self.project)]), 0)
            desk.assert_called_once_with(self.project)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
