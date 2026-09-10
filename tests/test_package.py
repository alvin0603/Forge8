from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import forge8.package as package_module
from forge8.capsule import FileChanges, VerificationResult
from forge8.package import PackageError, build_workspace_patch, write_delivery_bundle
from forge8.trace import GENESIS_HASH, TraceSeal


class DeliveryPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-package-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.candidate = self.root / "candidate"
        self.source.mkdir()
        self.candidate.mkdir()
        (self.source / "same.txt").write_text("same\n", encoding="utf-8")
        (self.candidate / "same.txt").write_text("same\n", encoding="utf-8")
        (self.source / "changed.txt").write_text("before\nline\n", encoding="utf-8")
        (self.candidate / "changed.txt").write_text("after\nline\n", encoding="utf-8")
        (self.source / "removed.txt").write_text("removed\n", encoding="utf-8")
        (self.candidate / "added.txt").write_text("added\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def fingerprints(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def verification(
        self,
        ok: bool = True,
        *,
        changes: FileChanges | None = None,
    ) -> VerificationResult:
        fingerprints = self.fingerprints(self.candidate)
        return VerificationResult(
            capsule_id="test.delivery",
            status="passed" if ok else "failed",
            ok=ok,
            started_at="2026-08-31T00:00:00Z",
            ended_at="2026-08-31T00:00:01Z",
            duration_seconds=1.0,
            return_code=0 if ok else 1,
            timed_out=False,
            launch_error=None,
            changes=changes
            or FileChanges(
                added=("added.txt",),
                modified=("changed.txt",),
                removed=("removed.txt",),
            ),
            policy_violations=(),
            required_artifacts_missing=(),
            stdout='{"passed":true}\n',
            stderr="",
            output_truncated=False,
            verifier_summary={"passed": ok},
            candidate_fingerprints=fingerprints,
        )

    @staticmethod
    def seal() -> TraceSeal:
        return TraceSeal(
            schema_version=1,
            event_count=4,
            head_hash=GENESIS_HASH,
            trace_sha256=hashlib.sha256(b"trace").hexdigest(),
            sealed_at="2026-08-31T00:00:02Z",
        )

    def test_patch_and_change_manifest_are_deterministic(self) -> None:
        patch, changes = build_workspace_patch(self.source, self.candidate)
        second_patch, second_changes = build_workspace_patch(self.source, self.candidate)

        self.assertEqual(patch, second_patch)
        self.assertEqual(changes, second_changes)
        self.assertEqual(
            [(change.kind, change.path) for change in changes],
            [
                ("added", "added.txt"),
                ("modified", "changed.txt"),
                ("removed", "removed.txt"),
            ],
        )
        self.assertIn("--- /dev/null\n+++ b/added.txt\n", patch)
        self.assertIn("--- a/changed.txt\n+++ b/changed.txt\n", patch)
        self.assertIn("--- a/removed.txt\n+++ /dev/null\n", patch)
        self.assertNotIn("same.txt", patch)

    def test_no_newline_marker_and_binary_rejection(self) -> None:
        (self.source / "tail.txt").write_text("old", encoding="utf-8")
        (self.candidate / "tail.txt").write_text("new", encoding="utf-8")
        patch, _changes = build_workspace_patch(self.source, self.candidate)
        self.assertIn("\\ No newline at end of file", patch)

        (self.candidate / "tail.txt").write_bytes(b"\x00binary")
        with self.assertRaisesRegex(PackageError, "binary change"):
            build_workspace_patch(self.source, self.candidate)

    def test_patch_splits_only_lf_not_unicode_line_separators(self) -> None:
        (self.source / "unicode-lines.txt").write_text(
            "left\u2028tail\n", encoding="utf-8", newline=""
        )
        (self.candidate / "unicode-lines.txt").write_text(
            "right\u2028tail\n", encoding="utf-8", newline=""
        )

        patch, _changes = build_workspace_patch(self.source, self.candidate)

        self.assertIn("-left\u2028tail\n", patch)
        self.assertIn("+right\u2028tail\n", patch)
        self.assertNotIn("-left\n", patch)

    def test_patch_rejects_markdown_or_header_injection_path(self) -> None:
        (self.candidate / "bad`path.txt").write_text("unsafe\n", encoding="utf-8")
        with self.assertRaisesRegex(PackageError, "unsafe patch path"):
            build_workspace_patch(self.source, self.candidate)

    def test_bundle_binds_patch_verification_trace_and_handoff(self) -> None:
        destination = self.root / "delivery"
        result = write_delivery_bundle(
            self.source,
            self.candidate,
            destination,
            accepted=True,
            verification=self.verification(),
            source_fingerprints=self.fingerprints(self.source),
            trace_seal=self.seal(),
            run_metadata={
                "model": "gemma4-e4b",
                "actions": 3,
                "isolation": "process_only",
                "network_isolation_enforced": False,
            },
        )

        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        patch = (destination / "changes.patch").read_bytes()
        verification = json.loads(
            (destination / "verification.json").read_text(encoding="utf-8")
        )
        handoff = (destination / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertTrue(result.verified)
        self.assertEqual(manifest["patch"]["sha256"], hashlib.sha256(patch).hexdigest())
        self.assertEqual(manifest["run"]["model"], "gemma4-e4b")
        self.assertEqual(manifest["trace"]["event_count"], 4)
        self.assertTrue(verification["ok"])
        self.assertIn("VERIFIED", handoff)
        self.assertIn("Isolation: `process_only`", handoff)
        self.assertIn("Network isolation enforced: `no`", handoff)
        self.assertIn("not proof of every meaning", handoff)
        self.assertIn("use trusted code only", handoff)
        self.assertIn("exact Before and After byte states", handoff)
        self.assertIn("manifest.json: verified", handoff)
        self.assertIn("verification.json: ok", handoff)
        self.assertNotIn("`None`", handoff)
        for change in result.changes:
            before = "absent" if change.before is None else change.before.sha256
            after = "absent" if change.after is None else change.after.sha256
            self.assertIn(
                (
                    f"- `{change.path}` - {change.kind}\n"
                    f"  - Before SHA-256: `{before}`\n"
                    f"  - After SHA-256: `{after}`"
                ),
                handoff,
            )

    def test_failed_verification_is_packaged_but_not_promotable(self) -> None:
        destination = self.root / "failed-delivery"
        result = write_delivery_bundle(
            self.source,
            self.candidate,
            destination,
            accepted=False,
            verification=self.verification(ok=False),
            trace_seal=self.seal(),
            run_metadata={},
        )
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        handoff = (destination / "HANDOFF.md").read_text(encoding="utf-8")

        self.assertFalse(result.verified)
        self.assertFalse(manifest["verified"])
        self.assertIn("Do not apply", handoff)
        self.assertIn("diagnostic evidence only", handoff)
        self.assertNotIn("Before applying, require", handoff)

    def test_passing_verifier_cannot_override_a_rejected_run(self) -> None:
        destination = self.root / "rejected-delivery"
        result = write_delivery_bundle(
            self.source,
            self.candidate,
            destination,
            accepted=False,
            verification=self.verification(ok=True),
            trace_seal=self.seal(),
            run_metadata={"status": "backend_error"},
        )
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        handoff = (destination / "HANDOFF.md").read_text(encoding="utf-8")

        self.assertFalse(result.verified)
        self.assertFalse(manifest["verified"])
        self.assertIn("NOT VERIFIED", handoff)
        self.assertIn("final verifier passed", handoff)
        self.assertIn("Do not apply", handoff)

    def test_acceptance_requires_a_passing_verifier(self) -> None:
        with self.assertRaisesRegex(PackageError, "requires a passing verifier"):
            write_delivery_bundle(
                self.source,
                self.candidate,
                self.root / "inconsistent-delivery",
                accepted=True,
                verification=self.verification(ok=False),
                source_fingerprints=self.fingerprints(self.source),
                trace_seal=self.seal(),
                run_metadata={},
            )

    def test_accepted_delivery_requires_nonempty_verified_changes(self) -> None:
        candidate = self.root / "unchanged-candidate"
        candidate.mkdir()
        for source in self.source.iterdir():
            if source.is_file():
                (candidate / source.name).write_bytes(source.read_bytes())
        original_candidate = self.candidate
        self.candidate = candidate
        destination = self.root / "empty-delivery"
        try:
            verification = self.verification(
                changes=FileChanges(added=(), modified=(), removed=())
            )
            with self.assertRaisesRegex(PackageError, "non-empty change set"):
                write_delivery_bundle(
                    self.source,
                    candidate,
                    destination,
                    accepted=True,
                    verification=verification,
                    source_fingerprints=self.fingerprints(self.source),
                    trace_seal=self.seal(),
                    run_metadata={},
                )
        finally:
            self.candidate = original_candidate
        self.assertFalse(destination.exists())

    def test_accepted_delivery_rejects_verifier_change_set_mismatch(self) -> None:
        destination = self.root / "mismatched-changes-delivery"
        verification = self.verification(
            changes=FileChanges(
                added=("added.txt",),
                modified=(),
                removed=("removed.txt",),
            )
        )

        with self.assertRaisesRegex(PackageError, "changes do not match"):
            write_delivery_bundle(
                self.source,
                self.candidate,
                destination,
                accepted=True,
                verification=verification,
                source_fingerprints=self.fingerprints(self.source),
                trace_seal=self.seal(),
                run_metadata={},
            )
        self.assertFalse(destination.exists())

    def test_accepted_delivery_rejects_candidate_mutation_after_verification(self) -> None:
        cases = ("modify", "add", "remove")
        for operation in cases:
            with self.subTest(operation=operation):
                verification = self.verification()
                destination = self.root / f"late-{operation}-delivery"
                if operation == "modify":
                    target = self.candidate / "changed.txt"
                    original = target.read_bytes()
                    target.write_text("changed after verification\n", encoding="utf-8")
                elif operation == "add":
                    target = self.candidate / "late.txt"
                    original = None
                    target.write_text("late addition\n", encoding="utf-8")
                else:
                    target = self.candidate / "same.txt"
                    original = target.read_bytes()
                    target.unlink()
                try:
                    with self.assertRaisesRegex(
                        PackageError, "verifier (evidence|fingerprints)"
                    ):
                        write_delivery_bundle(
                            self.source,
                            self.candidate,
                            destination,
                            accepted=True,
                            verification=verification,
                            source_fingerprints=self.fingerprints(self.source),
                            trace_seal=self.seal(),
                            run_metadata={},
                        )
                finally:
                    if operation == "add":
                        target.unlink(missing_ok=True)
                    else:
                        assert original is not None
                        target.write_bytes(original)
                self.assertFalse(destination.exists())

    def test_accepted_delivery_rejects_source_mutation_after_ingress(self) -> None:
        verification = self.verification()
        ingress_fingerprints = self.fingerprints(self.source)
        target = self.source / "changed.txt"
        original = target.read_bytes()
        target.write_text("mutated source baseline\n", encoding="utf-8")
        destination = self.root / "late-source-delivery"
        try:
            with self.assertRaisesRegex(PackageError, "source bytes"):
                write_delivery_bundle(
                    self.source,
                    self.candidate,
                    destination,
                    accepted=True,
                    verification=verification,
                    source_fingerprints=ingress_fingerprints,
                    trace_seal=self.seal(),
                    run_metadata={},
                )
        finally:
            target.write_bytes(original)
        self.assertFalse(destination.exists())

    def test_destination_race_is_preserved_and_never_overwritten(self) -> None:
        destination = self.root / "raced-delivery"
        original = package_module._build_workspace_patch_from_files

        def render_then_claim(before, after):
            result = original(before, after)
            destination.mkdir()
            (destination / "changes.patch").write_text("USER DATA\n", encoding="utf-8")
            return result

        with patch(
            "forge8.package._build_workspace_patch_from_files",
            side_effect=render_then_claim,
        ), self.assertRaisesRegex(PackageError, "appeared during publication"):
            write_delivery_bundle(
                self.source,
                self.candidate,
                destination,
                accepted=True,
                verification=self.verification(),
                source_fingerprints=self.fingerprints(self.source),
                trace_seal=self.seal(),
                run_metadata={},
            )
        self.assertEqual(
            (destination / "changes.patch").read_text(encoding="utf-8"),
            "USER DATA\n",
        )

    def test_existing_nonempty_destination_is_rejected(self) -> None:
        destination = self.root / "occupied"
        destination.mkdir()
        (destination / "keep.txt").write_text("user data", encoding="utf-8")
        with self.assertRaisesRegex(PackageError, "already exists"):
            write_delivery_bundle(
                self.source,
                self.candidate,
                destination,
                accepted=True,
                verification=self.verification(),
                source_fingerprints=self.fingerprints(self.source),
                trace_seal=self.seal(),
                run_metadata={},
            )


if __name__ == "__main__":
    unittest.main()
