from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from forge8.runtime import load_manifest, verify_runtime


class RuntimeEmptyFileTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        archive = b"owned archive fixture"
        (self.root / "asset.zip").write_bytes(archive)
        self.installed = self.root / "install" / "package" / "__init__.py"
        self.installed.parent.mkdir(parents=True)
        self.installed.write_bytes(b"")
        self.manifest = {
            "schema_version": 2,
            "build": "owned-empty-file-fixture",
            "commit": "owned",
            "assets": [{
                "filename": "asset.zip",
                "sha256": hashlib.sha256(archive).hexdigest(),
            }],
            "install_dir": "install",
            "required_files": ["package/__init__.py"],
            "installed_files": [{
                "filename": "package/__init__.py",
                "size_bytes": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
            }],
        }
        self.manifest_path = self.root / "manifest.json"
        self.write_manifest()

    def write_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_empty_regular_file_passes_schema_and_full_verification(self) -> None:
        loaded = load_manifest(self.manifest_path)
        self.assertEqual(loaded["installed_files"][0]["size_bytes"], 0)
        self.assertEqual(
            loaded["installed_files"][0]["sha256"],
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        result = verify_runtime(self.manifest_path, self.root)
        self.assertTrue(result.ok)
        self.assertEqual(result.checked, ("asset.zip", "install/package/__init__.py"))
        self.assertEqual(result.missing, ())
        self.assertEqual(result.mismatched, ())

    def test_negative_boolean_and_noninteger_sizes_still_fail(self) -> None:
        for size in (-1, True, False, 0.0, "0", None, [], {}):
            with self.subTest(size=size):
                self.manifest["installed_files"][0]["size_bytes"] = size
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, "non-negative integer"):
                    load_manifest(self.manifest_path)

    def test_empty_file_still_requires_matching_sha256(self) -> None:
        self.manifest["installed_files"][0]["sha256"] = hashlib.sha256(b"x").hexdigest()
        self.write_manifest()
        result = verify_runtime(self.manifest_path, self.root)
        self.assertFalse(result.ok)
        self.assertEqual(result.missing, ())
        self.assertEqual(len(result.mismatched), 1)
        self.assertIn("install/package/__init__.py: expected", result.mismatched[0])

    def test_changing_empty_file_to_nonempty_fails(self) -> None:
        self.installed.write_bytes(b"x")
        result = verify_runtime(self.manifest_path, self.root)
        self.assertFalse(result.ok)
        self.assertEqual(
            result.mismatched,
            ("install/package/__init__.py: expected 0 bytes, got 1",),
        )

    def test_missing_empty_file_still_fails(self) -> None:
        self.installed.unlink()
        result = verify_runtime(self.manifest_path, self.root)
        self.assertFalse(result.ok)
        self.assertEqual(result.missing, ("install/package/__init__.py",))

    def test_unexpected_empty_file_still_fails(self) -> None:
        self.installed.with_name("extra.py").write_bytes(b"")
        result = verify_runtime(self.manifest_path, self.root)
        self.assertFalse(result.ok)
        self.assertEqual(
            result.mismatched,
            ("unexpected installed file: install/package/extra.py",),
        )

    def test_directory_cannot_replace_empty_regular_file(self) -> None:
        self.installed.unlink()
        self.installed.mkdir()
        result = verify_runtime(self.manifest_path, self.root)
        self.assertFalse(result.ok)
        self.assertEqual(result.missing, ("install/package/__init__.py",))


if __name__ == "__main__":
    unittest.main()
