from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge8 import comparison
from forge8.explain import _canonical_json, _snapshot_identity
from forge8.git_source import GitBaseline, GitSourceError


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-comparison-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "project"
        self.source.mkdir()
        (self.source / ".git").mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.destination = self.state / "comparison"
        self.head = "a" * 40
        self.old = (b'DEFAULT = {"label": "unknown"}\r\n\r\n'
            b'def get_record(cache, key):\r\n    return cache.get(key) or DEFAULT\r\n')
        self.new = self.old.replace(b"cache.get(key) or DEFAULT", b"cache.get(key, DEFAULT)")
        self.caller = b'def label(cache):\n    return get_record(cache, "item")["label"]\n'
        self.write("records.py", self.new)
        self.write("caller.py", self.caller)
        self.baseline = GitBaseline(self.head, {"records.py": self.old, "caller.py": self.caller}, ("build",))
        read_patch = patch.object(comparison, "read_head", return_value=self.baseline)
        identity_patch = patch.object(comparison, "head_identity", return_value=self.head)
        self.addCleanup(read_patch.stop)
        self.addCleanup(identity_patch.stop)
        self.read = read_patch.start()
        self.identity = identity_patch.start()

    def write(self, path, content):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def prepare(self):
        return comparison.prepare_comparison(self.source, self.destination)

    def assert_unpublished(self):
        self.assertFalse(os.path.lexists(self.destination))
        self.assertEqual(list(self.state.iterdir()), [])

    def test_exact_original_bytes_lines_unchanged_caller_and_defaults_are_retained(self):
        result = self.prepare()
        self.assertEqual(result.source, self.source)
        self.assertEqual(result.reading_source, self.destination)
        self.assertEqual(Path(result.snapshot.snapshot_root), self.destination)
        self.assertEqual((self.destination / "before/records.py").read_bytes(), self.old)
        self.assertEqual((self.destination / "after/records.py").read_bytes(), self.new)
        for side in ("before", "after"):
            self.assertEqual((self.destination / side / "caller.py").read_bytes(), self.caller)
        self.assertEqual((self.source / "records.py").read_bytes(), self.new)
        self.assertEqual([entry["path"] for entry in result.catalogue["files"]], ["records.py"])
        self.assertEqual(result.catalogue["files"][0]["units"][0]["before"],
            {"start_line": 3, "end_line": 4})
        version, counts = _snapshot_identity(result.snapshot)
        self.assertEqual(dict(counts)["before/records.py"], 4)
        self.assertEqual(version, result._capsule_sha256)
        self.assertEqual(len(result.snapshot.fingerprints), 5)
        self.read.assert_called_once()
        self.assertNotEqual(self.read.call_args.args[1], self.source)

    def test_provenance_binds_both_versions_and_exclusions_without_its_own_hash(self):
        self.write(".editorconfig", b"excluded configuration\n")
        result = self.prepare()
        document = json.loads((self.destination / "comparison.json").read_text())
        self.assertEqual(document["head"], self.head)
        self.assertEqual(document["after"]["snapshot_sha256"], result.original_snapshot_sha256)
        self.assertEqual(document["after"]["excluded"], [".editorconfig", ".git"])
        self.assertEqual(document["before"]["excluded"], ["build"])
        self.assertEqual(result.view()["excluded"], {
            "before": ["build"], "after": [".editorconfig", ".git"]})
        before = {item["path"]: item for item in document["before"]["files"]}
        self.assertEqual(before["records.py"]["sha256"], hashlib.sha256(self.old).hexdigest())
        self.assertNotIn(result._capsule_sha256, (self.destination / "comparison.json").read_text())
        self.assertNotIn(str(self.state), json.dumps(document))
        self.assertFalse(document["source_executed"])
        self.assertFalse(document["semantics_verified"])
        self.assertEqual(document["catalogue_sha256"], hashlib.sha256(_canonical_json(result.catalogue)).hexdigest())

    def test_guard_success_has_exact_versioned_operational_evidence_not_semantics(self):
        result = self.prepare()
        gate = result.guard()
        self.assertTrue(gate.ok)
        self.assertIsNone(gate.reason)
        self.assertEqual(gate.evidence["head"], self.head)
        self.assertEqual(gate.evidence["original_snapshot_sha256"], result.original_snapshot_sha256)
        self.assertTrue(gate.evidence["capsule_unchanged"])
        self.assertTrue(gate.evidence["catalogue_unchanged"])
        self.assertFalse(gate.evidence["semantics_verified"])
        self.assertEqual(list(self.state.iterdir()), [self.destination])

    def test_view_returns_independent_catalogue_and_explicit_retained_scope(self):
        result = self.prepare()
        view = result.view()
        self.assertEqual(view["scope"], "retained comparison sources")
        self.assertEqual(view["sides"], {"before": "before/", "after": "after/"})
        view["catalogue"]["files"].clear()
        self.assertTrue(result.catalogue["files"])
        self.assertTrue(result.guard().ok)
        result.catalogue["files"].clear()
        self.assertEqual(result.guard().reason, "comparison_catalogue_changed")

    def test_saved_source_or_head_drift_fails_without_changing_retained_bytes(self):
        result = self.prepare()
        self.write("records.py", self.new + b"# changed afterwards\n")
        self.assertEqual(result.guard().reason, "comparison_source_changed")
        self.assertEqual((self.destination / "after/records.py").read_bytes(), self.new)
        self.write("records.py", self.new)
        self.identity.return_value = "b" * 40
        self.assertEqual(result.guard().reason, "comparison_head_changed")

    def test_excluded_inventory_drift_is_not_mistaken_for_unchanged_source(self):
        result = self.prepare()
        self.write(".editorconfig", b"new excluded file\n")
        self.assertEqual(result.guard().reason, "comparison_source_changed")

    def test_retained_source_provenance_and_extra_file_tampering_fail(self):
        result = self.prepare()
        for relative in ("before/records.py", "comparison.json"):
            with self.subTest(relative=relative):
                path = self.destination / relative
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                self.assertEqual(result.guard().reason, "comparison_capsule_changed")
                path.write_bytes(original)
        (self.destination / "extra.py").write_bytes(b"x = 1\n")
        self.assertEqual(result.guard().reason, "comparison_capsule_changed")

    def test_added_and_absent_sources_have_only_the_existing_side(self):
        self.baseline.files["old.py"] = b"def old(): return 1\n"
        self.write("new.py", b"def new(): return 2\n")
        result = self.prepare()
        self.assertTrue((self.destination / "before/old.py").is_file())
        self.assertFalse((self.destination / "after/old.py").exists())
        self.assertTrue((self.destination / "after/new.py").is_file())
        self.assertFalse((self.destination / "before/new.py").exists())
        entries = {entry["path"]: entry for entry in result.catalogue["files"]}
        self.assertEqual(entries["old.py"]["status"], "absent")

    def test_same_bytes_and_head_produce_same_capsule_identity_at_another_destination(self):
        first = self.prepare()
        second = comparison.prepare_comparison(self.source, self.state / "another")
        self.assertEqual(first._capsule_sha256, second._capsule_sha256)
        self.assertEqual(first.view(), second.view())

    def test_joint_file_limit_counts_provenance_and_never_truncates(self):
        with patch.object(comparison, "MAX_REPOSITORY_FILES", 4):
            with self.assertRaisesRegex(comparison.ComparisonError, "comparison_source_limit"):
                self.prepare()
        self.assert_unpublished()

    def test_provenance_size_and_shared_byte_limit_are_admitted_before_publication(self):
        with patch.object(comparison, "MAX_REPOSITORY_FILE_BYTES", 300):
            with self.assertRaisesRegex(comparison.ComparisonError, "comparison_provenance_limit"):
                self.prepare()
        self.assert_unpublished()
        source_bytes = 2 * len(self.caller) + len(self.old) + len(self.new)
        with patch.object(comparison, "MAX_REPOSITORY_BYTES", source_bytes):
            with self.assertRaisesRegex(comparison.ComparisonError, "comparison_source_limit"):
                self.prepare()
        self.assert_unpublished()

    def test_head_drift_during_capture_never_publishes(self):
        self.identity.return_value = "b" * 40
        with self.assertRaisesRegex(comparison.ComparisonError, "comparison_head_changed"):
            self.prepare()
        self.assert_unpublished()

    def test_source_drift_between_snapshot_and_final_guard_never_publishes(self):
        original = comparison.build_change_catalogue
        def change_after_capture(before, after):
            result = original(before, after)
            self.write("records.py", self.new + b"# saved during preparation\n")
            return result
        with patch.object(comparison, "build_change_catalogue", side_effect=change_after_capture):
            with self.assertRaisesRegex(comparison.ComparisonError, "comparison_source_changed"):
                self.prepare()
        self.assert_unpublished()

    def test_git_failure_is_fixed_diagnostic_without_private_exception_text(self):
        self.read.side_effect = GitSourceError("private remote configuration")
        with self.assertRaises(comparison.ComparisonError) as raised:
            self.prepare()
        self.assertEqual(str(raised.exception), "comparison_preparation_unavailable")
        self.assert_unpublished()

    def test_destination_overlap_existing_target_and_malformed_adapter_path_are_safe(self):
        with self.assertRaisesRegex(comparison.ComparisonError, "comparison_source_overlap"):
            comparison.prepare_comparison(self.source, self.source / "nested")
        self.read.assert_not_called()
        self.destination.mkdir()
        sentinel = self.destination / "keep.txt"
        sentinel.write_bytes(b"owned existing content")
        with self.assertRaises(comparison.ComparisonError):
            self.prepare()
        self.assertEqual(sentinel.read_bytes(), b"owned existing content")
        sentinel.unlink()
        self.destination.rmdir()
        self.baseline.files["../escaped.py"] = b"value = 1\n"
        with self.assertRaises(comparison.ComparisonError):
            self.prepare()
        self.assert_unpublished()
        self.assertFalse((self.state / "escaped.py").exists())


if __name__ == "__main__":
    unittest.main()
