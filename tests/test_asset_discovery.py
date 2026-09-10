from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import forge8.cli as cli_module


class AssetDiscoveryTests(unittest.TestCase):
    system = "Windows"

    def setUp(self) -> None:
        self.addCleanup(patch.stopall)
        deployment = patch.object(cli_module, "_load_deployment", return_value=None)
        deployment.start()
        self.addCleanup(deployment.stop)
        patch.object(cli_module.platform, "system", return_value=self.system).start()
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-assets-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.checkout = self.root / "checkout"
        repository = Path(__file__).resolve().parents[1]
        runtime_manifest, profile = cli_module._native_asset_paths()
        self.configurations = (runtime_manifest, cli_module._FIX_MODEL_MANIFEST, profile)
        for relative in self.configurations:
            target = self.checkout / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((repository / relative).read_bytes())
        (self.checkout / "runtime").mkdir()
        (self.checkout / "models").mkdir()
        self.installed_module = (
            self.root / "elsewhere" / "venv" / "Lib" / "site-packages" / "forge8" / "cli.py"
        )
        self.installed_module.parent.mkdir(parents=True)
        self.installed_module.write_text("# installed module fixture", encoding="utf-8")
        patch.object(cli_module, "__file__", str(self.installed_module)).start()
        patch.object(Path, "cwd", return_value=self.checkout).start()
        patch.dict(os.environ, {}, clear=True).start()

    def test_installed_package_can_find_pinned_assets_in_current_checkout(self) -> None:
        self.assertEqual(cli_module._resolve_fix_asset_root(), self.checkout)

    def test_shipped_config_content_matches_the_discovery_pins(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        for relative, expected in cli_module._CWD_ASSET_CONFIG_SHA256.items():
            with self.subTest(path=relative.as_posix()):
                normalized = (repository / relative).read_bytes().replace(b"\r\n", b"\n")
                self.assertEqual(hashlib.sha256(normalized).hexdigest(), expected)

    def test_windows_checkout_line_endings_are_accepted(self) -> None:
        for relative in self.configurations:
            path = self.checkout / relative
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.assertEqual(cli_module._resolve_fix_asset_root(), self.checkout)

    def test_invalid_explicit_override_never_falls_back_to_valid_cwd(self) -> None:
        for configured in ("", " ", str(self.root / "missing")):
            with self.subTest(configured=configured), patch.dict(
                os.environ, {"FORGE8_HOME": configured}
            ), self.assertRaisesRegex(ValueError, "FORGE8_HOME"):
                cli_module._resolve_fix_asset_root()

    def test_valid_explicit_override_takes_precedence_over_cwd(self) -> None:
        explicit = self.root / "explicit-assets"
        for relative in self.configurations:
            target = explicit / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.checkout / relative).read_bytes())
        (explicit / "runtime").mkdir()
        (explicit / "models").mkdir()
        with patch.dict(os.environ, {"FORGE8_HOME": str(explicit)}):
            self.assertEqual(cli_module._resolve_fix_asset_root(), explicit)

    def test_missing_marker_is_not_an_asset_checkout(self) -> None:
        (self.checkout / "runtime").rmdir()
        with self.assertRaisesRegex(ValueError, "cannot locate Forge8 assets"):
            cli_module._resolve_fix_asset_root()

    def test_current_directory_cannot_supply_modified_executable_pins(self) -> None:
        manifest = self.checkout / cli_module._native_asset_paths()[0]
        manifest.write_text('{"installed_files": []}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cannot locate Forge8 assets"):
            cli_module._resolve_fix_asset_root()

    def test_current_directory_ancestors_are_not_searched(self) -> None:
        nested = self.checkout / "unrelated-target"
        nested.mkdir()
        with patch.object(Path, "cwd", return_value=nested):
            with self.assertRaisesRegex(ValueError, "cannot locate Forge8 assets"):
                cli_module._resolve_fix_asset_root()

    def test_missing_platform_configuration_names_native_installation(self) -> None:
        (self.checkout / cli_module._native_asset_paths()[1]).unlink()
        with patch.dict(os.environ, {"FORGE8_HOME": str(self.checkout)}):
            with self.assertRaises(ValueError) as raised:
                cli_module._resolve_fix_asset_root()
        self.assertIn(f"native {self.system} requires", str(raised.exception))
        self.assertIn(cli_module._native_asset_paths()[0].as_posix(), str(raised.exception))
        self.assertIn("docs/PLATFORMS.md", str(raised.exception))

    def test_windows_assets_cannot_satisfy_native_linux_discovery(self) -> None:
        with patch.object(cli_module.platform, "system", return_value="Linux"):
            with self.assertRaisesRegex(ValueError, "native Linux requires"):
                cli_module._resolve_fix_asset_root()

    def test_linux_cwd_discovery_requires_its_own_exact_config_pins(self) -> None:
        # Use synthetic pins to test selection independently of a CUDA build.
        linux_pins = {}
        for relative in (cli_module._LINUX_RUNTIME_MANIFEST, cli_module._LINUX_PROFILE):
            target = self.checkout / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            content = b'{"fixture": "native-linux"}\n'
            target.write_bytes(content)
            linux_pins[relative] = hashlib.sha256(content).hexdigest()
        with patch.object(cli_module.platform, "system", return_value="Linux"):
            with self.assertRaisesRegex(ValueError, "cannot locate Forge8 assets"):
                cli_module._resolve_fix_asset_root()
            with patch.dict(cli_module._CWD_ASSET_CONFIG_SHA256, linux_pins):
                self.assertEqual(cli_module._resolve_fix_asset_root(), self.checkout)
                # Windows configuration is irrelevant to the native Linux client.
                (self.checkout / cli_module._FIX_RUNTIME_MANIFEST).unlink()
                (self.checkout / cli_module._FIX_PROFILE).unlink()
                self.assertEqual(cli_module._resolve_fix_asset_root(), self.checkout)
                (self.checkout / cli_module._LINUX_PROFILE).write_bytes(b'{}\n')
                with self.assertRaisesRegex(ValueError, "cannot locate Forge8 assets"):
                    cli_module._resolve_fix_asset_root()

    def add_reader_configurations(self, system: str, root: Path | None = None, *, reader: str = "qwen35") -> tuple[Path, ...]:
        root = self.checkout if root is None else root
        model, profile, linux_profile = (
            (cli_module._READ_MODEL_MANIFEST, cli_module._READ_PROFILE, cli_module._LINUX_READ_PROFILE)
            if reader == "qwen35" else
            (cli_module._GEMMA12_MODEL_MANIFEST, cli_module._GEMMA12_PROFILE, cli_module._LINUX_GEMMA12_PROFILE)
        )
        configurations = (
            cli_module._LINUX_RUNTIME_MANIFEST if system == "Linux" else cli_module._FIX_RUNTIME_MANIFEST,
            model,
            linux_profile if system == "Linux" else profile,
        )
        repository = Path(__file__).resolve().parents[1]
        for relative in configurations:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((repository / relative).read_bytes())
        (root / "runtime").mkdir(exist_ok=True)
        (root / "models").mkdir(exist_ok=True)
        return configurations

    def test_selected_reader_anchors_select_native_runtime_and_specific_configs(self) -> None:
        for system, reader in product(("Windows", "Linux"), ("qwen35", "gemma12b")):
            with self.subTest(system=system, reader=reader), patch.object(
                cli_module.platform, "system", return_value=system
            ):
                configurations = self.add_reader_configurations(system, reader=reader)
                self.assertEqual(cli_module._fix_asset_anchors(self.checkout, reader), (
                    *(self.checkout / relative for relative in configurations),
                    self.checkout / "runtime", self.checkout / "models",
                ))
                self.assertEqual(
                    cli_module._fix_asset_anchors(self.checkout)[1],
                    self.checkout / cli_module._FIX_MODEL_MANIFEST,
                )
                self.assertEqual(cli_module._resolve_fix_asset_root(reader), self.checkout)
        with self.assertRaisesRegex(ValueError, "unsupported reader"):
            cli_module._fix_asset_anchors(self.checkout, "unknown")

    def test_selected_reader_installed_cwd_discovery_does_not_require_other_models(self) -> None:
        for relative in (cli_module._FIX_MODEL_MANIFEST, cli_module._FIX_PROFILE):
            (self.checkout / relative).unlink()
        for system, reader in product(("Windows", "Linux"), ("qwen35", "gemma12b")):
            with self.subTest(system=system, reader=reader), patch.object(
                cli_module.platform, "system", return_value=system
            ):
                configurations = self.add_reader_configurations(system, reader=reader)
                self.assertEqual(cli_module._resolve_fix_asset_root(reader), self.checkout)
                for relative in configurations[1:]:
                    (self.checkout / relative).unlink()

    def test_modified_selected_configs_cannot_implicitly_discover_other_valid_models(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        for system, reader in product(("Windows", "Linux"), ("qwen35", "gemma12b")):
            with patch.object(cli_module.platform, "system", return_value=system):
                configurations = self.add_reader_configurations(system, reader=reader)
                other_reader = "gemma12b" if reader == "qwen35" else "qwen35"
                self.add_reader_configurations(system, reader=other_reader)
                gemma_profile = self.checkout / cli_module._native_asset_paths()[1]
                gemma_profile.write_bytes((repository / cli_module._native_asset_paths()[1]).read_bytes())
                for relative in configurations[1:]:
                    path = self.checkout / relative
                    original = path.read_bytes()
                    with self.subTest(system=system, reader=reader, modified=relative):
                        try:
                            path.write_bytes(b"{}\n")
                            self.assertEqual(cli_module._resolve_fix_asset_root(), self.checkout)
                            self.assertEqual(cli_module._resolve_fix_asset_root(other_reader), self.checkout)
                            with self.assertRaisesRegex(ValueError, "cannot locate Forge8 assets"):
                                cli_module._resolve_fix_asset_root(reader)
                        finally:
                            path.write_bytes(original)

    def test_selected_reader_explicit_home_is_authoritative_over_valid_cwd(self) -> None:
        explicit = self.root / "explicit-reader-assets"
        for system, reader in product(("Windows", "Linux"), ("qwen35", "gemma12b")):
            with self.subTest(system=system, reader=reader), patch.object(
                cli_module.platform, "system", return_value=system
            ):
                self.add_reader_configurations(system, reader=reader)
                configurations = self.add_reader_configurations(system, explicit, reader=reader)
                # Explicit operator choice permits custom configuration bytes;
                # later preparation, not discovery, validates their contents.
                (explicit / configurations[1]).write_bytes(b"{}\n")
                with patch.dict(os.environ, {"FORGE8_HOME": str(explicit)}):
                    self.assertEqual(cli_module._resolve_fix_asset_root(reader), explicit)
                for configured in ("", str(self.root / "missing")):
                    with self.subTest(configured=configured), patch.dict(
                        os.environ, {"FORGE8_HOME": configured}
                    ), self.assertRaisesRegex(ValueError, "FORGE8_HOME"):
                        cli_module._resolve_fix_asset_root(reader)
                (explicit / configurations[1]).unlink()
                with patch.dict(os.environ, {"FORGE8_HOME": str(explicit)}), self.assertRaisesRegex(
                    ValueError, "FORGE8_HOME"
                ):
                    cli_module._resolve_fix_asset_root(reader)


class EvidenceStateTests(unittest.TestCase):
    def setUp(self) -> None:
        deployment = patch.object(cli_module, "_load_deployment", return_value=None)
        deployment.start()
        self.addCleanup(deployment.stop)
        temporary = tempfile.TemporaryDirectory(prefix="forge8-state-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.assets = self.root / "assets"
        self.source = self.root / "source"
        self.assets.mkdir()
        self.source.mkdir()
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def runs(self) -> Path:
        return cli_module._fix_runs_parent(self.assets, source_root=self.source)

    def test_unset_state_keeps_existing_asset_storage(self) -> None:
        self.assertEqual(self.runs(), self.assets / ".forge8" / "runs")

    def test_explicit_state_creates_private_storage_without_touching_assets(self) -> None:
        state = self.root / "private" / "forge8"
        with patch.dict(os.environ, {"FORGE8_STATE_HOME": str(state)}):
            self.assertEqual(self.runs(), state / "runs")
            self.assertEqual(self.runs(), state / "runs")
        self.assertEqual(list(self.assets.iterdir()), [])
        if os.name != "nt":
            for directory in (state.parent, state, state / "runs"):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_invalid_explicit_state_never_falls_back(self) -> None:
        for configured in ("", " ", "relative", ".", str(self.root.anchor), str(self.root / ".." / "other")):
            with self.subTest(configured=configured), patch.dict(
                os.environ, {"FORGE8_STATE_HOME": configured}
            ), self.assertRaisesRegex(ValueError, "FORGE8_STATE_HOME"):
                self.runs()
        self.assertEqual(list(self.assets.iterdir()), [])

    def test_file_and_parent_file_reject_before_creating_storage(self) -> None:
        file = self.root / "file"
        file.write_bytes(b"original")
        for configured in (file, file / "child"):
            with self.subTest(configured=configured), patch.dict(
                os.environ, {"FORGE8_STATE_HOME": str(configured)}
            ), self.assertRaisesRegex(ValueError, "not a regular directory"):
                self.runs()
        self.assertEqual(file.read_bytes(), b"original")
        self.assertEqual(list(self.assets.iterdir()), [])

    def test_state_parent_and_runs_links_reject_without_writing_targets(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        for configured in (link, link / "nested"):
            with self.subTest(configured=configured), patch.dict(
                os.environ, {"FORGE8_STATE_HOME": str(configured)}
            ), self.assertRaisesRegex(ValueError, "not a regular directory"):
                self.runs()
        state = self.root / "state"
        state.mkdir()
        (state / "runs").symlink_to(outside, target_is_directory=True)
        with patch.dict(os.environ, {"FORGE8_STATE_HOME": str(state)}), self.assertRaisesRegex(
            ValueError, "not a regular directory"
        ):
            self.runs()
        self.assertEqual(list(outside.iterdir()), [])

    def test_reparse_state_is_rejected_even_when_metadata_says_directory(self) -> None:
        state = self.root / "state"
        state.mkdir()
        original_lstat = Path.lstat

        def metadata(path: Path, *args, **kwargs):
            if path == state:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_lstat(path, *args, **kwargs)

        with patch.dict(os.environ, {"FORGE8_STATE_HOME": str(state)}), patch.object(
            Path, "lstat", metadata
        ), self.assertRaisesRegex(ValueError, "not a regular directory"):
            self.runs()
        self.assertEqual(list(state.iterdir()), [])

    def test_both_workflows_reject_source_state_overlap_before_any_mkdir_or_inference(self) -> None:
        for command in ("fix", "explain"):
            for configured in (self.source, self.source / "new" / "state"):
                with self.subTest(command=command, configured=configured):
                    argv = [command, str(self.source), "--json"]
                    if command == "fix":
                        argv += ["--goal", "repair", "--allow-write", "app.py", "--check", "python_unittest"]
                    else:
                        argv += ["--question", "Explain the project"]
                    output = io.StringIO()
                    with (
                        patch.dict(os.environ, {"FORGE8_STATE_HOME": str(configured)}),
                        patch.object(cli_module, "_resolve_fix_asset_root", return_value=self.assets),
                        patch.object(Path, "mkdir") as mkdir,
                        patch.object(cli_module, "prepare_server") as server,
                        patch.object(cli_module, "LocalServerSupervisor") as supervisor,
                        redirect_stdout(output), redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(cli_module.main(argv), 2)
                    mkdir.assert_not_called()
                    server.assert_not_called()
                    supervisor.assert_not_called()
                    self.assertIn("must not overlap", json.loads(output.getvalue())["error"])


class ExplainExclusionDisplayTests(unittest.TestCase):
    def render(self, paths: list[str], *, ok: bool) -> str:
        result = {
            "ok": ok,
            "status": "answered" if ok else "insufficient_evidence",
            "error": None,
            "run_root": "private-run",
            "outcome": {
                "question": "Explain the project",
                "coverage": {"excluded": {"paths": paths, "entries": len(paths)}},
                "explanation_path": "private-run/explanation.json",
            },
        }
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            cli_module._emit_explain_human(result)
        return output.getvalue()

    def test_answered_and_incomplete_outputs_make_excluded_scope_visible(self) -> None:
        for ok in (True, False):
            with self.subTest(ok=ok):
                rendered = self.render([".env", ".venv", "image.png"], ok=ok)
                self.assertIn("Excluded from inspection: 3 entries", rendered)
                self.assertIn("not sent to model or source-verified", rendered)
                self.assertIn("directories include their descendants", rendered)
                for path in (".env", ".venv", "image.png"):
                    self.assertIn(f"  {path}\n", rendered)

    def test_excluded_paths_are_terminal_safe_and_limited_to_five(self) -> None:
        paths = ["\x1b[2J.env", "one", "two", "three", "four", "six-not-shown"]
        rendered = self.render(paths, ok=True)
        self.assertIn("Excluded from inspection: 6 entries", rendered)
        self.assertIn("\\u001b[2J.env", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("six-not-shown", rendered)
        self.assertIn("Full excluded list: private-run/explanation.json", rendered)

    def test_empty_excluded_scope_is_reported_without_extra_paths(self) -> None:
        rendered = self.render([], ok=True)
        self.assertIn("Excluded from inspection: 0 entries", rendered)
        self.assertNotIn("Full excluded list:", rendered)


class ExplainAnswerDisplayTests(unittest.TestCase):
    def test_interpretation_leads_quotes_without_mutating_evidence_order(self) -> None:
        citations = [{
            "path": "service.cpp", "start_line": 2, "end_line": 4, "evidence_id": "E1",
        }]
        claims = [
            {"type": "source_quote", "text": "\treturn first;", "citations": citations},
            {"type": "inference", "text": "First interpretation.", "citations": citations},
            {"type": "source_quote", "text": "return second;", "citations": citations},
            {"type": "inference", "text": "Second interpretation.", "citations": citations},
        ]
        original_claims = [dict(claim) for claim in claims]
        result = {
            "ok": True,
            "status": "answered",
            "question": "How does this service work?",
            "run_root": "run",
            "outcome": {"answer": {"claims": claims}},
        }
        output = io.StringIO()
        with redirect_stdout(output):
            cli_module._emit_explain_human(result)
        rendered = output.getvalue()
        expected_order = [
            "Question:",
            "Interpretation:",
            "1. Inference\n   First interpretation.",
            "2. Inference\n   Second interpretation.",
            "Supporting source lines:",
            "3. Source quote",
            "\\u0009return first;",
            "4. Source quote",
            "return second;",
            "Coverage:",
            "GPU/server:",
            "Artifacts:",
        ]
        positions = [rendered.index(fragment) for fragment in expected_order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(rendered.count("Based on: service.cpp:L2-L4 (E1)"), 2)
        self.assertEqual(rendered.count("Source: service.cpp:L2-L4 (E1)"), 2)
        self.assertIn("Citations prove provenance, not the truth of model inferences.", rendered)
        self.assertEqual(claims, original_claims)


if __name__ == "__main__":
    unittest.main()
