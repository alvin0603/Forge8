from __future__ import annotations

import errno
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import forge8.repository as repository
from forge8.repository import (
    MAX_REPOSITORY_BYTES,
    MAX_REPOSITORY_FILE_BYTES,
    MAX_REPOSITORY_FILES,
    RepositoryError,
    prepare_repository_snapshot,
    validate_allowed_write_paths,
)


class RepositorySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-repository-test-")
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        (self.source / "src").mkdir(parents=True)
        (self.source / "tests").mkdir()
        (self.source / "src" / "app.py").write_text(
            "def answer():\n    return 41\n", encoding="utf-8", newline=""
        )
        (self.source / "tests" / "test_app.py").write_text(
            "import unittest\n\nclass AppTests(unittest.TestCase):\n    pass\n",
            encoding="utf-8",
            newline="",
        )
        self.destination = self.base / "snapshot"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_rejected(self, pattern: str, destination: Path | None = None) -> None:
        target = self.destination if destination is None else destination
        with self.assertRaisesRegex(RepositoryError, pattern):
            prepare_repository_snapshot(self.source, target)
        self.assertFalse(os.path.lexists(target))

    def test_snapshot_copies_exact_text_inventory_without_mutating_source(self) -> None:
        app = self.source / "src" / "app.py"
        before = app.read_bytes()

        result = prepare_repository_snapshot(self.source, self.destination)

        self.assertEqual(result.source_root, str(self.source.resolve()))
        self.assertEqual(result.snapshot_root, str(self.destination.resolve()))
        self.assertEqual(result.excluded, ())
        inventory = {item.path: item for item in result.fingerprints}
        self.assertEqual(set(inventory), {"src/app.py", "tests/test_app.py"})
        self.assertEqual(inventory["src/app.py"].size_bytes, len(before))
        self.assertEqual(inventory["src/app.py"].sha256, hashlib.sha256(before).hexdigest())
        self.assertEqual((self.destination / "src" / "app.py").read_bytes(), before)
        self.assertEqual(app.read_bytes(), before)
        (self.destination / "src" / "app.py").write_text("changed\n", encoding="utf-8")
        self.assertEqual(app.read_bytes(), before)
        self.assertEqual(result.as_dict()["fingerprints"][0]["path"], "src/app.py")

    def test_snapshot_preserves_empty_included_directories(self) -> None:
        (self.source / "docs" / "empty").mkdir(parents=True)

        prepare_repository_snapshot(self.source, self.destination)

        self.assertTrue((self.destination / "docs" / "empty").is_dir())

    def test_reading_excludes_exact_lint_and_docs_configs_without_reading_them(self) -> None:
        for index, name in enumerate((".markdownlint.yaml", ".readthedocs.yml", "tests/.coveragerc")):
            with self.subTest(name=name):
                path = self.source / name
                path.write_bytes(b"not admitted or decoded: \xff\x00\n")
                with mock.patch.object(repository, "_read_regular_file", wraps=repository._read_regular_file) as read:
                    result = prepare_repository_snapshot(self.source, self.base / f"reading-{index}", for_explanation=True)
                self.assertEqual(result.excluded, (name,))
                self.assertNotIn(name, {item.path for item in result.fingerprints})
                self.assertNotIn(name, [call.args[2] for call in read.call_args_list])
                self.assertFalse((Path(result.snapshot_root) / name).exists())
                self.assert_rejected("hidden path", self.base / f"repair-{index}")
                path.unlink()

    def test_reading_config_exclusions_do_not_admit_other_hidden_names_or_directories(self) -> None:
        cases = ((".markdownlint.yaml", True, "hidden"), (".readthedocs.yml", True, "hidden"),
            (".coveragerc", True, "hidden"), (".coveragerc.secret", False, "hidden"),
            (".markdownlint.yml", False, "hidden"), (".readthedocs.secret", False, "hidden"),
            (".env", False, "credential-like"))
        for index, (name, directory, error) in enumerate(cases):
            with self.subTest(name=name):
                path = self.source / name
                if directory:
                    path.mkdir()
                else:
                    path.write_text("ordinary fixture\n", encoding="utf-8")
                destination = self.base / f"invalid-config-{index}"
                with self.assertRaisesRegex(RepositoryError, error):
                    prepare_repository_snapshot(self.source, destination, for_explanation=True)
                self.assertFalse(destination.exists())
                path.rmdir() if directory else path.unlink()

    def test_reading_config_names_do_not_bypass_link_checks(self) -> None:
        for index, name in enumerate((".markdownlint.yaml", ".readthedocs.yml", "tests/.coveragerc")):
            for kind in ("hardlink", "symlink"):
                with self.subTest(name=name, kind=kind):
                    path = self.source / name
                    try:
                        os.link(self.source / "src" / "app.py", path) if kind == "hardlink" else path.symlink_to(self.source / "src" / "app.py")
                    except (OSError, NotImplementedError) as exc:
                        self.skipTest(f"{kind} unavailable: {exc}")
                    with self.assertRaisesRegex(RepositoryError, "hard-linked|symlinks and reparse"):
                        prepare_repository_snapshot(self.source, self.base / f"linked-config-{index}-{kind}", for_explanation=True)
                    path.unlink()

    def test_explicit_build_and_cache_paths_and_pyc_files_are_excluded(self) -> None:
        names = (
            ".git",
            ".forge8",
            ".venv",
            "venv",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "node_modules",
            "dist",
            "build",
        )
        for name in names:
            directory = self.source / name
            directory.mkdir()
            (directory / "ignored.txt").write_text("ignored\n", encoding="utf-8")
        (self.source / "src" / "cached.pyc").write_bytes(b"\x00binary is excluded")

        result = prepare_repository_snapshot(self.source, self.destination)

        self.assertEqual(set(result.excluded), {*names, "src/cached.pyc"})
        for path in result.excluded:
            self.assertFalse((self.destination / path).exists())

    def test_destination_must_not_exist_even_as_a_dangling_link(self) -> None:
        self.destination.mkdir()
        with self.assertRaisesRegex(RepositoryError, "already exists"):
            prepare_repository_snapshot(self.source, self.destination)
        self.assertTrue(self.destination.is_dir())

        self.destination.rmdir()
        try:
            self.destination.symlink_to(self.base / "missing", target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaisesRegex(RepositoryError, "already exists"):
            prepare_repository_snapshot(self.source, self.destination)
        self.assertTrue(self.destination.is_symlink())

    def test_source_must_be_a_non_root_directory(self) -> None:
        source_file = self.base / "source.py"
        source_file.write_text("pass\n", encoding="utf-8")
        with self.assertRaisesRegex(RepositoryError, "not a directory"):
            prepare_repository_snapshot(source_file, self.destination)
        with self.assertRaisesRegex(RepositoryError, "filesystem root"):
            prepare_repository_snapshot(Path(self.base.anchor), self.destination)

    def test_destination_parent_must_exist_and_source_must_not_overlap_it(self) -> None:
        missing_parent = self.base / "missing" / "snapshot"
        self.assert_rejected("cannot inspect snapshot destination parent", missing_parent)

        nested = self.source / "generated-snapshot"
        self.assert_rejected("must not overlap", nested)

    def test_regular_gitignore_is_fingerprinted_and_copied_exactly(self) -> None:
        root_content = b"*.pyc\n.venv/\n"
        nested_content = b"generated/\n"
        (self.source / ".gitignore").write_bytes(root_content)
        (self.source / "src" / ".gitignore").write_bytes(nested_content)

        result = prepare_repository_snapshot(self.source, self.destination)

        inventory = {item.path: item for item in result.fingerprints}
        self.assertIn(".gitignore", inventory)
        self.assertIn("src/.gitignore", inventory)
        self.assertEqual(inventory[".gitignore"].size_bytes, len(root_content))
        self.assertEqual(
            inventory[".gitignore"].sha256,
            hashlib.sha256(root_content).hexdigest(),
        )
        self.assertEqual((self.destination / ".gitignore").read_bytes(), root_content)
        self.assertEqual(
            (self.destination / "src" / ".gitignore").read_bytes(),
            nested_content,
        )
        self.assertEqual((self.source / ".gitignore").read_bytes(), root_content)

    def test_other_hidden_paths_and_gitignore_directory_are_rejected(self) -> None:
        cases = (
            (".hidden", False),
            (".GITIGNORE", False),
            (".github", True),
            (".gitignore", True),
        )
        for index, (name, is_directory) in enumerate(cases):
            with self.subTest(name=name):
                path = self.source / name
                if is_directory:
                    path.mkdir()
                else:
                    path.write_text("hidden\n", encoding="utf-8")
                self.assert_rejected(
                    "hidden path",
                    self.base / f"snapshot-hidden-{index}",
                )
                if is_directory:
                    path.rmdir()
                else:
                    path.unlink()

    def test_gitignore_exception_still_requires_bounded_utf8_text(self) -> None:
        cases = (
            (b"\xff\xfe", "valid UTF-8"),
            (b"before\x00after", "NUL byte"),
            (b"before\x01after", "binary control"),
            (b"a" * (MAX_REPOSITORY_FILE_BYTES + 1), "file exceeds"),
        )
        for index, (content, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                path = self.source / ".gitignore"
                path.write_bytes(content)
                self.assert_rejected(
                    expected,
                    self.base / f"snapshot-gitignore-text-{index}",
                )
                path.unlink()

    def test_credential_like_paths_are_rejected(self) -> None:
        cases = {
            ".env": "secret",
            ".env.production": "secret",
            ".ssh": None,
            "credentials.json": "{}",
            "signing.pem": "secret",
        }
        for index, (name, content) in enumerate(cases.items()):
            with self.subTest(name=name):
                path = self.source / name
                if content is None:
                    path.mkdir()
                else:
                    path.write_text(content, encoding="utf-8")
                self.assert_rejected("credential-like", self.base / f"snapshot-{index}")
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()

    def test_symlink_is_rejected_without_following_it(self) -> None:
        for index, name in enumerate(("linked.py", ".gitignore")):
            with self.subTest(name=name):
                link = self.source / name
                try:
                    link.symlink_to(self.source / "src" / "app.py")
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"symlinks unavailable: {exc}")
                self.assert_rejected(
                    "symlinks and reparse",
                    self.base / f"snapshot-symlink-{index}",
                )
                link.unlink()

    def test_reparse_attribute_is_rejected(self) -> None:
        flag = getattr(repository.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        self.assertTrue(repository._is_reparse_point(SimpleNamespace(st_file_attributes=flag)))

        target = self.source / ".gitignore"
        target.write_text("*.pyc\n", encoding="utf-8")
        target_inode = target.stat().st_ino
        original = repository._is_reparse_point

        def simulated(metadata: os.stat_result) -> bool:
            return metadata.st_ino == target_inode or original(metadata)

        with mock.patch("forge8.repository._is_reparse_point", side_effect=simulated):
            self.assert_rejected("reparse")

    def test_hard_link_is_rejected(self) -> None:
        for index, name in enumerate(("alias.py", ".gitignore")):
            with self.subTest(name=name):
                link = self.source / name
                try:
                    os.link(self.source / "src" / "app.py", link)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"hard links unavailable: {exc}")
                self.assert_rejected(
                    "hard-linked",
                    self.base / f"snapshot-hardlink-{index}",
                )
                link.unlink()

    @unittest.skipIf(os.name == "nt", "POSIX FIFO test")
    def test_special_file_is_rejected(self) -> None:
        os.mkfifo(self.source / "events")
        self.assert_rejected("special files")

    def test_invalid_utf8_nul_and_binary_control_data_are_rejected(self) -> None:
        cases = {
            "invalid.py": b"\xff\xfe",
            "has_nul.py": b"before\x00after",
            "control.py": b"before\x01after",
        }
        patterns = {
            "invalid.py": "valid UTF-8",
            "has_nul.py": "NUL byte",
            "control.py": "binary control",
        }
        for index, (name, content) in enumerate(cases.items()):
            with self.subTest(name=name):
                path = self.source / "src" / name
                path.write_bytes(content)
                self.assert_rejected(patterns[name], self.base / f"snapshot-{index}")
                path.unlink()

    def test_exact_per_file_limit_is_allowed_and_one_byte_more_is_rejected(self) -> None:
        large = self.source / "src" / "large.txt"
        large.write_bytes(b"a" * MAX_REPOSITORY_FILE_BYTES)
        prepare_repository_snapshot(self.source, self.destination)
        self.assertEqual((self.destination / "src" / "large.txt").stat().st_size, MAX_REPOSITORY_FILE_BYTES)

        self.destination.rename(self.base / "accepted")
        large.write_bytes(b"a" * (MAX_REPOSITORY_FILE_BYTES + 1))
        self.assert_rejected("file exceeds")

    def test_file_count_budget_is_enforced(self) -> None:
        # Replace the ordinary fixture with exactly one more file than admitted.
        for path in sorted(self.source.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.source.mkdir(exist_ok=True)
        for index in range(MAX_REPOSITORY_FILES + 1):
            (self.source / f"f{index:04}.py").write_text("\n", encoding="utf-8")
        self.assert_rejected("file limit")

    def test_total_byte_budget_is_enforced(self) -> None:
        payload = b"a" * MAX_REPOSITORY_FILE_BYTES
        count = MAX_REPOSITORY_BYTES // MAX_REPOSITORY_FILE_BYTES + 1
        for index in range(count):
            (self.source / "src" / f"block{index:02}.txt").write_bytes(payload)
        self.assert_rejected("total limit")

    def test_source_change_during_copy_fails_without_partial_destination(self) -> None:
        original = repository._write_scanned_tree

        def write_then_mutate(scan: object, destination: Path) -> None:
            original(scan, destination)  # type: ignore[arg-type]
            (self.source / "src" / "app.py").write_text("changed during copy\n", encoding="utf-8")

        with mock.patch("forge8.repository._write_scanned_tree", side_effect=write_then_mutate):
            self.assert_rejected("source changed")
        self.assertEqual(list(self.base.glob(".snapshot.forge8-partial-*")), [])

    def test_empty_directory_change_during_copy_is_also_a_source_race(self) -> None:
        original = repository._write_scanned_tree

        def write_then_add_directory(scan: object, destination: Path) -> None:
            original(scan, destination)  # type: ignore[arg-type]
            (self.source / "new-empty-directory").mkdir()

        with mock.patch(
            "forge8.repository._write_scanned_tree", side_effect=write_then_add_directory
        ):
            self.assert_rejected("source changed")
        self.assertEqual(list(self.base.glob(".snapshot.forge8-partial-*")), [])

    def test_destination_appearing_during_copy_is_preserved_not_cleaned(self) -> None:
        original = repository._write_scanned_tree

        def write_then_claim_destination(scan: object, destination: Path) -> None:
            original(scan, destination)  # type: ignore[arg-type]
            self.destination.mkdir()
            (self.destination / "owner.txt").write_text("external owner\n", encoding="utf-8")

        with mock.patch(
            "forge8.repository._write_scanned_tree", side_effect=write_then_claim_destination
        ), self.assertRaisesRegex(RepositoryError, "appeared during creation"):
            prepare_repository_snapshot(self.source, self.destination)
        self.assertEqual(
            (self.destination / "owner.txt").read_text(encoding="utf-8"),
            "external owner\n",
        )
        self.assertEqual(list(self.base.glob(".snapshot.forge8-partial-*")), [])

    def test_empty_destination_racing_at_atomic_publish_is_not_replaced(self) -> None:
        publish = repository._publish_directory_noreplace

        def race_with_empty_directory(source: Path, destination: Path) -> None:
            destination.mkdir()
            publish(source, destination)

        with mock.patch(
            "forge8.repository._publish_directory_noreplace",
            side_effect=race_with_empty_directory,
        ), self.assertRaisesRegex(RepositoryError, "destination appeared"):
            prepare_repository_snapshot(self.source, self.destination)

        self.assertTrue(self.destination.is_dir())
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(list(self.base.glob(".snapshot.forge8-partial-*")), [])

    def test_unsupported_linux_filesystem_explains_native_state_without_rename_fallback(self) -> None:
        renameat2 = mock.Mock(return_value=-1)
        with (
            mock.patch.object(repository.os, "name", "posix"),
            mock.patch.object(repository.sys, "platform", "linux"),
            mock.patch.object(repository.ctypes, "CDLL", return_value=SimpleNamespace(renameat2=renameat2)),
            mock.patch.object(repository.ctypes, "get_errno", return_value=errno.EOPNOTSUPP),
            mock.patch.object(repository.os, "rename") as rename,
            mock.patch.object(repository.os, "replace") as replace,
            self.assertRaisesRegex(RepositoryError, "FORGE8_STATE_HOME.*native Linux") as raised,
        ):
            repository._publish_directory_noreplace(self.source, self.destination)
        self.assertIn("not /mnt/c", str(raised.exception))
        renameat2.assert_called_once()
        rename.assert_not_called()
        replace.assert_not_called()
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.destination.exists())

    @unittest.skipUnless(os.name == "nt", "native Windows publication retry test")
    def test_windows_transient_access_denied_retries_and_publishes(self) -> None:
        real_rename = os.rename
        first = PermissionError(13, "first access denied")
        first.winerror = 5  # type: ignore[attr-defined]
        second = PermissionError(13, "second access denied")
        second.winerror = 5  # type: ignore[attr-defined]
        failures = iter((first, second))

        def fail_twice_then_publish(source: Path, destination: Path) -> None:
            try:
                failure = next(failures)
            except StopIteration:
                real_rename(source, destination)
                return
            raise failure

        with mock.patch(
            "forge8.repository.os.rename", side_effect=fail_twice_then_publish
        ) as rename, mock.patch("time.sleep") as sleep:
            result = prepare_repository_snapshot(self.source, self.destination)

        self.assertEqual(rename.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.05, 0.10])
        self.assertEqual(
            {item.path: item.sha256 for item in result.fingerprints},
            {
                "src/app.py": hashlib.sha256(
                    (self.source / "src" / "app.py").read_bytes()
                ).hexdigest(),
                "tests/test_app.py": hashlib.sha256(
                    (self.source / "tests" / "test_app.py").read_bytes()
                ).hexdigest(),
            },
        )
        self.assertEqual(
            (self.destination / "src" / "app.py").read_bytes(),
            (self.source / "src" / "app.py").read_bytes(),
        )
        self.assertEqual(list(self.base.glob(".snapshot.forge8-partial-*")), [])

    @unittest.skipUnless(os.name == "nt", "native Windows publication retry test")
    def test_windows_retry_stops_when_destination_appears_during_backoff(self) -> None:
        denied = PermissionError(13, "access denied")
        denied.winerror = 5  # type: ignore[attr-defined]

        def claim_destination(_delay: float) -> None:
            self.destination.mkdir()
            (self.destination / "owner.txt").write_bytes(b"external owner\n")

        with mock.patch("forge8.repository.os.rename", side_effect=denied) as rename, mock.patch(
            "time.sleep", side_effect=claim_destination
        ) as sleep:
            with self.assertRaisesRegex(RepositoryError, "destination appeared"):
                prepare_repository_snapshot(self.source, self.destination)

        self.assertEqual(rename.call_count, 1)
        sleep.assert_called_once_with(0.05)
        self.assertEqual(
            (self.destination / "owner.txt").read_bytes(),
            b"external owner\n",
        )
        self.assertEqual(list(self.base.glob(".snapshot.forge8-partial-*")), [])

    @unittest.skipUnless(os.name == "nt", "native Windows publication retry test")
    def test_windows_persistent_access_denied_is_bounded_and_reports_last_error(self) -> None:
        failures = []
        for label in ("first", "second", "third"):
            failure = PermissionError(13, label)
            failure.winerror = 5  # type: ignore[attr-defined]
            failures.append(failure)

        with mock.patch("forge8.repository.os.rename", side_effect=failures) as rename, mock.patch(
            "time.sleep"
        ) as sleep:
            with self.assertRaisesRegex(RepositoryError, "cannot publish") as caught:
                prepare_repository_snapshot(self.source, self.destination)

        self.assertEqual(rename.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.05, 0.10])
        self.assertIs(caught.exception.__cause__, failures[-1])
        self.assertFalse(os.path.lexists(self.destination))
        self.assertEqual(list(self.base.glob(".snapshot.forge8-partial-*")), [])

    @unittest.skipUnless(os.name == "nt", "native Windows publication retry test")
    def test_windows_non_access_denied_is_never_retried(self) -> None:
        sharing_violation = PermissionError(13, "sharing violation")
        sharing_violation.winerror = 32  # type: ignore[attr-defined]

        with mock.patch(
            "forge8.repository.os.rename", side_effect=sharing_violation
        ) as rename, mock.patch("time.sleep") as sleep:
            with self.assertRaisesRegex(RepositoryError, "cannot publish") as caught:
                prepare_repository_snapshot(self.source, self.destination)

        self.assertEqual(rename.call_count, 1)
        sleep.assert_not_called()
        self.assertIs(caught.exception.__cause__, sharing_violation)
        self.assertFalse(os.path.lexists(self.destination))
        self.assertEqual(list(self.base.glob(".snapshot.forge8-partial-*")), [])

    def test_portable_component_rejects_header_injection_and_windows_aliases(self) -> None:
        invalid = (
            "bad\nname.py",
            "bad\rname.py",
            "bad\x01name.py",
            "bad?.py",
            "bad*.py",
            'bad"name.py',
            "bad<name.py",
            "bad>name.py",
            "bad|name.py",
            "COM¹.txt",
            "LPT³.log",
        )
        for name in invalid:
            with self.subTest(name=name):
                with self.assertRaisesRegex(RepositoryError, "(non-portable|reserved)"):
                    repository._validate_portable_component(name, name)


class AllowedWritePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-allowed-path-test-")
        self.base = Path(self.temporary.name)
        source = self.base / "source"
        (source / "src" / "pkg").mkdir(parents=True)
        (source / "docs").mkdir()
        (source / "tests").mkdir()
        (source / "src" / "app.py").write_text("pass\n", encoding="utf-8")
        (source / "src" / "pkg" / "helper.py").write_text("pass\n", encoding="utf-8")
        (source / "docs" / "README.md").write_text("docs\n", encoding="utf-8")
        (source / "tests" / "test_app.py").write_text("pass\n", encoding="utf-8")
        self.snapshot = self.base / "snapshot"
        prepare_repository_snapshot(source, self.snapshot)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_accepts_existing_non_overlapping_files_and_directories(self) -> None:
        result = validate_allowed_write_paths(
            self.snapshot, ["src/app.py", "docs"]
        )
        self.assertEqual(result, ("src/app.py", "docs"))

    def test_canonicalizes_each_component_to_snapshot_casing(self) -> None:
        self.assertEqual(
            validate_allowed_write_paths(self.snapshot, ["SRC/PKG/HELPER.PY"]),
            ("src/pkg/helper.py",),
        )

    def test_requires_a_nonempty_iterable_not_one_string(self) -> None:
        for paths in ([], (), "src/app.py", b"src/app.py"):
            with self.subTest(paths=paths):
                with self.assertRaises(RepositoryError):
                    validate_allowed_write_paths(self.snapshot, paths)  # type: ignore[arg-type]

    def test_rejects_non_normalized_absolute_and_non_string_paths(self) -> None:
        invalid = (
            "",
            ".",
            "../src/app.py",
            "src/../app.py",
            "src//app.py",
            "src/app.py/",
            "/src/app.py",
            "C:/src/app.py",
            "src\\app.py",
            "src/app.py\x00",
            7,
        )
        for path in invalid:
            with self.subTest(path=path):
                with self.assertRaises(RepositoryError):
                    validate_allowed_write_paths(self.snapshot, [path])  # type: ignore[list-item]

    def test_rejects_missing_paths(self) -> None:
        with self.assertRaisesRegex(RepositoryError, "cannot inspect allowed write path"):
            validate_allowed_write_paths(self.snapshot, ["src/missing.py"])

    def test_tests_are_immutable_at_any_path_depth(self) -> None:
        for path in ("tests", "tests/test_app.py"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(RepositoryError, "test paths"):
                    validate_allowed_write_paths(self.snapshot, [path])

        nested = self.snapshot / "src" / "pkg" / "tests"
        nested.mkdir()
        (nested / "test_helper.py").write_text("pass\n", encoding="utf-8")
        with self.assertRaisesRegex(RepositoryError, "test paths"):
            validate_allowed_write_paths(self.snapshot, ["src"])

    def test_hidden_and_credential_like_paths_are_rejected_lexically(self) -> None:
        for path in (
            ".gitignore",
            "src/.gitignore",
            ".secret",
            "src/.secret",
            ".env",
            "src/signing.key",
        ):
            with self.subTest(path=path):
                with self.assertRaises(RepositoryError):
                    validate_allowed_write_paths(self.snapshot, [path])

    def test_directory_allowlist_cannot_encompass_gitignore(self) -> None:
        (self.snapshot / "src" / ".gitignore").write_text(
            "generated/\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RepositoryError, "hidden allowed write path"):
            validate_allowed_write_paths(self.snapshot, ["src"])

    def test_duplicate_parent_child_and_casefold_overlap_are_rejected(self) -> None:
        cases = (
            ["src/app.py", "src/app.py"],
            ["src", "src/app.py"],
            ["src/pkg/helper.py", "src/pkg"],
            ["src/app.py", "SRC/APP.PY"],
        )
        for paths in cases:
            with self.subTest(paths=paths):
                with self.assertRaisesRegex(RepositoryError, "overlap or duplicate"):
                    validate_allowed_write_paths(self.snapshot, paths)

    def test_mutated_snapshot_link_and_hardlink_are_rejected(self) -> None:
        link = self.snapshot / "linked.py"
        try:
            link.symlink_to(self.snapshot / "src" / "app.py")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"links unavailable: {exc}")
        with self.assertRaisesRegex(RepositoryError, "symlink or reparse"):
            validate_allowed_write_paths(self.snapshot, ["linked.py"])
        link.unlink()

        alias = self.snapshot / "alias.py"
        try:
            os.link(self.snapshot / "src" / "app.py", alias)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        with self.assertRaisesRegex(RepositoryError, "hard-linked"):
            validate_allowed_write_paths(self.snapshot, ["alias.py"])


if __name__ == "__main__":
    unittest.main()
