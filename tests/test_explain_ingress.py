from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import forge8.repository as repository
from forge8.repository import RepositoryError, prepare_repository_snapshot


class ExplainIngressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-mixed-ingress-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "project"
        self.source.mkdir()
        self.code = self.source / "app.py"
        self.code.write_bytes(b"def answer():\n    return 42\n")
        self.destination = self.root / "snapshot"

    def prepare(self):
        return prepare_repository_snapshot(
            self.source, self.destination, for_explanation=True
        )

    def assert_rejected(self, pattern: str) -> None:
        with self.assertRaisesRegex(RepositoryError, pattern):
            self.prepare()
        self.assertFalse(os.path.lexists(self.destination))
        self.assertEqual(list(self.root.glob(".snapshot.forge8-partial-*")), [])

    def test_mixed_project_copies_only_text_and_explicitly_lists_exclusions(self) -> None:
        for directory in (".github", ".vscode"):
            path = self.source / directory
            path.mkdir()
            (path / "metadata.json").write_bytes(b'{"private": true}\n')
        assets = self.source / "assets"
        assets.mkdir()
        payloads = {
            "icon.png": b"\x89PNG\r\n\x1a\n\x00binary",
            "legacy.txt": b"caf\xe9\n",
            "control.dat": b"prefix\x01suffix",
            "zero.bin": b"\x00",
        }
        for name, content in payloads.items():
            (assets / name).write_bytes(content)
        (assets / "diagram.svg").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>\n')
        (self.source / ".gitignore").write_bytes(b"*.png\n")
        before = self.code.read_bytes()

        result = self.prepare()

        self.assertEqual(
            set(result.excluded),
            {".github", ".vscode", *(f"assets/{name}" for name in payloads)},
        )
        self.assertEqual(
            {item.path for item in result.fingerprints},
            {"app.py", ".gitignore", "assets/diagram.svg"},
        )
        self.assertEqual(result.as_dict()["excluded"], list(result.excluded))
        self.assertEqual(self.code.read_bytes(), before)
        self.assertEqual((self.destination / "app.py").read_bytes(), before)
        for name, content in payloads.items():
            self.assertEqual((assets / name).read_bytes(), content)
        for path in result.excluded:
            self.assertFalse((self.destination / path).exists(), path)

    def test_strict_default_still_rejects_each_new_explanation_exclusion(self) -> None:
        cases = ((".github", None, "hidden"), (".vscode", None, "hidden"),
                 (".devcontainer", None, "hidden"), (".editorconfig", b"root=true", "hidden"),
                 (".pre-commit-config.yaml", b"repos: []", "hidden"),
                 (".readthedocs.yaml", b"version: 2", "hidden"),
                 (".python-version", b"3.12", "hidden"),
                 ("asset.png", b"\x00", "NUL"), ("legacy.txt", b"\xff", "UTF-8"))
        for index, (name, content, reason) in enumerate(cases):
            with self.subTest(name=name):
                path = self.source / name
                path.mkdir() if content is None else path.write_bytes(content)
                with self.assertRaisesRegex(RepositoryError, reason):
                    prepare_repository_snapshot(self.source, self.root / f"strict-{index}")
                path.rmdir() if content is None else path.unlink()

    def test_other_hidden_paths_and_metadata_files_stay_rejected(self) -> None:
        for name in (".custom", ".github", ".vscode", ".devcontainer"):
            with self.subTest(name=name):
                path = self.source / name
                path.write_bytes(b"\xff")
                self.assert_rejected("hidden")
                path.unlink()

    def test_development_configuration_is_excluded_without_reading_or_traversing(self) -> None:
        directory = self.source / ".devcontainer"
        directory.mkdir()
        (directory / ".env").write_bytes(b"synthetic unread fixture")
        names = (".editorconfig", ".pre-commit-config.yaml", ".readthedocs.yaml", ".python-version")
        for name in names:
            (self.source / name).write_bytes(b"\x00unread configuration")
        read_file, scandir = repository._read_regular_file, os.scandir

        def guarded_read(path, metadata, relative):
            self.assertNotIn(path.name, names, "excluded configuration was opened")
            return read_file(path, metadata, relative)

        def guarded_scan(path):
            self.assertNotEqual(Path(path), directory, "excluded directory was traversed")
            return scandir(path)

        with mock.patch("forge8.repository._read_regular_file", side_effect=guarded_read), mock.patch(
            "forge8.repository.os.scandir", side_effect=guarded_scan,
        ):
            result = self.prepare()
        self.assertEqual(set(result.excluded), {".devcontainer", *names})
        self.assertEqual([item.path for item in result.fingerprints], ["app.py"])
        self.assertEqual(list(self.destination.iterdir()), [self.destination / "app.py"])

    def test_file_only_configuration_names_do_not_exclude_directories(self) -> None:
        for name in (".editorconfig", ".pre-commit-config.yaml", ".readthedocs.yaml", ".python-version"):
            with self.subTest(name=name):
                path = self.source / name
                path.mkdir()
                self.assert_rejected("hidden")
                path.rmdir()

    def test_credentials_stay_rejected_before_non_text_classification(self) -> None:
        for name in (".env", ".env.local", "private.key", "credentials.json"):
            with self.subTest(name=name):
                path = self.source / name
                path.write_bytes(b"\x00secret")
                self.assert_rejected("credential-like")
                path.unlink()

    def test_symlinks_are_rejected_even_when_target_would_be_excluded(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        for name, directory in ((".github", True), (".devcontainer", True),
                                (".editorconfig", False), (".pre-commit-config.yaml", False),
                                (".readthedocs.yaml", False), (".python-version", False), ("asset.png", False)):
            link = self.source / name
            try:
                link.symlink_to(outside if directory else self.code,
                                target_is_directory=directory)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.subTest(name=name):
                self.assert_rejected("symlinks and reparse")
            link.unlink()

    def test_non_text_hardlinks_are_rejected(self) -> None:
        binary = self.root / "outside.bin"
        binary.write_bytes(b"\x00")
        try:
            os.link(binary, self.source / "asset.bin")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        self.assert_rejected("hard-linked")

    def test_configuration_hardlinks_are_rejected_before_exclusion(self) -> None:
        original = self.root / "original.txt"
        original.write_bytes(b"synthetic outside configuration")
        for name in (".editorconfig", ".pre-commit-config.yaml", ".readthedocs.yaml", ".python-version"):
            with self.subTest(name=name):
                path = self.source / name
                try:
                    os.link(original, path)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"hardlinks unavailable: {exc}")
                self.assert_rejected("hard-linked")
                path.unlink()

    def test_reparse_entries_are_rejected_before_exclusion(self) -> None:
        original = repository._is_reparse_point
        for name, directory in ((".vscode", True), (".devcontainer", True),
                                (".editorconfig", False), (".pre-commit-config.yaml", False),
                                (".readthedocs.yaml", False), (".python-version", False)):
            with self.subTest(name=name):
                path = self.source / name
                path.mkdir() if directory else path.write_bytes(b"configuration")
                target_inode = path.stat().st_ino
                with mock.patch(
                    "forge8.repository._is_reparse_point",
                    side_effect=lambda value: value.st_ino == target_inode or original(value),
                ):
                    self.assert_rejected("reparse")
                path.rmdir() if directory else path.unlink()

    def test_configuration_special_file_kind_is_rejected_before_exclusion(self) -> None:
        path = self.source / ".editorconfig"
        path.write_bytes(b"fixture with synthetic special-file metadata")
        lstat = Path.lstat
        metadata = SimpleNamespace(st_mode=stat.S_IFIFO, st_file_attributes=0, st_nlink=1)
        with mock.patch.object(Path, "lstat", lambda value: metadata if value == path else lstat(value)):
            self.assert_rejected("special files")

    def test_read_errors_and_source_races_are_not_reclassified_as_exclusions(self) -> None:
        for reason in ("cannot open repository file", "repository file changed while reading"):
            with self.subTest(reason=reason), mock.patch(
                "forge8.repository._read_regular_file",
                side_effect=RepositoryError(reason),
            ):
                self.assert_rejected(reason)

    def test_changed_size_after_directory_stat_cannot_undercount_read_budget(self) -> None:
        metadata = self.code.lstat()
        self.code.write_bytes(self.code.read_bytes() + b"# changed before opening\n")
        with self.assertRaisesRegex(RepositoryError, "changed while reading"):
            repository._read_regular_file(self.code, metadata, "app.py")

    def test_oversized_binary_is_still_rejected(self) -> None:
        (self.source / "asset.bin").write_bytes(
            b"\x00" * (repository.MAX_REPOSITORY_FILE_BYTES + 1)
        )
        self.assert_rejected("file exceeds")

    def test_non_text_classification_counts_toward_scan_budgets(self) -> None:
        binary = b"\x00binary"
        (self.source / "asset.bin").write_bytes(binary)
        with mock.patch("forge8.repository.MAX_REPOSITORY_FILES", 1):
            self.assert_rejected("file limit")
        limit = self.code.stat().st_size + len(binary) - 1
        with mock.patch("forge8.repository.MAX_REPOSITORY_BYTES", limit):
            self.assert_rejected("total limit")

    def test_source_binary_to_text_transition_during_copy_fails_closed(self) -> None:
        asset = self.source / "asset.dat"
        asset.write_bytes(b"\x00binary")
        write_tree = repository._write_scanned_tree

        def mutate_after_copy(scan, destination):
            write_tree(scan, destination)
            asset.write_bytes(b"now readable source\n")

        with mock.patch("forge8.repository._write_scanned_tree", side_effect=mutate_after_copy):
            self.assert_rejected("source changed")

    def test_source_text_to_binary_transition_during_copy_fails_closed(self) -> None:
        write_tree = repository._write_scanned_tree

        def mutate_after_copy(scan, destination):
            write_tree(scan, destination)
            self.code.write_bytes(b"\x00changed")

        with mock.patch("forge8.repository._write_scanned_tree", side_effect=mutate_after_copy):
            self.assert_rejected("source changed")

    def test_exclusion_membership_drift_during_copy_is_detected(self) -> None:
        write_tree = repository._write_scanned_tree

        def mutate_after_copy(scan, destination):
            write_tree(scan, destination)
            (self.source / ".vscode").mkdir()

        with mock.patch("forge8.repository._write_scanned_tree", side_effect=mutate_after_copy):
            self.assert_rejected("source changed")

    def test_copied_snapshot_is_strict_even_when_source_ingress_is_filtered(self) -> None:
        write_tree = repository._write_scanned_tree

        def inject_after_copy(scan, destination):
            write_tree(scan, destination)
            (destination / "injected.bin").write_bytes(b"\x00")

        with mock.patch("forge8.repository._write_scanned_tree", side_effect=inject_after_copy):
            self.assert_rejected("NUL")

    def test_excluded_contents_are_outside_fingerprinted_scope(self) -> None:
        asset = self.source / "asset.bin"
        asset.write_bytes(b"\x00before")
        write_tree = repository._write_scanned_tree

        def mutate_after_copy(scan, destination):
            write_tree(scan, destination)
            asset.write_bytes(b"\x00after")

        with mock.patch("forge8.repository._write_scanned_tree", side_effect=mutate_after_copy):
            result = self.prepare()

        self.assertEqual(result.excluded, ("asset.bin",))
        self.assertEqual([item.path for item in result.fingerprints], ["app.py"])
        self.assertFalse((self.destination / "asset.bin").exists())


if __name__ == "__main__":
    unittest.main()
