from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import call, patch

from forge8 import runtime
from forge8.runtime import load_manifest, sha256_file, verify_model, verify_runtime


def runtime_manifest(
    *,
    archive_name: str,
    archive_bytes: bytes,
    installed_name: str,
    installed_bytes: bytes,
) -> dict:
    return {
        "schema_version": 2,
        "build": 1,
        "commit": "abc",
        "assets": [
            {
                "filename": archive_name,
                "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            }
        ],
        "install_dir": "install",
        "required_files": [installed_name],
        "installed_files": [
            {
                "filename": installed_name,
                "size_bytes": len(installed_bytes),
                "sha256": hashlib.sha256(installed_bytes).hexdigest(),
            }
        ],
    }


class RuntimeIntegrityTests(unittest.TestCase):
    def test_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "sample"
            path.write_bytes(b"forge8")
            self.assertEqual(sha256_file(path), hashlib.sha256(b"forge8").hexdigest())

    def test_cancel_before_verification_does_not_open_any_file(self) -> None:
        missing = Path("not-opened")
        for operation, args in (
            (sha256_file, (missing,)),
            (verify_runtime, (missing, missing)),
            (verify_model, (missing, missing)),
        ):
            with self.subTest(operation=operation.__name__), patch.object(Path, "open") as opened:
                with self.assertRaisesRegex(KeyboardInterrupt, "verification cancelled"):
                    operation(*args, cancel_requested=lambda: True)
                opened.assert_not_called()

    def test_hash_cancellation_at_read_boundaries_closes_stream(self) -> None:
        for cancel_at_eof in (False, True):
            with self.subTest(cancel_at_eof=cancel_at_eof):
                cancelled = threading.Event()
                stream = io.BytesIO(b"forge8")
                original_read = stream.read

                def read(size: int) -> bytes:
                    chunk = original_read(size)
                    if not cancel_at_eof or not chunk:
                        cancelled.set()
                    return chunk

                with patch.object(Path, "open", return_value=stream), patch.object(
                    stream, "read", side_effect=read
                ) as reads:
                    with self.assertRaises(KeyboardInterrupt):
                        sha256_file(Path("sample"), 3, cancel_requested=cancelled.is_set)
                self.assertTrue(stream.closed)
                self.assertEqual(reads.call_count, 3 if cancel_at_eof else 1)

    def test_optional_cancellation_keeps_all_pins_and_legacy_hash_calls(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, installed, model = root / "asset.zip", root / "install/server.exe", root / "model.gguf"
            installed.parent.mkdir()
            for path in (archive, installed, model):
                path.write_bytes(b"forge8")
            runtime_path, model_path = root / "runtime.json", root / "model.json"
            runtime_path.write_text(json.dumps(runtime_manifest(
                archive_name=archive.name, archive_bytes=b"forge8",
                installed_name=installed.name, installed_bytes=b"forge8",
            )), encoding="utf-8")
            model_path.write_text(json.dumps({"schema_version": 1, "files": [{
                "filename": model.name, "size_bytes": 6,
                "sha256": hashlib.sha256(b"forge8").hexdigest(),
            }]}), encoding="utf-8")

            for verifier, manifest, paths in (
                (verify_runtime, runtime_path, [archive, installed]),
                (verify_model, model_path, [model]),
            ):
                with self.subTest(verifier=verifier.__name__):
                    with patch.object(runtime, "sha256_file", wraps=sha256_file) as hashes:
                        expected = verifier(manifest, root)
                    self.assertTrue(expected.ok)
                    self.assertEqual(hashes.call_args_list, [call(path) for path in paths])
                    never_cancel = lambda: False
                    with patch.object(runtime, "sha256_file", wraps=sha256_file) as hashes:
                        actual = verifier(manifest, root, cancel_requested=never_cancel)
                    self.assertEqual(actual, expected)
                    self.assertEqual(hashes.call_args_list, [
                        call(path, cancel_requested=never_cancel) for path in paths
                    ])

                    for target in paths:
                        with self.subTest(cancel_during=target.name):
                            cancelled = threading.Event()
                            opened = []

                            def interrupt_hash(path: Path, **kwargs: object) -> str:
                                if path != target:
                                    return sha256_file(path, **kwargs)
                                stream = path.open("rb")
                                opened.append(stream)
                                original_read = stream.read

                                def read(size: int) -> bytes:
                                    chunk = original_read(size)
                                    cancelled.set()
                                    return chunk

                                with patch.object(Path, "open", return_value=stream), patch.object(
                                    stream, "read", side_effect=read
                                ):
                                    return sha256_file(path, 3, **kwargs)

                            with patch.object(runtime, "sha256_file", side_effect=interrupt_hash) as hashes:
                                with self.assertRaises(KeyboardInterrupt):
                                    verifier(manifest, root, cancel_requested=cancelled.is_set)
                            self.assertEqual(hashes.call_count, paths.index(target) + 1)
                            self.assertTrue(opened[0].closed)

                    paths[-1].write_bytes(b"tamper")
                    self.assertFalse(verifier(manifest, root, cancel_requested=never_cancel).ok)
                    paths[-1].write_bytes(b"forge8")

    def test_inventory_and_skipped_model_paths_still_check_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "server.exe").write_bytes(b"server")
            cancelled = threading.Event()
            original_scandir = os.scandir

            def scandir(path: Path):
                cancelled.set()
                return original_scandir(path)

            with patch.object(runtime.os, "scandir", side_effect=scandir):
                with self.assertRaises(KeyboardInterrupt):
                    runtime._installed_inventory(root, cancel_requested=cancelled.is_set)

            manifest = root / "model.json"
            manifest.write_text(json.dumps({"schema_version": 1, "files": [{
                "filename": "absent.gguf", "size_bytes": 1, "sha256": "0" * 64,
            }]}), encoding="utf-8")
            cancelled.clear()

            def is_file() -> bool:
                cancelled.set()
                return False

            with patch.object(Path, "is_file", side_effect=is_file), patch.object(runtime, "sha256_file") as hashes:
                with self.assertRaises(KeyboardInterrupt):
                    verify_model(manifest, root, cancel_requested=cancelled.is_set)
                hashes.assert_not_called()

    def test_manifest_verification_detects_missing_and_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "asset.zip").write_bytes(b"wrong")
            (root / "install").mkdir()
            manifest = runtime_manifest(
                archive_name="asset.zip",
                archive_bytes=b"expected",
                installed_name="server.exe",
                installed_bytes=b"trusted server",
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = verify_runtime(manifest_path, root)
            self.assertFalse(result.ok)
            self.assertEqual(len(result.mismatched), 1)
            self.assertIn("install/server.exe", result.missing)

    def test_installed_file_hash_is_verified_even_when_archive_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_bytes = b"authentic release archive"
            trusted_executable = b"MZ trusted server"
            (root / "asset.zip").write_bytes(archive_bytes)
            install = root / "install"
            install.mkdir()
            executable = install / "llama-server.exe"
            executable.write_bytes(trusted_executable)
            manifest = runtime_manifest(
                archive_name="asset.zip",
                archive_bytes=archive_bytes,
                installed_name=executable.name,
                installed_bytes=trusted_executable,
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertTrue(verify_runtime(manifest_path, root).ok)

            # Preserve both the archive and file length: existence and size alone
            # must not admit a replaced production executable.
            executable.write_bytes(b"MZ hostile server")
            result = verify_runtime(manifest_path, root)

            self.assertFalse(result.ok)
            self.assertEqual(result.missing, ())
            self.assertEqual(len(result.mismatched), 1)
            self.assertIn("install/llama-server.exe", result.mismatched[0])
            self.assertIn(hashlib.sha256(trusted_executable).hexdigest(), result.mismatched[0])

    def test_installed_file_size_mismatch_fails_without_trusting_the_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_bytes = b"authentic release archive"
            trusted_executable = b"trusted"
            (root / "asset.zip").write_bytes(archive_bytes)
            install = root / "install"
            install.mkdir()
            executable = install / "server.exe"
            executable.write_bytes(trusted_executable)
            manifest = runtime_manifest(
                archive_name="asset.zip",
                archive_bytes=archive_bytes,
                installed_name=executable.name,
                installed_bytes=trusted_executable,
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            executable.write_bytes(trusted_executable + b"-changed")

            result = verify_runtime(manifest_path, root)

            self.assertFalse(result.ok)
            self.assertIn("expected 7 bytes", result.mismatched[0])

    def test_manifest_requires_an_exact_complete_installed_pin_set(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            base = runtime_manifest(
                archive_name="asset.zip",
                archive_bytes=b"archive",
                installed_name="server.exe",
                installed_bytes=b"server",
            )
            cases = {}

            missing_section = deepcopy(base)
            del missing_section["installed_files"]
            cases["missing installed_files"] = missing_section

            missing_hash = deepcopy(base)
            del missing_hash["installed_files"][0]["sha256"]
            cases["missing hash"] = missing_hash

            missing_pin = deepcopy(base)
            missing_pin["required_files"].append("helper.dll")
            cases["missing pin"] = missing_pin

            duplicate_pin = deepcopy(base)
            duplicate_pin["installed_files"].append(
                deepcopy(duplicate_pin["installed_files"][0])
            )
            cases["duplicate pin"] = duplicate_pin

            unknown_pin_field = deepcopy(base)
            unknown_pin_field["installed_files"][0]["url"] = "https://example.invalid"
            cases["unknown pin field"] = unknown_pin_field

            for name, manifest in cases.items():
                with self.subTest(name=name):
                    manifest_path = root / f"{name.replace(' ', '-')}.json"
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_manifest(manifest_path)

    def test_installed_inventory_rejects_extra_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_bytes = b"archive"
            server_bytes = b"server"
            (root / "asset.zip").write_bytes(archive_bytes)
            install = root / "install"
            install.mkdir()
            (install / "server.exe").write_bytes(server_bytes)
            manifest = runtime_manifest(
                archive_name="asset.zip",
                archive_bytes=archive_bytes,
                installed_name="server.exe",
                installed_bytes=server_bytes,
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            unexpected = install / "evil.dll"
            unexpected.write_bytes(b"hostile")
            extra_result = verify_runtime(manifest_path, root)
            self.assertFalse(extra_result.ok)
            self.assertIn("unexpected installed file: install/evil.dll", extra_result.mismatched)

            unexpected.unlink()
            manifest["installed_files"].append(
                {"filename": "helper.dll", "size_bytes": 1, "sha256": "0" * 64}
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            missing_result = verify_runtime(manifest_path, root)
            self.assertFalse(missing_result.ok)
            self.assertIn("install/helper.dll", missing_result.missing)

    def test_non_required_implementation_dll_is_still_content_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_bytes = b"archive"
            server_bytes = b"MZ launcher"
            implementation_bytes = b"trusted implementation"
            (root / "asset.zip").write_bytes(archive_bytes)
            install = root / "install"
            install.mkdir()
            (install / "llama-server.exe").write_bytes(server_bytes)
            implementation = install / "llama-server-impl.dll"
            implementation.write_bytes(implementation_bytes)
            manifest = runtime_manifest(
                archive_name="asset.zip",
                archive_bytes=archive_bytes,
                installed_name="llama-server.exe",
                installed_bytes=server_bytes,
            )
            manifest["installed_files"].append(
                {
                    "filename": implementation.name,
                    "size_bytes": len(implementation_bytes),
                    "sha256": hashlib.sha256(implementation_bytes).hexdigest(),
                }
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertTrue(verify_runtime(manifest_path, root).ok)

            implementation.write_bytes(b"X" * len(implementation_bytes))
            result = verify_runtime(manifest_path, root)
            self.assertFalse(result.ok)
            self.assertTrue(
                any("llama-server-impl.dll" in item for item in result.mismatched)
            )

    def test_installed_inventory_rejects_symlink_hardlink_and_special_file(self) -> None:
        def fixture() -> tuple[tempfile.TemporaryDirectory, Path, Path, Path, dict]:
            temporary = tempfile.TemporaryDirectory()
            root = Path(temporary.name)
            archive_bytes = b"archive"
            server_bytes = b"server"
            (root / "asset.zip").write_bytes(archive_bytes)
            install = root / "install"
            install.mkdir()
            server = install / "server.exe"
            server.write_bytes(server_bytes)
            manifest = runtime_manifest(
                archive_name="asset.zip",
                archive_bytes=archive_bytes,
                installed_name=server.name,
                installed_bytes=server_bytes,
            )
            manifest_path = root / "manifest.json"
            return temporary, root, install, manifest_path, manifest

        temporary, root, install, manifest_path, manifest = fixture()
        self.addCleanup(temporary.cleanup)
        hardlink = install / "helper.dll"
        try:
            os.link(install / "server.exe", hardlink)
        except OSError:
            self.skipTest("hard links unavailable")
        manifest["installed_files"].append(
            {
                "filename": hardlink.name,
                "size_bytes": hardlink.stat().st_size,
                "sha256": sha256_file(hardlink),
            }
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        hardlink_result = verify_runtime(manifest_path, root)
        self.assertFalse(hardlink_result.ok)
        self.assertTrue(any("hard-linked" in item for item in hardlink_result.mismatched))

        temporary, root, install, manifest_path, manifest = fixture()
        self.addCleanup(temporary.cleanup)
        symlink = install / "helper.dll"
        try:
            symlink.symlink_to(install / "server.exe")
        except OSError:
            self.skipTest("symbolic links unavailable")
        manifest["installed_files"].append(
            {
                "filename": symlink.name,
                "size_bytes": (install / "server.exe").stat().st_size,
                "sha256": sha256_file(install / "server.exe"),
            }
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        symlink_result = verify_runtime(manifest_path, root)
        self.assertFalse(symlink_result.ok)
        self.assertTrue(
            any("symlink or reparse" in item for item in symlink_result.mismatched)
        )

        if hasattr(os, "mkfifo"):
            temporary, root, install, manifest_path, manifest = fixture()
            self.addCleanup(temporary.cleanup)
            pipe = install / "helper.pipe"
            os.mkfifo(pipe)
            manifest["installed_files"].append(
                {"filename": pipe.name, "size_bytes": 1, "sha256": "0" * 64}
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            special_result = verify_runtime(manifest_path, root)
            self.assertFalse(special_result.ok)
            self.assertTrue(
                any("not a regular file" in item for item in special_result.mismatched)
            )

    def test_manifest_rejects_duplicate_keys_and_nonfinite_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"schema_version":2,"schema_version":2}', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                load_manifest(duplicate)

            nonfinite = root / "nonfinite.json"
            nonfinite.write_text('{"schema_version":2,"build":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-finite"):
                load_manifest(nonfinite)

    def test_manifest_rejects_invalid_hash_size_and_escape_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            base = runtime_manifest(
                archive_name="asset.zip",
                archive_bytes=b"archive",
                installed_name="server.exe",
                installed_bytes=b"server",
            )
            cases = []

            bad_hash = deepcopy(base)
            bad_hash["installed_files"][0]["sha256"] = "not-a-hash"
            cases.append(bad_hash)

            boolean_size = deepcopy(base)
            boolean_size["installed_files"][0]["size_bytes"] = True
            cases.append(boolean_size)

            escaped_required = deepcopy(base)
            escaped_required["required_files"] = ["../server.exe"]
            escaped_required["installed_files"][0]["filename"] = "../server.exe"
            cases.append(escaped_required)

            absolute_asset = deepcopy(base)
            absolute_asset["assets"][0]["filename"] = "C:/runtime.zip"
            cases.append(absolute_asset)

            for index, manifest in enumerate(cases):
                with self.subTest(index=index):
                    manifest_path = root / f"invalid-{index}.json"
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_manifest(manifest_path)

    def test_model_verification_checks_size_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data = b"model"
            (root / "model.gguf").write_bytes(data)
            manifest = {
                "schema_version": 1,
                "files": [
                    {
                        "filename": "model.gguf",
                        "size_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ],
            }
            manifest_path = root / "model.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertTrue(verify_model(manifest_path, root).ok)


if __name__ == "__main__":
    unittest.main()
