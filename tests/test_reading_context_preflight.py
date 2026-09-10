"""Owned inert source fixtures; preparation never executes source or a model."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge8 import cli, explain


class ReadingContextPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="f8-context-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()

    def test_unicode_path_expansion_can_exceed_the_action_budget_estimate(self):
        # Exact geometry is independent of native long-path support. U+200B is
        # permitted in these source path components but is escaped for display.
        path = "/".join(letter + "\u200b" * 80 for letter in "abc") + ".py"
        self.assertEqual(len(path), 248)
        self.assertTrue(all(len(part.encode("utf-8")) <= 255 for part in path.split("/")))
        focus = tuple(f"{path}:{number}-{number}" for number in range(1, 4))
        explain._focus_actions(focus)
        evidence = [SimpleNamespace(evidence_id=f"E{number}", path=path,
            start_line=number, end_line=number,
            model_text=f"{number:>6}|#" + "x" * 2192) for number in range(1, 4)]
        self.assertTrue(all(len(item.model_text) == 2200 for item in evidence))
        self.assertEqual(len(explain._evidence_context(evidence)), 7556)
        prepared = SimpleNamespace(task_id="parent", snapshot_sha256="a" * 64,
            question="Explain these ranges.", focus=focus, context_supplements=())
        self.assertLessEqual(len(explain._reading_context(prepared, evidence)), 12000)
        prepared.question = "Q" * 2000
        self.assertLessEqual(len(explain._request_prefix(prepared, evidence))
            + len(explain._FINAL_ACTION_SUFFIX) + 1, 12000)
        with self.assertRaisesRegex(explain.ExplanationError, "fixed prompt budget"):
            explain._reading_context(prepared, evidence)

    def test_longer_question_rejects_before_hashing_or_model_startup(self):
        # Six project windows exercise the same expansion with one short native
        # filename. The source is only comments plus a never-executed sentinel.
        path = "a" + "\u200b" * 80 + ".py"
        original = (("#" + "x" * 1192 + "\n") * 6
            + 'raise RuntimeError("this source must never execute")\n').encode("utf-8")
        (self.source / path).write_bytes(original)
        focus = tuple(f"{path}:{number}-{number}" for number in range(1, 7))
        parent = explain.prepare_explanation(self.source, self.root / "parent",
            "Explain these ranges.", "parent", focus=focus, focus_origin="project_candidates")
        self.assertEqual(parent.focus, focus)
        self.assertEqual(parent.focus_origin, "project_candidates")
        self.assertEqual(parent.context_supplements, ())
        assets, runs = self.root / "assets", self.root / "runs"
        assets.mkdir()
        runs.mkdir()
        args = SimpleNamespace(repo=self.source, question="Q" * 2000,
            focus=list(focus), reader="qwen35", as_json=True)
        results = []
        with (patch.object(cli, "_resolve_fix_asset_root", return_value=assets),
              patch.object(cli, "_fix_runs_parent", return_value=runs),
              patch.object(cli, "_new_task_id", return_value="continued"),
              patch.object(cli, "_fix_asset_anchors") as anchors,
              patch.object(cli, "prepare_server") as prepare_server,
              patch.object(cli, "LocalServerSupervisor") as supervisor,
              patch.object(cli, "OpenAITransport") as transport):
            code = cli._run_explain_cli(args, on_result=results.append,
                on_progress=lambda _: None, expected_snapshot=parent.snapshot_sha256,
                focus_origin=parent.focus_origin, context_supplements=parent.context_supplements)
        self.assertEqual(code, 2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "configuration_error")
        self.assertIn("selected source exceeds the fixed prompt budget", results[0]["error"])
        self.assertFalse(results[0]["repository_code_executed"])
        self.assertIsNone(results[0]["server"])
        for call in (anchors, prepare_server, supervisor, transport):
            call.assert_not_called()
        self.assertEqual(sorted(item.name for item in (runs / "continued").iterdir()), ["input"])
        self.assertEqual((self.source / path).read_bytes(), original)

    def test_normal_manual_focus_without_supplements_still_prepares(self):
        original = (b'raise RuntimeError("this source must never execute")\n'
            b'def identity(value):\n    return value\n')
        (self.source / "identity.py").write_bytes(original)
        with patch.object(explain, "_reading_context", wraps=explain._reading_context) as rendered:
            prepared = explain.prepare_explanation(self.source, self.root / "ordinary",
                "Explain identity().", "ordinary", focus=("identity.py:2-3",))
        rendered.assert_called_once()
        self.assertEqual(prepared.focus_origin, "user_focus")
        self.assertEqual(prepared.context_supplements, ())
        ingress = json.loads(prepared.ingress_path.read_text(encoding="utf-8"))
        self.assertEqual(ingress["focus"], ["identity.py:2-3"])
        self.assertNotIn("focus_origin", ingress)
        self.assertNotIn("context_supplements", ingress)
        self.assertEqual(sorted(item.name for item in prepared.run_root.iterdir()), ["input"])
        self.assertEqual((self.source / "identity.py").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
