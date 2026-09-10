"""Native deployment setup; every environment/configuration path is temporary."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import forge8.cli as cli


class DeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="forge8-deployment-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.root / "linux-config"),
            "LOCALAPPDATA": str(self.root / "windows-config"),
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.path = cli._deployment_config_path()
        self.assets = self.make_assets("assets with spaces")
        self.state = self.root / "private-state"
        self.source = self.root / "source"
        self.source.mkdir()
        self.record = {"schema_version": 1, "assets": str(self.assets), "state": str(self.state)}

    def make_assets(self, name: str) -> Path:
        root = self.root / name
        for reader in ("gemma4", "qwen35"):
            for target in cli._fix_asset_anchors(root, reader)[:3]:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}", encoding="utf-8")
        (root / "runtime").mkdir()
        (root / "models").mkdir()
        return root

    def write_record(self, payload=None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.record if payload is None else payload), encoding="utf-8")

    def invoke(self, *arguments: str):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = cli.main(["configure", *arguments])
        return code, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

    def save(self, *extra: str, state: Path | None = None):
        return self.invoke("--assets", str(self.assets), "--state", str(state or self.state), *extra)

    def test_native_config_locations_and_absolute_environment_requirement(self) -> None:
        for system, variable, suffix in (("Linux", "XDG_CONFIG_HOME", "forge8"),
                                         ("Windows", "LOCALAPPDATA", "Forge8")):
            with self.subTest(system=system), patch.object(cli.platform, "system", return_value=system):
                with patch.object(Path, "home", side_effect=RuntimeError("home not available")):
                    self.assertEqual(cli._deployment_config_path(), Path(os.environ[variable]) / suffix / "deployment.json")
                with patch.dict(os.environ, {variable: "relative"}):
                    with self.assertRaisesRegex(ValueError, variable):
                        cli._deployment_config_path()
                with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=self.root):
                    base = self.root / (".config" if system == "Linux" else "AppData/Local")
                    self.assertEqual(cli._deployment_config_path(), base / suffix / "deployment.json")

    def test_show_is_read_only_and_distinguishes_saved_paths_from_environment(self) -> None:
        code, output, error = self.invoke()
        self.assertEqual((code, error, output["status"], output["saved"]), (0, "", "not_configured", None))
        self.assertFalse(self.path.parent.exists())
        self.write_record()
        before = self.path.read_bytes()
        with patch.dict(os.environ, {"FORGE8_HOME": "explicit-other-home"}):
            code, output, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(output["saved"], self.record)
        self.assertEqual(output["environment_overrides"]["FORGE8_HOME"], "explicit-other-home")
        self.assertIn("environment > saved record", output["note"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_validates_anchors_without_hashing_starting_gpu_or_creating_state(self) -> None:
        with patch.object(cli, "verify_runtime") as runtime, patch.object(cli, "verify_model") as model, \
                patch.object(cli, "prepare_server") as prepare, patch.object(cli, "LocalServerSupervisor") as server:
            code, output, error = self.save()
        self.assertEqual((code, error, output["status"]), (0, "", "saved"))
        self.assertEqual(cli._load_deployment(), self.record)
        for operation in (runtime, model, prepare, server):
            operation.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertEqual(self.path.stat().st_nlink, 1)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_identical_record_is_idempotent_without_a_publication(self) -> None:
        self.assertEqual(self.save()[0], 0)
        before = self.path.read_bytes()
        metadata = self.path.stat()
        with patch.object(cli.os, "replace") as replace, patch.object(cli.os, "link") as link:
            code, output, _ = self.save()
        self.assertEqual((code, output["status"]), (0, "unchanged"))
        replace.assert_not_called()
        link.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        after = self.path.stat()
        # Reading can update atime, including across a native Windows clock tick;
        # idempotence concerns content, identity and publication, not access time.
        for field in ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"):
            self.assertEqual(getattr(after, field), getattr(metadata, field), field)

    def test_changed_record_requires_replace_and_is_published_atomically(self) -> None:
        self.write_record()
        before = self.path.read_bytes()
        other_state = self.root / "other-state"
        code, _, error = self.save(state=other_state)
        self.assertEqual(code, 2)
        self.assertIn("--replace", error)
        self.assertEqual(self.path.read_bytes(), before)
        replace = os.replace

        def observe_publish(source, destination):
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(json.loads(Path(source).read_bytes())["state"], str(other_state))
            replace(source, destination)

        with patch.object(cli.os, "replace", side_effect=observe_publish) as publication:
            self.assertEqual(self.save("--replace", state=other_state)[0], 0)
        publication.assert_called_once()
        self.assertEqual(cli._load_deployment()["state"], str(other_state))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_replacement_preserves_old_record_and_cleans_temporary_file(self) -> None:
        self.write_record()
        before = self.path.read_bytes()
        with patch.object(cli.os, "replace", side_effect=OSError("publication failed")):
            code, _, error = self.save("--replace", state=self.root / "other-state")
        self.assertEqual(code, 2)
        self.assertIn("publication failed", error)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_first_setup_is_not_overwritten(self) -> None:
        competing = {**self.record, "state": str(self.root / "competing-state")}

        def occupied(_source, destination):
            Path(destination).write_text(json.dumps(competing), encoding="utf-8")
            raise FileExistsError("concurrent setup already exists")

        with patch.object(cli.os, "link", side_effect=occupied):
            self.assertEqual(self.save()[0], 2)
        self.assertEqual(cli._load_deployment(), competing)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_explicit_replace_can_recover_safe_corrupt_or_unsupported_records(self) -> None:
        for content in (b"{invalid", b"\xff", b'{"schema_version":2}',
                        b'{"schema_version":1,"schema_version":1}', b"[" * 2_000 + b"0" + b"]" * 2_000):
            with self.subTest(content=content):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(content)
                self.assertEqual(self.invoke()[0], 2)
                self.assertEqual(self.save()[0], 2)
                self.assertEqual(self.path.read_bytes(), content)
                self.assertEqual(self.save("--replace")[0], 0)
                self.assertEqual(cli._load_deployment(), self.record)

    def test_setup_requires_both_native_reader_anchors(self) -> None:
        for reader in ("gemma4", "qwen35"):
            path = cli._fix_asset_anchors(self.assets, reader)[2]
            with self.subTest(reader=reader):
                original = path.read_bytes()
                path.unlink()
                try:
                    code, _, error = self.save()
                    self.assertEqual(code, 2)
                    self.assertIn("both Gemma and Qwen native", error)
                    self.assertFalse(self.path.exists())
                finally:
                    path.write_bytes(original)

    def test_setup_rejects_partial_flags_and_unsafe_paths_before_writing(self) -> None:
        for arguments in (("--assets", str(self.assets)), ("--state", str(self.state)), ("--replace",),
                          ("--assets", "relative", "--state", str(self.state)),
                          ("--assets", str(self.assets), "--state", str(Path(self.root.anchor))),
                          ("--assets", str(self.assets), "--state", str(self.root / "a" / ".." / "b"))):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.invoke(*arguments)[0], 2)
                self.assertFalse(self.path.parent.exists())

    def test_record_schema_is_strict_including_bool_version_and_duplicate_keys(self) -> None:
        records = [[], {}, {**self.record, "schema_version": True}, {**self.record, "schema_version": 2},
                   {**self.record, "extra": 1}, {**self.record, "assets": 1},
                   {**self.record, "state": "relative"}, {**self.record, "state": str(Path(self.root.anchor))}]
        for record in records:
            with self.subTest(record=record):
                self.write_record(record)
                with self.assertRaises(ValueError):
                    cli._load_deployment()
        self.path.write_text(json.dumps(self.record)[:-1] + ',"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate keys"):
            cli._load_deployment()

    def test_oversize_and_nonregular_config_are_not_repaired_even_explicitly(self) -> None:
        self.write_record()
        self.path.write_bytes(b" " * (cli._DEPLOYMENT_MAX_BYTES + 1))
        self.assertEqual(self.save("--replace")[0], 2)
        self.assertEqual(self.path.stat().st_size, cli._DEPLOYMENT_MAX_BYTES + 1)
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(self.save("--replace")[0], 2)
        self.assertTrue(self.path.is_dir())

    def test_config_hardlink_is_rejected_without_mutating_either_link(self) -> None:
        self.write_record()
        alias = self.root / "alias.json"
        os.link(self.path, alias)
        before = alias.read_bytes()
        self.assertEqual(self.save("--replace")[0], 2)
        self.assertEqual(alias.read_bytes(), before)
        self.assertEqual(self.path.stat().st_nlink, 2)

    def test_config_symlink_and_parent_symlink_are_rejected(self) -> None:
        self.write_record()
        real = self.root / "real.json"
        self.path.rename(real)
        try:
            self.path.symlink_to(real)
        except OSError as exc:
            self.skipTest(f"native symlink creation unavailable: {exc}")
        self.assertEqual(self.save("--replace")[0], 2)
        self.path.unlink()
        self.path.parent.rmdir()
        target = self.root / "redirected"
        target.mkdir()
        self.path.parent.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.save()[0], 2)
        self.assertEqual(list(target.iterdir()), [])

    def test_saved_assets_override_checkout_and_bad_saved_assets_do_not_fall_back(self) -> None:
        self.write_record()
        for reader in ("gemma4", "qwen35"):
            self.assertEqual(cli._resolve_fix_asset_root(reader), self.assets)
        self.write_record({**self.record, "assets": str(self.root / "missing")})
        with self.assertRaisesRegex(ValueError, "saved deployment assets"):
            cli._resolve_fix_asset_root()

    def test_environment_overrides_each_field_independently(self) -> None:
        self.write_record()
        alternate = self.make_assets("explicit-assets")
        with patch.dict(os.environ, {"FORGE8_HOME": str(alternate)}):
            self.assertEqual(cli._resolve_fix_asset_root(), alternate)
            self.assertEqual(cli._fix_runs_parent(alternate, source_root=self.source), self.state / "runs")
        other_state = self.root / "explicit-state"
        with patch.dict(os.environ, {"FORGE8_STATE_HOME": str(other_state)}):
            self.assertEqual(cli._resolve_fix_asset_root(), self.assets)
            self.assertEqual(cli._fix_runs_parent(self.assets, source_root=self.source), other_state / "runs")

    def test_both_explicit_environment_fields_bypass_an_unused_corrupt_record(self) -> None:
        self.write_record({"broken": True})
        with patch.dict(os.environ, {"FORGE8_HOME": str(self.assets), "FORGE8_STATE_HOME": str(self.state)}), \
                patch.object(cli, "_load_deployment", side_effect=AssertionError("unused saved record")):
            self.assertEqual(cli._resolve_fix_asset_root(), self.assets)
            self.assertEqual(cli._fix_runs_parent(self.assets, source_root=self.source), self.state / "runs")

    def test_empty_explicit_environment_fields_fail_instead_of_using_saved_value(self) -> None:
        self.write_record()
        with patch.dict(os.environ, {"FORGE8_HOME": ""}):
            with self.assertRaisesRegex(ValueError, "FORGE8_HOME"):
                cli._resolve_fix_asset_root()
        with patch.dict(os.environ, {"FORGE8_STATE_HOME": ""}):
            with self.assertRaisesRegex(ValueError, "FORGE8_STATE_HOME"):
                cli._fix_runs_parent(self.assets, source_root=self.source)

    def test_saved_state_retains_per_job_source_overlap_rejection_before_creation(self) -> None:
        unsafe = self.source / "not-created"
        self.write_record({**self.record, "state": str(unsafe)})
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            cli._fix_runs_parent(self.assets, source_root=self.source)
        self.assertFalse(unsafe.exists())

    def test_absent_saved_record_preserves_module_discovery_and_default_state(self) -> None:
        fake_module = self.assets / "src/forge8/cli.py"
        fake_module.parent.mkdir(parents=True)
        fake_module.write_text("# trusted installed module fixture", encoding="utf-8")
        with patch.object(cli, "__file__", str(fake_module)):
            self.assertEqual(cli._resolve_fix_asset_root(), self.assets)
        self.assertEqual(cli._fix_runs_parent(self.assets, source_root=self.source), self.assets / ".forge8/runs")

    def test_verify_commands_use_saved_native_paths_and_keep_explicit_overrides(self) -> None:
        self.write_record()
        result = SimpleNamespace(ok=True, checked=[])
        for command, verifier, directory, manifest in (
                ("runtime", "verify_runtime", "runtime", cli._native_asset_paths()[0]),
                ("model", "verify_model", "models", cli._FIX_MODEL_MANIFEST)):
            with self.subTest(command=command), patch.object(cli, verifier, return_value=result) as verify, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main([command, "verify"]), 0)
                verify.assert_called_once_with(self.assets / manifest, self.assets / directory)
                verify.reset_mock()
                with patch.object(cli, "_load_deployment", side_effect=AssertionError("explicit verifier paths")):
                    self.assertEqual(cli.main([command, "verify", "--root", str(self.root),
                                               "--manifest", str(self.root / "custom.json")]), 0)
                verify.assert_called_once_with(self.root / "custom.json", self.root)


if __name__ == "__main__":
    unittest.main()
