"""Owned temporary repositories only; no foreign source or configured helpers run."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from forge8 import git_source
from forge8.repository import RepositoryError


def _blob(content: bytes, algorithm: str = "sha1") -> bytes:
    return hashlib.new(algorithm, b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest().encode()


def _tree_and_batch(files: dict[str, bytes], algorithm: str = "sha1") -> tuple[bytes, bytes]:
    tree, batch = [], []
    for name, content in files.items():
        oid = _blob(content, algorithm)
        size = str(len(content)).encode()
        tree.append(b"100644 blob " + oid + b" " + size + b"\t" + name.encode() + b"\0")
        batch.append(oid + b" blob " + size + b"\n" + content + b"\n")
    return b"".join(tree), b"".join(batch)


class GitSourceOwnedRepositoryTests(unittest.TestCase):
    """Git writes occur only while constructing our own tiny fixture."""

    def setUp(self) -> None:
        located = shutil.which("git")
        if located is None:
            self.skipTest("native Git is unavailable")
        self.binary = Path(located).resolve()
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-git-source-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "owned-source"
        self.source.mkdir()
        self.scratch = self.base / "owned-reader"
        self.scratch.mkdir()
        self.templates = self.base / "empty-templates"
        self.templates.mkdir()
        self.hooks = self.base / "disabled-setup-hooks"
        self.hooks.mkdir()
        self.empty = self.base / "empty-config"
        self.empty.write_bytes(b"")
        self.env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH") if key in os.environ}
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=str(self.empty),
            GIT_CONFIG_GLOBAL=str(self.empty), GIT_CONFIG_COUNT="0",
            GIT_CEILING_DIRECTORIES=str(self.base),
            GIT_ALLOW_PROTOCOL="", GIT_TERMINAL_PROMPT="0", GIT_ATTR_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Owned Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Owned Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
            GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00", GIT_COMMITTER_DATE="2026-01-01T00:00:00+00:00",
            LC_ALL="C", LANG="C", TMP=str(self.base), TEMP=str(self.base),
        )
        self._git("init", "--quiet", "--initial-branch=main", "--object-format=sha1",
                  "--template=" + str(self.templates))
        (self.source / ".git/info").mkdir(exist_ok=True)
        self.original = b"def answer():\r\n    return 41\r\n"
        (self.source / "app.py").write_bytes(self.original)
        self.commit = self._commit()

    def _git(self, *args: str, data: bytes | None = None) -> bytes:
        result = subprocess.run(
            [str(self.binary), "--no-pager", "--no-replace-objects",
             "-c", "core.hooksPath=" + str(self.hooks), "-c", "core.fsmonitor=false",
             "-c", "core.autocrlf=false", "-c", "commit.gpgSign=false",
             "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args],
            cwd=self.source, env=self.env, input=data, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10, check=False, shell=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return result.stdout

    def _commit(self) -> str:
        self._git("add", "--all")
        self._git("commit", "--quiet", "--no-gpg-sign", "-m", "Owned fixture")
        return self._git("rev-parse", "--verify", "HEAD").decode().strip()

    def _read(self) -> git_source.GitBaseline:
        try:
            return git_source.read_head(self.source, self.scratch)
        finally:
            self.assertEqual(list(self.scratch.iterdir()), [], "reader scratch was not cleaned")

    def test_raw_head_does_not_read_index_or_change_saved_files(self) -> None:
        app = self.source / "app.py"
        app.write_bytes(b"def answer():\n    return 42\n")
        (self.source / "staged.py").write_bytes(b"staged = True\n")
        self._git("add", "--", "app.py", "staged.py")
        app.write_bytes(b"def answer():\n    return 43\n")
        (self.source / "saved.py").write_bytes(b"saved = True\n")
        paths = (app, self.source / "staged.py", self.source / "saved.py", self.source / ".git/index")
        before = {path: (path.read_bytes(), path.stat()) for path in paths}
        original_run = git_source._GitReader._run
        with mock.patch.object(git_source._GitReader, "_run", autospec=True, side_effect=original_run) as run:
            result = self._read()
        self.assertEqual(result.commit, self.commit)
        self.assertEqual(result.files, {"app.py": self.original})
        self.assertEqual(result.excluded, ())
        for path, (content, metadata) in before.items():
            self.assertEqual(path.read_bytes(), content)
            after = path.stat()
            self.assertEqual((after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                             (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns))
        families = [call.args[1][0] for call in run.call_args_list]
        self.assertEqual(set(families), {"--version", "config", "rev-parse", "ls-tree", "cat-file"})

    def test_an_absent_index_is_not_recreated(self) -> None:
        index = self.source / ".git/index"
        index.unlink()  # Only our owned fixture; production must never touch it.
        self.assertEqual(self._read().files, {"app.py": self.original})
        self.assertFalse(index.exists())

    def test_head_identity_checks_metadata_without_reading_tree_or_blobs(self) -> None:
        original_run = git_source._GitReader._run
        with mock.patch.object(git_source._GitReader, "_run", autospec=True, side_effect=original_run) as run:
            self.assertEqual(git_source.head_identity(self.source, self.scratch), self.commit)
        self.assertEqual({call.args[1][0] for call in run.call_args_list},
                         {"--version", "config", "rev-parse"})
        (self.source / "app.py").write_bytes(b"new_saved_value = 1\n")
        self.assertEqual(git_source.head_identity(self.source, self.scratch), self.commit)
        new_commit = self._commit()
        self.assertNotEqual(new_commit, self.commit)
        self.assertEqual(git_source.head_identity(self.source, self.scratch), new_commit)

    def test_native_sha256_repository_retains_matching_blob_bytes(self) -> None:
        self.source = self.base / "owned-sha256-source"
        self.source.mkdir()
        self._git("init", "--quiet", "--initial-branch=main", "--object-format=sha256",
                  "--template=" + str(self.templates))
        (self.source / "app.py").write_bytes(self.original)
        commit = self._commit()
        result = self._read()
        self.assertEqual(len(commit), 64)
        self.assertEqual(result.commit, commit)
        self.assertEqual(result.files, {"app.py": self.original})

    def test_source_is_read_as_bytes_even_if_importing_it_would_fail(self) -> None:
        content = b"raise RuntimeError('owned fixture must never execute')\n"
        (self.source / "app.py").write_bytes(content)
        self._commit()
        self.assertEqual(self._read().files["app.py"], content)

    def test_local_filters_textconv_fsmonitor_and_hooks_are_not_invoked(self) -> None:
        marker = self.base / "helper-was-executed"
        executable = self.base / "owned-marker-hook"
        executable.write_text("#!/bin/sh\nprintf touched > " + shlex.quote(marker.as_posix()) + "\nexit 91\n",
                              encoding="utf-8", newline="")
        executable.chmod(0o755)
        active_hooks = self.base / "active-owned-hooks"
        active_hooks.mkdir()
        for name in ("pre-commit", "post-checkout", "post-merge", "reference-transaction"):
            target = active_hooks / name
            target.write_bytes(executable.read_bytes())
            target.chmod(0o755)
        command = shlex.quote(executable.as_posix())
        settings = {
            "core.hooksPath": str(active_hooks), "core.fsmonitor": command,
            "filter.fixture.clean": command, "filter.fixture.smudge": command,
            "filter.fixture.process": command, "filter.fixture.required": "true",
            "diff.external": command, "diff.fixture.textconv": command,
        }
        for key, value in settings.items():
            self._git("config", "--local", key, value)
        (self.source / ".git/info/attributes").write_bytes(b"app.py filter=fixture diff=fixture\n")
        self.assertEqual(self._read().files["app.py"], self.original)
        self.assertFalse(marker.exists())

    def test_missing_blob_fails_locally_without_requesting_fetch(self) -> None:
        oid = self._git("rev-parse", "HEAD:app.py").decode().strip()
        blob = self.source / ".git/objects" / oid[:2] / oid[2:]
        blob.chmod(0o600)  # Git-created owned objects are read-only on Windows.
        blob.unlink()
        original_run = git_source._GitReader._run
        with mock.patch.object(git_source._GitReader, "_run", autospec=True, side_effect=original_run) as run:
            with self.assertRaises(git_source.GitSourceError):
                self._read()
        self.assertNotIn("fetch", [call.args[1][0] for call in run.call_args_list])

    def test_partial_clone_keys_refuse_before_any_object_operation(self) -> None:
        config = self.source / ".git/config"
        original = config.read_bytes()
        for key, value in (
            ("remote.origin.partialclonefilter", "blob:none"),
            ("remote.origin.with.dots.partialclonefilter", "blob:none"),
            ("remote.origin.promisor", "false"),
            ("extensions.partialclone", "origin"),
            ("extensions.worktreeconfig", "false"),
            ("core.worktree", str(self.source)),
        ):
            with self.subTest(key=key):
                config.write_bytes(original)
                self._git("config", "--local", key, value)
                original_run = git_source._GitReader._run
                with mock.patch.object(git_source._GitReader, "_run", autospec=True, side_effect=original_run) as run:
                    with self.assertRaisesRegex(git_source.GitSourceError, "unsupported indirection or partial"):
                        self._read()
                self.assertTrue(run.call_args_list)
                self.assertTrue(all(call.kwargs.get("repository") is False for call in run.call_args_list))

    def test_include_is_rejected_without_parsing_its_target(self) -> None:
        target = self.base / "not-a-valid-config"
        target.write_bytes(b"[deliberately malformed configuration\n")
        self._git("config", "--local", "include.path", str(target))
        with self.assertRaisesRegex(git_source.GitSourceError, "unsupported indirection"):
            self._read()

    def test_indirected_metadata_and_promisor_markers_refuse_before_git(self) -> None:
        for relative in ("commondir", "config.worktree", "objects/info/alternates",
                         "objects/info/http-alternates", "info/grafts", "objects/pack/owned.promisor"):
            with self.subTest(relative=relative):
                target = self.source / ".git" / relative
                target.write_bytes(b"owned unsupported fixture\n")
                try:
                    with mock.patch.object(git_source._GitReader, "_run") as run:
                        with self.assertRaises(git_source.GitSourceError):
                            self._read()
                    run.assert_not_called()
                finally:
                    target.unlink()

    def test_gitfile_redirection_is_not_followed(self) -> None:
        metadata = self.source / ".git"
        saved = self.base / "owned-redirected-git"
        metadata.rename(saved)
        metadata.write_text("gitdir: " + str(saved) + "\n", encoding="utf-8")
        with mock.patch.object(git_source._GitReader, "_run") as run:
            with self.assertRaises(git_source.GitSourceError):
                self._read()
        run.assert_not_called()

    def test_symlinked_metadata_is_not_followed(self) -> None:
        metadata = self.source / ".git"
        saved = self.base / "owned-linked-git"
        metadata.rename(saved)
        try:
            metadata.symlink_to(saved, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        with mock.patch.object(git_source._GitReader, "_run") as run:
            with self.assertRaises(git_source.GitSourceError):
                self._read()
        run.assert_not_called()

    def test_inherited_git_environment_is_not_used(self) -> None:
        trace = self.base / "inherited-trace"
        injection = {
            "GIT_DIR": str(self.base / "not-a-repository"),
            "GIT_OBJECT_DIRECTORY": str(self.base / "not-an-object-store"),
            "GIT_CONFIG_PARAMETERS": "'remote.injected.partialclonefilter'='blob:none'",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.injected.promisor",
            "GIT_CONFIG_VALUE_0": "true", "GIT_TRACE": str(trace),
        }
        original_popen = subprocess.Popen
        with mock.patch.dict(os.environ, injection):
            with mock.patch.object(git_source.subprocess, "Popen", wraps=original_popen) as popen:
                self.assertEqual(self._read().files["app.py"], self.original)
        self.assertFalse(trace.exists())
        for call in popen.call_args_list:
            env = call.kwargs["env"]
            for key in injection.keys() - {"GIT_CONFIG_COUNT"}:
                self.assertNotIn(key, env)
            self.assertEqual(env["GIT_CONFIG_COUNT"], "0")
            self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "")
            self.assertEqual(env["GIT_CEILING_DIRECTORIES"], str(self.scratch))
            self.assertIs(call.kwargs["shell"], False)

    def test_metadata_change_is_refused_before_head_read(self) -> None:
        reader = git_source._GitReader(self.source, self.scratch)
        config = self.source / ".git/config"
        config.write_bytes(config.read_bytes() + b"\n# owned concurrent change\n")
        with mock.patch.object(reader, "_run") as run:
            with self.assertRaisesRegex(git_source.GitSourceError, "metadata changed"):
                reader.head()
        run.assert_not_called()

    def test_reader_scratch_inside_source_is_rejected(self) -> None:
        nested_scratch = self.source / "reader-scratch"
        nested_scratch.mkdir()
        with mock.patch.object(git_source._GitReader, "_run") as run:
            with self.assertRaisesRegex(git_source.GitSourceError, "outside the project"):
                git_source.read_head(self.source, nested_scratch)
        run.assert_not_called()

    def test_large_config_refuses_before_git(self) -> None:
        (self.source / ".git/config").write_bytes(b"#" + b"x" * (64 * 1024))
        with mock.patch.object(git_source._GitReader, "_run") as run:
            with self.assertRaisesRegex(git_source.GitSourceError, "configuration exceeds"):
                self._read()
        run.assert_not_called()


class GitSourceFramingTests(unittest.TestCase):
    """Synthetic Git wire bytes: malformed objects never require Git execution."""

    def _read(self, tree: bytes, data: bytes, *, algorithm: str = "sha1", heads: list[str] | None = None):
        reader = mock.Mock()
        commit = "a" * (40 if algorithm == "sha1" else 64)
        reader.head.side_effect = heads if heads is not None else [commit, commit]
        reader._run.side_effect = [tree, data]
        with mock.patch.object(git_source, "_GitReader", return_value=reader):
            result = git_source.read_head(Path("unused-source"), Path("unused-scratch"))
        return result, reader

    def test_both_object_hash_formats_and_crlf_preservation(self) -> None:
        files = {"src/讀取.py": "value = '原始內容'\r\n".encode(), "empty.py": b""}
        for algorithm in ("sha1", "sha256"):
            with self.subTest(algorithm=algorithm):
                tree, batch = _tree_and_batch(files, algorithm)
                result, reader = self._read(tree, batch, algorithm=algorithm)
                self.assertEqual(result.files, files)
                self.assertEqual(len(result.commit), 40 if algorithm == "sha1" else 64)
                self.assertEqual(reader._run.call_args_list[1].args[0], ["cat-file", "--batch"])

    def test_links_gitlinks_and_binary_are_explicitly_excluded(self) -> None:
        tree, batch = _tree_and_batch({"app.py": b"pass\n", "asset.bin": b"\0\xff"})
        tree += b"120000 blob " + b"b" * 40 + b" 8\tlink.py\0"
        tree += b"160000 commit " + b"c" * 40 + b" -\tdependency\0"
        result, reader = self._read(tree, batch)
        self.assertEqual(result.files, {"app.py": b"pass\n"})
        self.assertEqual(result.excluded, ("asset.bin", "dependency", "link.py"))
        payload = reader._run.call_args_list[1].kwargs["data"]
        self.assertNotIn(b"b" * 40, payload)
        self.assertNotIn(b"c" * 40, payload)

    def test_named_source_exclusions_match_filesystem_admission(self) -> None:
        tree, batch = _tree_and_batch({"app.py": b"pass\n"})
        for name in (".vscode/settings.json", "build/app.py", ".editorconfig", "cached.pyc"):
            record, _ = _tree_and_batch({name: b"excluded text\n"})
            tree += record
        result, _ = self._read(tree, batch)
        self.assertEqual(result.files, {"app.py": b"pass\n"})
        self.assertEqual(set(result.excluded), {".vscode", "build", ".editorconfig", "cached.pyc"})

    def test_nonportable_hidden_and_credential_paths_refuse(self) -> None:
        paths = ("../escape.py", "/absolute.py", "src//app.py", "src/./app.py", "src\\app.py",
                 "src/CON.py", "src/bad\nname.py", ".evil/.gitignore", ".gitignore/app.py", ".env")
        for name in paths:
            with self.subTest(path=name):
                tree, batch = _tree_and_batch({name: b"pass\n"})
                with self.assertRaises(RepositoryError):
                    self._read(tree, batch)

    def test_file_and_directory_case_collisions_refuse(self) -> None:
        for names in (("App.py", "app.py"), ("Pkg/a.py", "pkg/b.py"), ("entry", "entry/file.py")):
            with self.subTest(names=names):
                tree, batch = _tree_and_batch({name: b"pass\n" for name in names})
                with self.assertRaises(git_source.GitSourceError):
                    self._read(tree, batch)

    def test_tree_requires_nul_framing_and_complete_descriptor(self) -> None:
        tree, batch = _tree_and_batch({"app.py": b"pass\n"})
        variants = (tree[:-1], b"\0" + tree, tree.replace(b"100644 blob ", b"100644 ", 1),
                    tree.replace(b" 5\t", b" -1\t", 1), tree.replace(b"100644", b"100600", 1),
                    tree.replace(b"blob", b"tree", 1), tree.replace(_blob(b"pass\n"), b"z" * 40, 1))
        for broken in variants:
            with self.subTest(tree=broken):
                with self.assertRaises(git_source.GitSourceError):
                    self._read(broken, batch)

    def test_blob_framing_identity_length_and_trailing_data_refuse(self) -> None:
        tree, batch = _tree_and_batch({"app.py": b"pass\n"})
        variants = (batch.replace(b" blob ", b" tree ", 1), batch.replace(b"pass", b"fail", 1),
                    batch[:-1], batch[:-2], batch + b"unexpected\n", b"missing\n", b"")
        for broken in variants:
            with self.subTest(batch=broken):
                with self.assertRaises(git_source.GitSourceError):
                    self._read(tree, broken)

    def test_admission_limits_refuse_before_blob_read(self) -> None:
        cases = (
            ("MAX_REPOSITORY_FILE_BYTES", 4, {"app.py": b"pass\n"}),
            ("MAX_REPOSITORY_BYTES", 9, {"a.py": b"pass\n", "b.py": b"pass\n"}),
            ("MAX_REPOSITORY_FILES", 1, {"a.py": b"pass\n", "b.py": b"pass\n"}),
        )
        for constant, limit, files in cases:
            with self.subTest(limit=constant):
                tree, batch = _tree_and_batch(files)
                with mock.patch.object(git_source, constant, limit):
                    with self.assertRaisesRegex(git_source.GitSourceError, "admission limits"):
                        self._read(tree, batch)

    def test_head_movement_does_not_publish_old_baseline(self) -> None:
        tree, batch = _tree_and_batch({"app.py": b"pass\n"})
        with self.assertRaisesRegex(git_source.GitSourceError, "HEAD changed"):
            self._read(tree, batch, heads=["a" * 40, "b" * 40])


class _OwnedFakeProcess:
    def __init__(self, output: bytes, *, running: bool = False, returncode: int = 0):
        self.stdout = io.BytesIO(output)
        self.returncode = None if running else returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("owned fake process", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class GitSourceProcessBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-git-capture-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.reader = object.__new__(git_source._GitReader)
        self.reader.owned_root = self.base
        self.reader.source = self.base / "unused-source"
        self.reader.git = self.reader.source / ".git"
        self.reader.binary = self.base / "never-executed-git"
        self.reader.started = time.monotonic()
        self.reader.modern = False

    def test_stdout_limit_is_enforced_and_scratch_is_removed(self) -> None:
        process = _OwnedFakeProcess(b"over limit")
        with mock.patch.object(git_source.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(git_source.GitSourceError, "bounded reader limits"):
                self.reader._run(["--version"], repository=False, limit=4)
        self.assertTrue(process.stdout.closed)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_failed_git_process_is_not_empty_success(self) -> None:
        process = _OwnedFakeProcess(b"", returncode=128)
        with mock.patch.object(git_source.subprocess, "Popen", return_value=process):
            with self.assertRaises(git_source.GitSourceError):
                self.reader._run(["rev-parse", "HEAD"], limit=128)
        self.assertTrue(process.stdout.closed)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_expired_whole_reader_budget_starts_no_process(self) -> None:
        with mock.patch.object(git_source.time, "monotonic", return_value=self.reader.started + 21):
            with mock.patch.object(git_source.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(git_source.GitSourceError, "20-second"):
                    self.reader._run(["--version"], repository=False, limit=512)
        popen.assert_not_called()
        self.assertEqual(list(self.base.iterdir()), [])

    def test_deadline_terminates_owned_child_and_closes_capture(self) -> None:
        process = _OwnedFakeProcess(b"", running=True)
        ticks = iter(self.reader.started + number for number in range(100))
        with mock.patch.object(git_source.time, "monotonic", side_effect=lambda: next(ticks)):
            with mock.patch.object(git_source.subprocess, "Popen", return_value=process):
                with self.assertRaises(git_source.GitSourceError):
                    self.reader._run(["--version"], repository=False, limit=512)
        self.assertTrue(process.terminated)
        self.assertTrue(process.stdout.closed)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_capture_read_failure_refuses_and_cleans_up(self) -> None:
        process = _OwnedFakeProcess(b"")
        with mock.patch.object(process.stdout, "read", side_effect=OSError("owned read failure")):
            with mock.patch.object(git_source.subprocess, "Popen", return_value=process):
                with self.assertRaises(git_source.GitSourceError):
                    self.reader._run(["--version"], repository=False, limit=512)
        self.assertTrue(process.stdout.closed)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_capture_thread_start_failure_does_not_leak_started_child(self) -> None:
        process = _OwnedFakeProcess(b"", running=True)
        with mock.patch.object(git_source.threading.Thread, "start", side_effect=RuntimeError("owned thread failure")):
            with mock.patch.object(git_source.subprocess, "Popen", return_value=process):
                with self.assertRaises(git_source.GitSourceError):
                    self.reader._run(["--version"], repository=False, limit=512)
        self.assertTrue(process.terminated)
        self.assertTrue(process.stdout.closed)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_native_git_is_never_selected_from_the_project(self) -> None:
        project_binary = self.base / "project" / "git"
        project_binary.parent.mkdir()
        project_binary.write_bytes(b"owned fixture must not execute\n")
        with mock.patch.object(git_source.shutil, "which", return_value=str(project_binary)):
            with self.assertRaisesRegex(git_source.GitSourceError, "must not come from the project"):
                git_source._native_git(project_binary.parent)

    def test_missing_native_git_fails_without_starting_process(self) -> None:
        with mock.patch.object(git_source.shutil, "which", return_value=None):
            with mock.patch.object(git_source.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(git_source.GitSourceError, "Native Git is required"):
                    git_source._native_git(self.base)
        popen.assert_not_called()

    def test_only_trusted_installed_tool_may_have_multiple_hard_links(self) -> None:
        binary = self.base / "owned-installed-tool"
        binary.write_bytes(b"not an executable; never run\n")
        os.link(binary, self.base / "owned-tool-alias")
        with mock.patch.object(git_source.shutil, "which", return_value=str(binary)):
            self.assertEqual(git_source._native_git(self.base / "project"), binary)
        with self.assertRaises(git_source.GitSourceError):
            git_source._identity(binary)  # Source/metadata admission stays strict.
        self.assertEqual(git_source._identity(binary, installed_tool=True)[3], 2)

    def test_no_lazy_fetch_flag_only_for_supported_version(self) -> None:
        for modern in (False, True):
            with self.subTest(modern=modern):
                self.reader.modern = modern
                process = _OwnedFakeProcess(b"ok")
                with mock.patch.object(git_source.subprocess, "Popen", return_value=process) as popen:
                    self.assertEqual(self.reader._run(["rev-parse", "HEAD"], limit=128), b"ok")
                command = popen.call_args.args[0]
                self.assertEqual("--no-lazy-fetch" in command, modern)
                self.assertIn("--no-replace-objects", command)
                self.assertNotIn("--filters", command)
                self.assertNotIn("--textconv", command)
        self.assertEqual(list(self.base.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
