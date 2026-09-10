from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from forge8.capsule import (
    CapsuleError,
    load_capsule,
    prepare_capsule,
    verify_candidate,
)


class CapsuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-capsule-test-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "capsule"
        (self.root / "workspace").mkdir(parents=True)
        (self.root / "verifier").mkdir()
        (self.root / "workspace" / "app.py").write_bytes(b"STATE = 'broken'\n")
        (self.root / "prompt.md").write_text("Repair app.py.\n", encoding="utf-8")
        (self.root / "verifier" / "verify.py").write_text(
            "from pathlib import Path\n"
            "import json\n"
            "passed = \"fixed\" in Path('workspace/app.py').read_text()\n"
            "print(json.dumps({'passed': passed, 'tests_run': 1}))\n"
            "raise SystemExit(0 if passed else 1)\n",
            encoding="utf-8",
        )
        self.payload = {
            "schema_version": "0.1",
            "id": "test.repair-app",
            "title": "Repair app",
            "domain": "repository",
            "task_type": "bug_fix",
            "difficulty": "small",
            "prompt": "prompt.md",
            "workspace": "workspace",
            "policy": {
                "network": "deny",
                "workspace_access": "read_write",
                "allowed_write_roots": ["workspace/app.py"],
                "stdlib_only": True,
            },
            "budget": {
                "wall_time_seconds": 10,
                "max_processes": 2,
                "max_output_bytes": 10000,
            },
            "verifier": {
                "command": [sys.executable, "verifier/verify.py"],
                "success_exit_code": 0,
                "network_required": False,
            },
            "required_artifacts": ["workspace/app.py"],
            "tags": ["python", "test"],
        }
        self.write_manifest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self) -> None:
        (self.root / "capsule.json").write_text(
            json.dumps(self.payload), encoding="utf-8"
        )

    def candidate(self) -> Path:
        destination = self.base / f"candidate-{len(list(self.base.glob('candidate-*')))}"
        shutil.copytree(self.root / "workspace", destination)
        return destination

    def test_load_and_prepare_exposes_prompt_but_not_verifier(self) -> None:
        manifest = load_capsule(self.root)
        prepared = prepare_capsule(manifest, self.base / "prepared")

        self.assertEqual(manifest.capsule_id, "test.repair-app")
        self.assertEqual(Path(prepared.prompt).read_text(encoding="utf-8"), "Repair app.py.\n")
        self.assertTrue((Path(prepared.workspace) / "app.py").is_file())
        self.assertFalse((self.base / "prepared" / "verifier").exists())
        self.assertEqual(
            prepared.source_fingerprints,
            {"app.py": "3e0a173ad3a57b79ceca4123647f974cc81e1d1cffe08369fd8ca07157d9e926"},
        )

    def test_rejects_manifest_path_traversal(self) -> None:
        self.payload["prompt"] = "../prompt.md"
        self.write_manifest()
        with self.assertRaisesRegex(CapsuleError, "normalized relative path"):
            load_capsule(self.root)

    def test_packaged_operator_fixtures_load(self) -> None:
        repository = Path(__file__).resolve().parent / "fixtures" / "capsules"
        manifests = [load_capsule(path) for path in sorted(repository.iterdir())]
        self.assertEqual(
            [manifest.capsule_id for manifest in manifests],
            [
                "repository.jsonl-checkpoint-recovery",
                "repository.production-config-recovery",
            ],
        )

    def test_verifier_accepts_candidate_and_returns_structured_evidence(self) -> None:
        manifest = load_capsule(self.root)
        candidate = self.candidate()
        (candidate / "app.py").write_text("STATE = 'fixed'\n", encoding="utf-8")

        result = verify_candidate(manifest, candidate)

        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.return_code, 0)
        self.assertEqual(result.changes.modified, ("app.py",))
        self.assertEqual(result.verifier_summary, {"passed": True, "tests_run": 1})
        self.assertEqual(result.isolation, "process_only")
        self.assertFalse(result.network_isolation_enforced)

    def test_policy_violation_fails_before_verifier(self) -> None:
        manifest = load_capsule(self.root)
        candidate = self.candidate()
        (candidate / "outside.txt").write_text("not allowed\n", encoding="utf-8")

        result = verify_candidate(manifest, candidate)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "policy_failed")
        self.assertIsNone(result.return_code)
        self.assertEqual(
            result.policy_violations, ("write outside allowed roots: outside.txt",)
        )

    def test_timeout_is_evidence_not_an_exception(self) -> None:
        (self.root / "verifier" / "verify.py").write_text(
            "import time\ntime.sleep(5)\n", encoding="utf-8"
        )
        manifest = load_capsule(self.root)

        result = verify_candidate(manifest, self.candidate(), timeout_seconds=0.05)

        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.status, "timed_out")

    def test_launch_error_is_evidence_not_an_exception(self) -> None:
        self.payload["verifier"]["command"] = [
            str(self.base / "does-not-exist"),
            "verifier/verify.py",
        ]
        self.write_manifest()
        manifest = load_capsule(self.root)

        result = verify_candidate(manifest, self.candidate())

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "launch_error")
        self.assertIn("FileNotFoundError", result.launch_error or "")

if __name__ == "__main__":
    unittest.main()
