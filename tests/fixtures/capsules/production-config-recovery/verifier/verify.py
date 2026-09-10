#!/usr/bin/env python3
"""Independent verifier for the Streamgate production overlay capsule."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CAPSULE_ROOT = Path(__file__).resolve().parents[1]
SUBMITTED_WORKSPACE = CAPSULE_ROOT / "workspace"
MUTABLE_PATH = "config/production.ini"

# Hashes of the original immutable fixture files prevent a candidate from
# bypassing the task by weakening loader validation.
EXPECTED_IMMUTABLE_SHA256 = {
    "README.md": "fa5b0b98ec88458827b487d636ace79afb6deab6f2b4715851cde2503ca53b63",
    "config/base.ini": "02e59239fd6d9116e3f36dc89d3b1fe2aafa48aaa8023c2a88cccd3eece671bc",
    "logs/production-startup.log": "f3015c455e127c190a4c05db97b7de53e74f691e898fb6cae214aad40c3ad73a",
    "streamgate/__init__.py": "14a1dd56dd01070463adbf6ecc6c858e9c74a02ad52342ce9854a68245f6259d",
    "streamgate/__main__.py": "cc7a19cbd7207e2a9f45bc0f20358b51e951d3d7a5e89327094e110a09bf3948",
    "streamgate/config.py": "4b459e87500af1cf4735e9620ca50d54a01bc597c81c2d03764e953ee736f727",
    "tests/test_config.py": "0e75bc2d1c11dc30bbdba61d858e0744f74eb3dc2cf65b2f71b65827de3f4d7f",
}


def file_sha256(path: Path) -> str:
    # The fixture is text-only. Normalize checkout line endings so the capsule
    # remains portable between native Windows, WSL, and Linux Git settings.
    content = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


class ProductionConfigContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = tempfile.TemporaryDirectory(prefix="forge8-config-")
        cls.workspace = Path(cls.sandbox.name) / "workspace"
        shutil.copytree(SUBMITTED_WORKSPACE, cls.workspace)
        sys.path.insert(0, str(cls.workspace))
        from streamgate import load_config

        cls.load_config = staticmethod(load_config)

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path.remove(str(cls.workspace))
        cls.sandbox.cleanup()

    def test_only_production_overlay_changed(self) -> None:
        actual_files = {
            path.relative_to(SUBMITTED_WORKSPACE).as_posix()
            for path in SUBMITTED_WORKSPACE.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        }
        expected_files = set(EXPECTED_IMMUTABLE_SHA256) | {MUTABLE_PATH}
        self.assertEqual(actual_files, expected_files)
        for relative, expected_hash in EXPECTED_IMMUTABLE_SHA256.items():
            self.assertEqual(
                file_sha256(SUBMITTED_WORKSPACE / relative),
                expected_hash,
                f"immutable fixture changed: {relative}",
            )

    def test_effective_settings_satisfy_operational_contract(self) -> None:
        settings = self.load_config(
            self.workspace / "config/base.ini",
            self.workspace / "config/production.ini",
            environ={"STREAMGATE_TOKEN": "capsule-secret-for-verifier"},
        )
        self.assertEqual(settings.runtime.environment, "production")
        self.assertEqual(settings.runtime.workers, 6)
        self.assertGreater(settings.runtime.shutdown_grace_seconds, 12)
        self.assertLessEqual(settings.runtime.shutdown_grace_seconds, 60)
        self.assertEqual(
            settings.delivery.url,
            "https://collector.internal.example/v2/events",
        )
        self.assertEqual(settings.delivery.request_timeout_seconds, 12)
        self.assertGreaterEqual(settings.delivery.max_inflight, 256)
        self.assertLessEqual(settings.delivery.max_inflight, 6 * 64)
        self.assertTrue(settings.security.verify_tls)
        self.assertEqual(settings.security.token, "capsule-secret-for-verifier")
        self.assertEqual(
            settings.storage.spool_dir.as_posix(),
            "/var/lib/streamgate/spool",
        )

    def test_overlay_references_environment_instead_of_containing_secret(self) -> None:
        raw = (self.workspace / MUTABLE_PATH).read_text(encoding="utf-8")
        self.assertIn("${STREAMGATE_TOKEN}", raw)
        self.assertNotIn("capsule-secret-for-verifier", raw)

    def test_real_cli_accepts_repaired_overlay(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.workspace)
        environment["STREAMGATE_TOKEN"] = "capsule-secret-for-verifier"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "streamgate",
                "--base",
                "config/base.ini",
                "--overlay",
                "config/production.ini",
                "--check",
            ],
            cwd=self.workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["configuration"], "valid")


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        ProductionConfigContractTests
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(
        json.dumps(
            {
                "capsule": "repository.production-config-recovery",
                "passed": result.wasSuccessful(),
                "tests_run": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
            },
            sort_keys=True,
        )
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
