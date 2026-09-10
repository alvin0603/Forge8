"""Model-free doctor contracts; no verifier, subprocess, socket or state writes."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge8 import cli, doctor
from forge8.runtime import IntegrityResult
from forge8.server import ServerPreparation


class DoctorTests(unittest.TestCase):
    def setUp(self):
        # Shared native selection can use Windows Python's initial OS-ver probe;
        # resolve it before forbidding every navigation-time child in these tests.
        platform.system()
        temporary = tempfile.TemporaryDirectory(prefix="forge8-doctor-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.assets, self.state = self.root / "assets with spaces", self.root / "state"
        self.environment = patch.dict(os.environ, {"FORGE8_HOME": str(self.assets),
            "FORGE8_STATE_HOME": str(self.state), "XDG_CONFIG_HOME": str(self.root / "config-linux"),
            "LOCALAPPDATA": str(self.root / "config-windows")}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.make_assets("qwen35")
        self.integrity = IntegrityResult(True, ("fixture",), (), ())
        plan = SimpleNamespace(as_dict=lambda: {"model_path": str(self.assets / "models/fixture.gguf")})
        self.ready = ServerPreparation("ready", (), plan, self.integrity, self.integrity)
        self.preparation = patch.object(doctor, "prepare_server", return_value=self.ready)
        self.prepare = self.preparation.start()
        self.addCleanup(self.preparation.stop)
        for target in ("subprocess.Popen", "socket.socket", "forge8.cli.LocalServerSupervisor",
                       "forge8.cli._fix_runs_parent", "forge8.cli.verify_runtime", "forge8.cli.verify_model"):
            guard = patch(target, side_effect=AssertionError("forbidden extra operation: " + target))
            guard.start()
            self.addCleanup(guard.stop)

    def make_assets(self, reader):
        for path in cli._fix_asset_anchors(self.assets, reader)[:3]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        for name in ("runtime", "models"):
            (self.assets / name).mkdir(exist_ok=True)

    def write_saved(self, value):
        path = cli._deployment_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return path

    def invoke(self, reader="qwen35", *, as_json=True):
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        directories = {str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_dir()}
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error), \
                patch.object(Path, "mkdir", side_effect=AssertionError("doctor wrote state")):
            code = doctor.run_doctor(SimpleNamespace(reader=reader, as_json=as_json))
        self.last_stderr = error.getvalue()
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertEqual({str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_dir()}, directories)
        return code, json.loads(output.getvalue()) if as_json else output.getvalue()

    def test_qwen_only_deployment_uses_exact_selected_paths_once(self):
        self.assertFalse((self.assets / cli._FIX_MODEL_MANIFEST).exists())
        code, report = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual((report["kind"], report["schema_version"], report["status"], report["ok"]),
                         ("forge8.doctor", 1, "passed", True))
        expected = cli._fix_asset_anchors(Path(), "qwen35")[:3]
        self.prepare.assert_called_once_with(self.assets, runtime_manifest_path=expected[0],
                                            model_manifest_path=expected[1], profile_path=expected[2])
        self.assertEqual(report["reader_paths"]["model_manifest"], str(self.assets / cli._READ_MODEL_MANIFEST))
        self.assertEqual(report["python"]["executable"], sys.executable)
        self.assertEqual(report["checks"]["configuration"]["status"], "not_checked")
        self.assertIn("inference readiness", report["notice"])
        self.assertEqual(self.last_stderr, "")
        self.assertEqual(report["not_checked"], ["gpu", "port", "actual_state_write", "snapshot_publication", "model_start", "inference"])

    def test_all_reader_choices_select_their_own_profile_and_manifest(self):
        for reader in ("gemma4", "gemma12b"):
            with self.subTest(reader=reader):
                self.make_assets(reader)
                self.prepare.reset_mock()
                code, report = self.invoke(reader)
                self.assertEqual(code, 0)
                expected = cli._fix_asset_anchors(Path(), reader)[:3]
                self.prepare.assert_called_once_with(self.assets, runtime_manifest_path=expected[0],
                    model_manifest_path=expected[1], profile_path=expected[2])
                self.assertEqual(report["reader_paths"]["profile"], str(self.assets / expected[2]))

    def test_missing_model_retains_full_integrity_and_native_repair_command(self):
        missing = IntegrityResult(False, (), ("qwen-missing.gguf",), ())
        self.prepare.return_value = ServerPreparation("integrity_failed", ("model integrity verification failed",),
            runtime_integrity=self.integrity, model_integrity=missing)
        code, report = self.invoke()
        self.assertEqual(code, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["preparation"]["model_integrity"]["missing"], ["qwen-missing.gguf"])
        details = "\n".join(report["checks"]["reader_preparation"]["details"])
        self.assertIn("qwen-missing.gguf", details)
        self.assertIn("model verify", details.replace("' '", " "))
        self.assertIn(str(self.assets / cli._READ_MODEL_MANIFEST), details)
        self.assertNotIn(str(self.assets / cli._FIX_MODEL_MANIFEST), details)
        self.prepare.assert_called_once()

    def test_bad_profile_and_wrong_native_fail_without_model_launch(self):
        for status, message in (("configuration_error", "supplied model manifest does not match the profile pin"),
                                ("unsupported_platform", "runtime platform is not native to Linux")):
            with self.subTest(status=status):
                self.prepare.return_value = ServerPreparation(status, (message,))
                self.prepare.reset_mock()
                code, report = self.invoke()
                self.assertEqual(code, 2)
                self.assertEqual(report["preparation"]["status"], status)
                self.assertIn(message, report["checks"]["reader_preparation"]["details"])
                self.prepare.assert_called_once()

    def test_unused_corrupt_record_is_not_read_when_both_fields_override(self):
        path = self.write_saved("{broken")
        with patch.object(cli, "_load_deployment", side_effect=AssertionError("unused record was read")):
            code, report = self.invoke()
        self.assertEqual(code, 0)
        self.assertFalse(report["configuration"]["saved_used"])
        self.assertEqual(report["configuration"]["path"], str(path))
        self.assertEqual(report["configuration"]["assets_origin"], "FORGE8_HOME")
        self.assertEqual(report["configuration"]["state_origin"], "FORGE8_STATE_HOME")

    def test_unused_invalid_config_location_does_not_block_two_overrides(self):
        with patch.object(cli, "_deployment_config_path", side_effect=ValueError("invalid configuration location")), \
                patch.object(cli, "_load_deployment", side_effect=AssertionError("unused record was read")):
            code, report = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(report["checks"]["configuration"]["status"], "not_checked")
        self.prepare.assert_called_once()

    def test_effective_paths_follow_saved_record_and_each_environment_override(self):
        self.write_saved({"schema_version": 1, "assets": str(self.assets), "state": str(self.state)})
        for remove in (("FORGE8_HOME",), ("FORGE8_STATE_HOME",), ("FORGE8_HOME", "FORGE8_STATE_HOME")):
            with self.subTest(remove=remove), patch.dict(os.environ):
                for name in remove:
                    os.environ.pop(name)
                self.prepare.reset_mock()
                code, report = self.invoke()
                self.assertEqual(code, 0)
                self.assertEqual(report["configuration"]["effective_assets"], str(self.assets))
                self.assertEqual(report["configuration"]["effective_state"], str(self.state))
                for field, variable in (("assets", "FORGE8_HOME"), ("state", "FORGE8_STATE_HOME")):
                    self.assertEqual(report["configuration"][field + "_origin"],
                                     "saved record" if variable in remove else variable)
                self.prepare.assert_called_once()

    def test_bad_required_record_does_not_hide_independent_overridden_assets(self):
        self.write_saved("{broken")
        with patch.dict(os.environ):
            os.environ.pop("FORGE8_STATE_HOME")
            code, report = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(report["checks"]["configuration"]["status"], "failed")
        self.assertEqual(report["checks"]["state"]["status"], "not_checked")
        self.assertEqual(report["checks"]["reader_preparation"]["status"], "passed")
        self.prepare.assert_called_once()

    def test_bad_assets_and_state_are_both_reported_without_preparation(self):
        with patch.dict(os.environ, {"FORGE8_HOME": str(self.root / "missing"), "FORGE8_STATE_HOME": "relative"}):
            code, report = self.invoke()
        self.assertEqual(code, 2)
        for name in ("assets", "state"):
            self.assertEqual(report["checks"][name]["status"], "failed")
            self.assertTrue(report["checks"][name]["next_steps"])
        self.assertEqual(report["checks"]["reader_preparation"]["status"], "not_checked")
        self.prepare.assert_not_called()

    def test_default_and_explicit_assets_contained_state_fail_only_resident_layout(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit), patch.dict(os.environ):
                if explicit:
                    os.environ["FORGE8_STATE_HOME"] = str(self.assets / ".forge8")
                else:
                    os.environ.pop("FORGE8_STATE_HOME")
                self.prepare.reset_mock()
                code, report = self.invoke()
                self.assertEqual(code, 2)
                self.assertEqual(report["configuration"]["effective_state"], str(self.assets / ".forge8"))
                self.assertEqual(report["checks"]["state"]["status"], "passed")
                self.assertEqual(report["checks"]["resident_layout"]["status"], "failed")
                self.assertIn("--idle-timeout 0", " ".join(report["checks"]["resident_layout"]["next_steps"]))
                self.assertEqual(report["checks"]["reader_preparation"]["status"], "passed")
                self.prepare.assert_called_once()

    def test_state_ancestor_is_not_misreported_as_definite_resident_refusal(self):
        with patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.root)}):
            code, report = self.invoke()
        self.assertEqual(code, 0)
        layout = report["checks"]["resident_layout"]
        self.assertEqual(layout["status"], "warning")
        self.assertIn("state/runs/read-<id>", " ".join(layout["details"]))
        self.assertNotIn("will refuse", " ".join(layout["details"]))
        self.assertEqual(report["checks"]["reader_preparation"]["status"], "passed")
        self.prepare.assert_called_once()

    def test_gemma_one_shot_does_not_fail_due_to_resident_only_overlap(self):
        self.make_assets("gemma4")
        with patch.dict(os.environ, {"FORGE8_STATE_HOME": str(self.assets / ".forge8")}):
            code, report = self.invoke("gemma4")
        self.assertEqual(code, 0)
        self.assertEqual(report["checks"]["resident_layout"]["status"], "not_applicable")

    @unittest.skipUnless(sys.platform.startswith("linux"), "WSL-style path warning is Linux-specific")
    def test_conventional_mounted_state_is_a_warning_not_filesystem_proof(self):
        with patch.object(cli, "_state_directory_plan", return_value=(Path("/mnt/d/private-state"), [], [])):
            code, report = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(report["checks"]["state"]["status"], "warning")
        self.assertIn("not filesystem identification", " ".join(report["checks"]["state"]["details"]))
        self.assertIn("snapshot_publication", report["not_checked"])

    def test_cancellation_reports_interrupted_never_partial_success(self):
        self.prepare.side_effect = KeyboardInterrupt()
        code, report = self.invoke()
        self.assertEqual((code, report["status"], report["ok"]), (130, "interrupted", False))
        self.assertEqual(report["checks"]["reader_preparation"]["status"], "interrupted")
        self.assertIsNone(report["preparation"])
        self.prepare.assert_called_once()

    def test_human_errors_are_inert_and_distinguish_hashes_from_readiness(self):
        self.prepare.return_value = ServerPreparation("configuration_error", ("profile \x1b[2J error\nsecond line",))
        code, output = self.invoke(as_json=False)
        self.assertEqual(code, 2)
        self.assertNotIn("\x1b", output)
        self.assertIn("\\u001b", output)
        self.assertIn("not inference readiness", output)
        self.assertIn("qwen35", output)
        self.assertIn(str(self.assets / cli._READ_MODEL_MANIFEST), output)
        self.assertIn("Checking complete runtime/model hashes", self.last_stderr)
        self.assertIn("Ctrl+C cancels", self.last_stderr)

    def test_oversized_report_fails_whole_without_a_partial_green_result(self):
        self.prepare.return_value = ServerPreparation("configuration_error", ("x" * 262144,))
        code, report = self.invoke()
        self.assertEqual((code, report["status"], report["ok"]), (2, "failed", False))
        self.assertNotIn("checks", report)
        self.assertIn("no partial success", report["error"])


if __name__ == "__main__":
    unittest.main()
