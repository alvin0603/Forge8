from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from forge8.workspace import (
    MAX_REPLACE_LINES,
    ArtifactStore,
    PolicyViolation,
    WorkspacePolicy,
    WorkspaceTools,
)


class WorkspaceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "main.py").write_bytes(b"alpha = 1\nprint(alpha)\n")
        self.run_root = Path(self.temp.name) / "run"
        self.policy = WorkspacePolicy(self.root, max_output_chars=20)
        self.tools = WorkspaceTools(self.policy, ArtifactStore(self.run_root))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rejects_traversal_absolute_and_credentials(self) -> None:
        for path in (
            "../outside",
            "src//main.py",
            "src/./main.py",
            "/etc/passwd",
            r"C:\\Users\\x",
            "C:relative.txt",
            ".ssh/id_rsa",
            ".git/config",
            ".gitignore",
            "src/.gitignore",
            ".env",
            ".env.production",
            "src/NUL.txt",
            "src/data.txt:secret",
        ):
            with self.subTest(path=path), self.assertRaises(PolicyViolation):
                self.policy.resolve(path, must_exist=False)

    def test_symlink_escape_is_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "escape"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(PolicyViolation):
            self.policy.resolve("escape")

    def test_even_internal_symlinks_are_rejected(self) -> None:
        link = self.root / "internal-link.py"
        try:
            link.symlink_to(self.root / "src" / "main.py")
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(PolicyViolation, "Symbolic links"):
            self.policy.resolve("internal-link.py")

    def test_hard_link_is_not_exposed_or_writable(self) -> None:
        link = self.root / "src" / "alias.py"
        try:
            link.hardlink_to(self.root / "src" / "main.py")
        except OSError:
            self.skipTest("hard links unavailable")
        result = self.tools.read_text("src/alias.py")
        self.assertFalse(result.ok)
        self.assertIn("Hard-linked", result.error or "")

        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True), self.tools.artifacts
        )
        changed = writable.write_text(
            "src/alias.py", "changed\n", expected_sha256=hashlib.sha256(
                b"alpha = 1\nprint(alpha)\n"
            ).hexdigest()
        )
        self.assertFalse(changed.ok)
        self.assertIn("Hard-linked", changed.error or "")
        line_change = writable.replace_lines(
            "src/alias.py",
            1,
            1,
            "changed",
            expected_sha256=hashlib.sha256(
                b"alpha = 1\nprint(alpha)\n"
            ).hexdigest(),
        )
        self.assertFalse(line_change.ok)
        self.assertIn("Hard-linked", line_change.error or "")

    def test_read_has_line_numbers_and_immutable_artifact(self) -> None:
        result = self.tools.read_text("src/main.py")
        self.assertTrue(result.ok)
        self.assertIn("     1|alpha", result.preview)
        self.assertEqual(
            result.metadata["sha256"],
            hashlib.sha256(b"alpha = 1\nprint(alpha)\n").hexdigest(),
        )
        assert result.artifact is not None
        stored = self.tools.artifacts.read(result.artifact).decode()
        self.assertIn("     2|print", stored)

        indented = self.root / "src" / "indented.py"
        indented.write_bytes(b"def value():\n    return 1\n")
        rendered = self.tools.read_text("src/indented.py")
        assert rendered.artifact is not None
        exact_rendering = self.tools.artifacts.read(rendered.artifact).decode()
        self.assertIn("     2|    return 1", exact_rendering)

    @unittest.skipUnless(os.name == "nt", "Windows path-length contract")
    def test_artifact_store_keeps_atomic_temp_name_below_legacy_max_path(self) -> None:
        data = b"deep artifact evidence"
        digest = hashlib.sha256(data).hexdigest()
        relative = Path("objects") / digest[:2] / digest
        base = Path(self.temp.name).resolve(strict=True)
        target_length = 254
        padding = target_length - len(str(base)) - len(str(relative)) - 2
        self.assertGreater(padding, 0)
        self.assertLessEqual(padding, 200)
        run_root = base / ("r" * padding)
        store = ArtifactStore(run_root)

        reference = store.put_bytes(data)

        target = store.run_root / reference.relative_path
        self.assertEqual(len(str(target)), target_length)
        self.assertEqual(store.read(reference), data)

    def test_long_model_view_is_truncated_but_artifact_is_complete(self) -> None:
        (self.root / "long.txt").write_text("x" * 100, encoding="utf-8")
        result = self.tools.read_text("long.txt")
        self.assertIn("truncated", result.preview)
        assert result.artifact is not None
        self.assertGreater(len(self.tools.artifacts.read(result.artifact)), len(result.preview))

    def test_search_and_list(self) -> None:
        search = self.tools.search_text("print")
        self.assertIn("src/main.py:2", search.preview)
        listing = self.tools.list_files()
        self.assertIn("src/", self.tools.artifacts.read(listing.artifact).decode())  # type: ignore[arg-type]

    def test_gitignore_stays_invisible_and_immutable_to_model_tools(self) -> None:
        content = b"print-this-must-stay-hidden\n"
        target = self.root / ".gitignore"
        target.write_bytes(content)

        listing = self.tools.list_files()
        listing_text = self.tools.artifacts.read(listing.artifact).decode()  # type: ignore[arg-type]
        direct_listing = self.tools.list_files(".gitignore")
        direct_read = self.tools.read_text(".gitignore")
        search = self.tools.search_text("print-this-must-stay-hidden")
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True),
            self.tools.artifacts,
        )
        write = writable.write_text(
            ".gitignore",
            "changed\n",
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )

        self.assertNotIn(".gitignore", listing_text)
        self.assertFalse(direct_listing.ok)
        self.assertIn("Hidden paths", direct_listing.error or "")
        self.assertFalse(direct_read.ok)
        self.assertIn("Hidden paths", direct_read.error or "")
        self.assertEqual(search.metadata["matches"], 0)
        self.assertFalse(write.ok)
        self.assertIn("Hidden paths", write.error or "")
        self.assertEqual(target.read_bytes(), content)

    def test_write_is_disabled_by_default(self) -> None:
        result = self.tools.write_text(
            "src/main.py", "changed\n", expected_sha256="missing"
        )
        self.assertFalse(result.ok)
        self.assertIn("disabled", result.error or "")
        self.assertEqual(
            (self.root / "src" / "main.py").read_text(encoding="utf-8"),
            "alpha = 1\nprint(alpha)\n",
        )

    def test_atomic_write_requires_current_hash_and_supports_new_files(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True, max_output_chars=20),
            self.tools.artifacts,
        )
        current = hashlib.sha256(b"alpha = 1\nprint(alpha)\n").hexdigest()

        stale = writable.write_text(
            "src/main.py", "alpha = 2\n", expected_sha256="0" * 64
        )
        self.assertFalse(stale.ok)
        self.assertIn("guard", stale.error or "")

        updated = writable.write_text(
            "src/main.py", "alpha = 2\n", expected_sha256=current
        )
        self.assertTrue(updated.ok)
        self.assertEqual(
            (self.root / "src" / "main.py").read_text(encoding="utf-8"),
            "alpha = 2\n",
        )
        self.assertEqual(list((self.root / "src").glob("*.forge8-*.tmp")), [])

        created = writable.write_text(
            "src/new.py", "created = True\n", expected_sha256="missing"
        )
        self.assertTrue(created.ok)
        self.assertEqual(
            (self.root / "src" / "new.py").read_text(encoding="utf-8"),
            "created = True\n",
        )

    def test_byte_identical_effects_are_rejected_without_replacing_the_file(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True), self.tools.artifacts
        )
        target = self.root / "src" / "main.py"
        original = target.read_bytes()
        guard = hashlib.sha256(original).hexdigest()

        stale_identical = writable.write_text(
            "src/main.py", original.decode("utf-8"), expected_sha256="0" * 64
        )
        self.assertFalse(stale_identical.ok)
        self.assertIn("guard", stale_identical.error or "")
        self.assertNotIn("byte-identical", stale_identical.error or "")

        identical_write = writable.write_text(
            "src/main.py", original.decode("utf-8"), expected_sha256=guard
        )
        self.assertFalse(identical_write.ok)
        self.assertIn("byte-identical", identical_write.error or "")
        self.assertEqual(identical_write.metadata["actual_sha256"], guard)
        self.assertIsNone(identical_write.artifact)

        identical_lines = writable.replace_lines(
            "src/main.py", 1, 1, "alpha = 1", expected_sha256=guard
        )
        self.assertFalse(identical_lines.ok)
        self.assertIn("byte-identical", identical_lines.error or "")

        identical_text = writable.replace_text(
            "src/main.py", "alpha = 1", "alpha = 1", expected_sha256=guard
        )
        self.assertFalse(identical_text.ok)
        self.assertIn("byte-identical", identical_text.error or "")
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list((self.root / "src").glob("*.forge8-*.tmp")), [])

        empty_creation = writable.write_text(
            "src/empty.py", "", expected_sha256="missing"
        )
        self.assertTrue(empty_creation.ok, empty_creation.error)
        self.assertTrue((self.root / "src" / "empty.py").is_file())
        self.assertEqual((self.root / "src" / "empty.py").read_bytes(), b"")

    def test_replace_text_requires_unique_span_and_current_file_hash(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True, max_output_chars=200),
            self.tools.artifacts,
        )
        current = hashlib.sha256(b"alpha = 1\nprint(alpha)\n").hexdigest()

        stale = writable.replace_text(
            "src/main.py",
            "alpha = 1",
            "alpha = 2",
            expected_sha256="0" * 64,
        )
        self.assertFalse(stale.ok)
        self.assertIn("guard", stale.error or "")

        missing = writable.replace_text(
            "src/main.py",
            "does not exist",
            "replacement",
            expected_sha256=current,
        )
        self.assertFalse(missing.ok)
        self.assertEqual(missing.metadata["matches"], 0)

        ambiguous = writable.replace_text(
            "src/main.py",
            "alpha",
            "beta",
            expected_sha256=current,
        )
        self.assertFalse(ambiguous.ok)
        self.assertEqual(ambiguous.metadata["matches"], 2)

        replaced = writable.replace_text(
            "src/main.py",
            "alpha = 1\n",
            "alpha = 2\n",
            expected_sha256=current,
        )
        self.assertTrue(replaced.ok)
        self.assertEqual(replaced.metadata["matches"], 1)
        self.assertEqual(
            (self.root / "src" / "main.py").read_text(encoding="utf-8"),
            "alpha = 2\nprint(alpha)\n",
        )

    def test_replace_text_is_disabled_and_never_creates_or_inserts_ambiguously(self) -> None:
        current = hashlib.sha256(b"alpha = 1\nprint(alpha)\n").hexdigest()
        disabled = self.tools.replace_text(
            "src/main.py", "alpha", "beta", expected_sha256=current
        )
        self.assertFalse(disabled.ok)
        self.assertIn("disabled", disabled.error or "")

        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True), self.tools.artifacts
        )
        empty = writable.replace_text(
            "src/main.py", "", "inserted", expected_sha256=current
        )
        self.assertFalse(empty.ok)
        self.assertIn("must not be empty", empty.error or "")
        absent = writable.replace_text(
            "src/new.py", "old", "new", expected_sha256=current
        )
        self.assertFalse(absent.ok)
        self.assertFalse((self.root / "src" / "new.py").exists())

    def test_replace_text_repairs_one_escape_layer_only_for_unique_preimage(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True, max_output_chars=200),
            self.tools.artifacts,
        )
        current = hashlib.sha256(b"alpha = 1\nprint(alpha)\n").hexdigest()
        result = writable.replace_text(
            "src/main.py",
            r"alpha = 1\nprint(alpha)\n",
            "alpha = 3\nprint(alpha)\n",
            expected_sha256=current,
        )

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.metadata["preimage_normalized"])
        self.assertEqual(
            (self.root / "src" / "main.py").read_text(encoding="utf-8"),
            "alpha = 3\nprint(alpha)\n",
        )

    def test_replace_lines_is_guarded_atomic_and_reports_range_metadata(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True, max_output_chars=200),
            self.tools.artifacts,
        )
        target = self.root / "src" / "main.py"
        before = target.read_bytes()
        before_sha = hashlib.sha256(before).hexdigest()
        expected = b"alpha = 2\nprint(alpha)\n"

        result = writable.replace_lines(
            "src/main.py",
            1,
            1,
            "alpha = 2",
            expected_sha256=before_sha,
        )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(target.read_bytes(), expected)
        self.assertEqual(result.metadata["before_sha256"], before_sha)
        self.assertEqual(
            result.metadata["after_sha256"],
            hashlib.sha256(expected).hexdigest(),
        )
        self.assertEqual(
            (
                result.metadata["start_line"],
                result.metadata["end_line"],
                result.metadata["replaced_line_count"],
                result.metadata["line_count_before"],
            ),
            (1, 1, 1, 2),
        )
        self.assertTrue(result.metadata["terminal_eol_preserved"])
        self.assertEqual(list((self.root / "src").glob("*.forge8-*.tmp")), [])
        assert result.artifact is not None
        self.assertEqual(self.tools.artifacts.read(result.artifact), expected)

    def test_replace_lines_preserves_crlf_and_mixed_terminal_boundaries(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True, max_output_chars=200),
            self.tools.artifacts,
        )
        target = self.root / "mixed.txt"
        original = b"one\r\ntwo\nthree\rfour"
        target.write_bytes(original)

        result = writable.replace_lines(
            "mixed.txt",
            2,
            3,
            "middle",
            expected_sha256=hashlib.sha256(original).hexdigest(),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(target.read_bytes(), b"one\r\nmiddle\rfour")
        self.assertTrue(result.metadata["terminal_eol_preserved"])

        crlf = b"one\r\ntwo\r\nthree\r\n"
        target.write_bytes(crlf)
        crlf_result = writable.replace_lines(
            "mixed.txt",
            2,
            2,
            "TWO",
            expected_sha256=hashlib.sha256(crlf).hexdigest(),
        )
        self.assertTrue(crlf_result.ok, crlf_result.error)
        self.assertEqual(target.read_bytes(), b"one\r\nTWO\r\nthree\r\n")

    def test_replace_lines_respects_explicit_eol_and_unterminated_eof(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True), self.tools.artifacts
        )
        target = self.root / "lines.txt"
        original = b"one\r\ntwo\r\nlast"
        target.write_bytes(original)

        explicit = writable.replace_lines(
            "lines.txt",
            2,
            2,
            "TWO\n",
            expected_sha256=hashlib.sha256(original).hexdigest(),
        )
        self.assertTrue(explicit.ok, explicit.error)
        self.assertEqual(target.read_bytes(), b"one\r\nTWO\nlast")
        self.assertFalse(explicit.metadata["terminal_eol_preserved"])

        before_eof = target.read_bytes()
        eof = writable.replace_lines(
            "lines.txt",
            3,
            3,
            "LAST",
            expected_sha256=hashlib.sha256(before_eof).hexdigest(),
        )
        self.assertTrue(eof.ok, eof.error)
        self.assertEqual(target.read_bytes(), b"one\r\nTWO\nLAST")
        self.assertFalse(eof.metadata["terminal_eol_preserved"])

    def test_replace_lines_empty_new_text_deletes_the_exact_range(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True), self.tools.artifacts
        )
        target = self.root / "delete.txt"
        original = b"keep\nremove one\nremove two\nlast"
        target.write_bytes(original)

        result = writable.replace_lines(
            "delete.txt",
            2,
            3,
            "",
            expected_sha256=hashlib.sha256(original).hexdigest(),
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(target.read_bytes(), b"keep\nlast")
        self.assertFalse(result.metadata["terminal_eol_preserved"])

    def test_replace_lines_range_bounds_are_strict_and_side_effect_free(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True), self.tools.artifacts
        )
        target = self.root / "src" / "main.py"
        original = target.read_bytes()
        guard = hashlib.sha256(original).hexdigest()
        invalid_ranges = [
            (0, 1),
            (2, 1),
            (True, 1),
            (1, False),
            (1.5, 1),
            (1, MAX_REPLACE_LINES + 1),
            (1, 3),
        ]
        for start_line, end_line in invalid_ranges:
            with self.subTest(start_line=start_line, end_line=end_line):
                result = writable.replace_lines(  # type: ignore[arg-type]
                    "src/main.py",
                    start_line,
                    end_line,
                    "replacement",
                    expected_sha256=guard,
                )
                self.assertFalse(result.ok)
                self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list((self.root / "src").glob("*.forge8-*.tmp")), [])

    def test_replace_lines_accepts_400_existing_lines_but_not_401(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True), self.tools.artifacts
        )
        target = self.root / "many.txt"
        original = "".join(f"line {number}\n" for number in range(1, 402)).encode()
        target.write_bytes(original)
        guard = hashlib.sha256(original).hexdigest()

        too_many = writable.replace_lines(
            "many.txt",
            1,
            MAX_REPLACE_LINES + 1,
            "head",
            expected_sha256=guard,
        )
        self.assertFalse(too_many.ok)
        self.assertEqual(target.read_bytes(), original)

        boundary = writable.replace_lines(
            "many.txt",
            1,
            MAX_REPLACE_LINES,
            "head",
            expected_sha256=guard,
        )
        self.assertTrue(boundary.ok, boundary.error)
        self.assertEqual(target.read_bytes(), b"head\nline 401\n")
        self.assertEqual(boundary.metadata["replaced_line_count"], MAX_REPLACE_LINES)

    def test_replace_lines_rejects_stale_missing_non_utf8_and_oversized_effects(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(
                self.root,
                allow_write=True,
                max_read_bytes=256 * 1024,
                max_write_bytes=24,
            ),
            self.tools.artifacts,
        )
        target = self.root / "src" / "main.py"
        original = target.read_bytes()
        guard = hashlib.sha256(original).hexdigest()

        failures = [
            writable.replace_lines(
                "src/main.py", 1, 1, "changed", expected_sha256="0" * 64
            ),
            writable.replace_lines(
                "src/main.py", 1, 1, "changed", expected_sha256="missing"
            ),
            writable.replace_lines(
                "src/missing.py", 1, 1, "changed", expected_sha256=guard
            ),
            writable.replace_lines("src", 1, 1, "changed", expected_sha256=guard),
            writable.replace_lines(
                "src/main.py", 1, 1, "x" * 30, expected_sha256=guard
            ),
            writable.replace_lines(
                "src/main.py", 1, 1, "bad\x00text", expected_sha256=guard
            ),
            writable.replace_lines(  # type: ignore[arg-type]
                "src/main.py", 1, 1, None, expected_sha256=guard
            ),
            writable.replace_lines(
                "src/main.py", 1, 1, "\ud800", expected_sha256=guard
            ),
        ]
        self.assertTrue(all(not result.ok for result in failures))
        self.assertEqual(target.read_bytes(), original)

        binary = self.root / "binary.txt"
        binary.write_bytes(b"\xff\n")
        binary_result = writable.replace_lines(
            "binary.txt",
            1,
            1,
            "text",
            expected_sha256=hashlib.sha256(b"\xff\n").hexdigest(),
        )
        self.assertFalse(binary_result.ok)
        self.assertEqual(binary.read_bytes(), b"\xff\n")

        read_limited = WorkspaceTools(
            WorkspacePolicy(
                self.root,
                allow_write=True,
                max_read_bytes=len(original) - 1,
            ),
            self.tools.artifacts,
        )
        too_large_to_read = read_limited.replace_lines(
            "src/main.py", 1, 1, "changed", expected_sha256=guard
        )
        self.assertFalse(too_large_to_read.ok)
        self.assertIn("read policy", too_large_to_read.error or "")
        self.assertEqual(target.read_bytes(), original)

        disabled = self.tools.replace_lines(
            "src/main.py", 1, 1, "changed", expected_sha256=guard
        )
        self.assertFalse(disabled.ok)
        self.assertEqual(target.read_bytes(), original)

    def test_write_and_replace_reject_null_text(self) -> None:
        writable = WorkspaceTools(
            WorkspacePolicy(self.root, allow_write=True), self.tools.artifacts
        )
        current = hashlib.sha256(b"alpha = 1\nprint(alpha)\n").hexdigest()
        self.assertFalse(
            writable.write_text(
                "src/main.py", "before\x00after", expected_sha256=current
            ).ok
        )
        self.assertFalse(
            writable.replace_text(
                "src/main.py", "alpha", "before\x00after", expected_sha256=current
            ).ok
        )


if __name__ == "__main__":
    unittest.main()
