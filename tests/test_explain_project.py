"""End-to-end read-only admission on normal mixed-content projects."""

import json
import tempfile
import unittest
from pathlib import Path

from forge8.explain import prepare_explanation, run_explanation
from test_explain import ScriptedBackend, passing_gate, read_service, answer_document


class MixedProjectExplanationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-mixed-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "repo"
        (self.source / "src").mkdir(parents=True)
        (self.source / "src/service.py").write_text(
            '\"\"\"Order policy.\"\"\"\n\ndef normalize(total):\n    return max(total, 0)\n',
            encoding="utf-8",
        )
        (self.source / ".github").mkdir()
        (self.source / ".github/workflow.yml").write_text("not admitted", encoding="utf-8")
        (self.source / "logo.png").write_bytes(b"\x89PNG\x00\xff")

    def prepare(self):
        return prepare_explanation(
            self.source, self.root / "run", "How are negative totals handled?", "mixed-project"
        )

    def test_mixed_project_answers_with_explicit_exclusions_and_unchanged_source(self):
        prepared = self.prepare()
        outcome = run_explanation(
            prepared, ScriptedBackend([read_service(), answer_document()]),
            model="scripted", acceptance_gate=passing_gate,
        )
        self.assertTrue(outcome.ok, outcome.failure_reason)
        self.assertTrue(outcome.source_unchanged)
        self.assertTrue(outcome.snapshot_unchanged)
        ingress = json.loads(prepared.ingress_path.read_text(encoding="utf-8"))
        self.assertEqual(set(ingress["excluded"]), {".github", "logo.png"})
        self.assertEqual(outcome.coverage["admitted"]["files"], 1)
        self.assertEqual(set(outcome.coverage["excluded"]["paths"]), {".github", "logo.png"})
        answer = Path(outcome.answer_path).read_text(encoding="utf-8")
        self.assertIn("Excluded: 2 entries (not sent to model or source-verified)", answer)
        self.assertEqual((self.source / "logo.png").read_bytes(), b"\x89PNG\x00\xff")

    def test_newly_readable_excluded_file_invalidates_source_inventory(self):
        prepared = self.prepare()
        (self.source / "logo.png").write_text("now readable source\n", encoding="utf-8")
        backend = ScriptedBackend([])
        outcome = run_explanation(
            prepared, backend, model="scripted", acceptance_gate=passing_gate,
        )
        self.assertEqual(outcome.status, "source_drift")
        self.assertEqual(backend.requests, [])

    def test_binary_injection_into_snapshot_is_not_silently_excluded(self):
        prepared = self.prepare()
        (prepared.run_root / "input/source/injected.bin").write_bytes(b"\x00\xff")
        backend = ScriptedBackend([])
        outcome = run_explanation(
            prepared, backend, model="scripted", acceptance_gate=passing_gate,
        )
        self.assertEqual(outcome.status, "snapshot_drift")
        self.assertEqual(backend.requests, [])


if __name__ == "__main__":
    unittest.main()
