"""Read a bounded, already-local HEAD tree; never use index/diff/filter machinery.

Git and stable user-owned Git metadata are trusted native inputs, not sandboxed
code. Indirected/partial stores are refused. Older Git's no-fetch contract relies
on the checked configuration staying stable; stat checks are not an adversarial
concurrent-mutation defense. Nothing here executes the project's source.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .repository import (
    MAX_REPOSITORY_BYTES, MAX_REPOSITORY_FILES, MAX_REPOSITORY_FILE_BYTES,
    RepositoryError, _EXCLUDED_NAMES, _EXPLAIN_EXCLUDED_DIRECTORIES,
    _EXPLAIN_EXCLUDED_FILES, _NonTextFile, _credential_like,
    _validate_portable_component, _validate_text,
)


class GitSourceError(RepositoryError):
    """An unavailable baseline, never an empty successful comparison."""


def _plain(path: Path, *, directory: bool, installed_tool: bool = False) -> os.stat_result:
    value = path.lstat()
    expected = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if (not expected or getattr(value, 'st_file_attributes', 0) & 0x400
            or path.resolve(strict=True) != path
            or (not directory and not installed_tool and value.st_nlink != 1)):
        raise GitSourceError('Git metadata must be plain, unredirected local entries')
    return value


def _identity(path: Path, *, directory: bool = False, installed_tool: bool = False) -> tuple[int, ...]:
    value = _plain(path, directory=directory, installed_tool=installed_tool)
    return tuple(getattr(value, field) for field in (
        'st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _metadata(git: Path, check: Callable[[], None] = lambda: None) -> dict[str, tuple[int, ...]]:
    check()
    result = {'.': _identity(git, directory=True)}
    for relative in ('commondir', 'config.worktree', 'objects/info/alternates',
                     'objects/info/http-alternates', 'info/grafts'):
        if os.path.lexists(git / relative):
            raise GitSourceError('Indirected Git metadata is not supported for change reading')
    for relative in ('config', 'HEAD', 'packed-refs', 'shallow'):
        path = git / relative
        if os.path.lexists(path):
            result[relative] = _identity(path)
    if 'config' not in result or 'HEAD' not in result:
        raise GitSourceError('A normal local Git checkout with HEAD and config is required')
    if (git / 'config').stat().st_size > 64 * 1024:
        raise GitSourceError('Git configuration exceeds the bounded reader limit')
    stack = [git / 'objects', git / 'refs']
    while stack:
        check()
        path = stack.pop()
        result[path.relative_to(git).as_posix()] = _identity(path, directory=True)
        with os.scandir(path) as entries:
            for entry in entries:
                check()
                if len(result) >= 16_384:
                    raise GitSourceError('Git metadata exceeds the bounded reader limit')
                child = Path(entry.path)
                is_directory = entry.is_dir(follow_symlinks=False)
                result[child.relative_to(git).as_posix()] = _identity(child, directory=is_directory)
                if child.name.endswith('.promisor'):
                    raise GitSourceError('Partial-clone object stores are not supported')
                if is_directory:
                    stack.append(child)
    return result


def _native_git(source: Path) -> Path:
    located = shutil.which('git')
    if located is None:
        raise GitSourceError('Native Git is required to read HEAD; ordinary source reading still works')
    binary = Path(located).resolve(strict=True)
    if binary == source or source in binary.parents:
        raise GitSourceError('Git executable must not come from the project being read')
    if os.name == 'nt':
        # Git for Windows cmd/git.exe is a launcher; use its packaged native
        # executable when available, without changing PATH or installation.
        native = binary.parent.parent / 'mingw64/bin/git.exe'
        if binary.parent.name.casefold() == 'cmd' and native.is_file():
            binary = native.resolve(strict=True)
    if binary == source or source in binary.parents:
        raise GitSourceError('Git executable must not come from the project being read')
    # Git for Windows legitimately hard-links its installed executables. Source
    # and metadata files remain single-link; the trusted tool is stat-guarded.
    _plain(binary, directory=False, installed_tool=True)
    return binary


class _GitReader:
    def __init__(self, source: Path, owned_root: Path):
        self.started = time.monotonic()
        self.source, self.owned_root = source.resolve(strict=True), owned_root.resolve(strict=True)
        if self.source == self.owned_root or self.source in self.owned_root.parents:
            raise GitSourceError('Git reader scratch space must be outside the project')
        self.git = self.source / '.git'
        self.stamp = _metadata(self.git, self.check_deadline)
        self.binary = _native_git(self.source)
        self.binary_stamp = _identity(self.binary, installed_tool=True)
        version = self._run(['--version'], repository=False, limit=512)
        match = re.fullmatch(rb'git version (\d+)\.(\d+)\.[^\r\n]+[\r\n]*', version)
        if match is None or tuple(map(int, match.groups())) < (2, 43):
            raise GitSourceError('Change reading requires native Git 2.43 or newer')
        self.modern = tuple(map(int, match.groups())) >= (2, 45)
        keys = self._run(['config', '--file', str(self.git / 'config'), '--no-includes',
                          '--null', '--name-only', '--list'], repository=False, limit=128 * 1024)
        for raw in keys.split(b'\0'):
            key = raw.decode('utf-8').casefold()
            if (key.startswith(('include.', 'includeif.')) or key in
                    ('extensions.partialclone', 'extensions.worktreeconfig', 'core.worktree')
                    or (key.startswith('remote.') and key.endswith(('.promisor', '.partialclonefilter')))):
                raise GitSourceError('Git configuration uses unsupported indirection or partial cloning')
        self.check_metadata()

    def check_deadline(self) -> None:
        if time.monotonic() - self.started >= 20.0:
            raise GitSourceError('Local Git reading exceeded its 20-second limit')

    def check_metadata(self) -> None:
        if (_metadata(self.git, self.check_deadline) != self.stamp
                or _identity(self.binary, installed_tool=True) != self.binary_stamp):
            raise GitSourceError('Git metadata changed during reading; refresh the comparison')
        self.check_deadline()

    def _run(self, args: list[str], *, repository: bool = True, data: bytes = b'', limit: int) -> bytes:
        remaining = 20.0 - (time.monotonic() - self.started)
        if remaining <= 0:
            raise GitSourceError('Local Git reading exceeded its 20-second limit')
        with tempfile.TemporaryDirectory(prefix='git-read-', dir=self.owned_root) as raw:
            scratch = Path(raw)
            empty = scratch / 'empty-config'
            empty.touch(exist_ok=False)
            env = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}
            env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM=str(empty),
                GIT_CONFIG_GLOBAL=str(empty), GIT_CONFIG_COUNT='0', GIT_ALLOW_PROTOCOL='',
                GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1',
                GIT_NO_LAZY_FETCH='1', GIT_CEILING_DIRECTORIES=str(self.owned_root),
                LC_ALL='C', LANG='C', TMP=str(scratch), TEMP=str(scratch))
            command = [str(self.binary), '--no-pager', '--no-replace-objects', '--no-optional-locks']
            if repository:
                if self.modern:
                    command.append('--no-lazy-fetch')
                command += ['--git-dir=' + str(self.git), '--work-tree=' + str(self.source),
                            '-c', 'core.fsmonitor=false']
            command += args
            output, overflow, capture_failed = bytearray(), threading.Event(), threading.Event()
            with tempfile.TemporaryFile(dir=scratch) as input_file:
                input_file.write(data)
                input_file.seek(0)
                process = subprocess.Popen(command, cwd=scratch, env=env, stdin=input_file,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False)
                def capture():
                    try:
                        while True:
                            chunk = process.stdout.read(64 * 1024)
                            if not chunk:
                                return
                            if len(output) + len(chunk) > limit:
                                overflow.set()
                                return
                            output.extend(chunk)
                    except (OSError, ValueError):
                        capture_failed.set()
                thread = threading.Thread(target=capture, name='forge8-git-output', daemon=True)
                deadline = self.started + 20.0
                failed = thread_started = False
                try:
                    thread.start()
                    thread_started = True
                    while process.poll() is None:
                        if overflow.is_set() or capture_failed.is_set() or time.monotonic() >= deadline:
                            failed = True
                            break
                        try:
                            process.wait(timeout=0.05)
                        except subprocess.TimeoutExpired:
                            pass
                except RuntimeError as exc:
                    raise GitSourceError('Git output capture could not be started') from exc
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)
                    if thread_started:
                        thread.join(timeout=2)
                    process.stdout.close()
                if failed or thread.is_alive() or overflow.is_set() or capture_failed.is_set():
                    raise GitSourceError('Git output or execution exceeded the bounded reader limits')
                if process.returncode != 0:
                    raise GitSourceError('The requested HEAD objects are unavailable locally; no fetch was requested')
                return bytes(output)

    def head(self) -> str:
        self.check_metadata()
        result = self._run(['rev-parse', '--verify', '--end-of-options', 'HEAD^{commit}'], limit=128).strip()
        if re.fullmatch(rb'(?:[0-9a-f]{40}|[0-9a-f]{64})', result) is None:
            raise GitSourceError('Git did not return one complete HEAD commit identity')
        self.check_metadata()
        return result.decode('ascii')


@dataclass(frozen=True)
class GitBaseline:
    commit: str
    files: dict[str, bytes]
    excluded: tuple[str, ...]


def _tree_exclusion(parts: tuple[str, ...], mode: bytes) -> str | None:
    """Mirror filesystem path admission before considering historical blob bytes."""
    if mode in (b'120000', b'160000'):
        return '/'.join(parts)  # Historical links/gitlinks are never read or followed.
    for index, part in enumerate(parts):
        folded = part.casefold()
        directory = index < len(parts) - 1
        if folded in _EXCLUDED_NAMES or (not directory and folded.endswith('.pyc')):
            return '/'.join(parts[:index + 1])
        if _credential_like(part):
            raise GitSourceError('HEAD contains credential-like source paths')
        if ((directory and folded in _EXPLAIN_EXCLUDED_DIRECTORIES)
                or (not directory and folded in _EXPLAIN_EXCLUDED_FILES)):
            return '/'.join(parts[:index + 1])
        if part.startswith('.') and not (not directory and part == '.gitignore'):
            raise GitSourceError('HEAD contains an unsupported hidden source path')
    return None


def _read_head(source: Path, owned_root: Path) -> GitBaseline:
    """Retain bounded raw HEAD bytes without checkout or source execution."""
    reader = _GitReader(source, owned_root)
    commit = reader.head()
    tree = reader._run(['ls-tree', '-r', '-l', '-z', '--full-tree', commit], limit=1024 * 1024)
    if tree and not tree.endswith(b'\0'):
        raise GitSourceError('Git returned an incomplete tree inventory')
    entries, excluded, seen, prefixes, prefix_types = [], [], set(), {}, {}
    total = 0
    for raw in (tree[:-1].split(b'\0') if tree else ()):
        reader.check_deadline()
        if not raw:
            raise GitSourceError('Git returned an empty tree record')
        try:
            descriptor, raw_path = raw.split(b'\t', 1)
            mode, kind, oid, size = descriptor.split()
            path = raw_path.decode('utf-8')
        except (ValueError, UnicodeError) as exc:
            raise GitSourceError('HEAD contains an unsupported tree entry') from exc
        parts = PurePosixPath(path).parts
        if not parts or path.startswith('/') or '/'.join(parts) != path or any(p in ('.', '..') for p in parts):
            raise GitSourceError('HEAD contains a non-portable source path')
        for part in parts:
            _validate_portable_component(part, path)
        for count in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:count])
            if prefixes.setdefault(prefix.casefold(), prefix) != prefix:
                raise GitSourceError('HEAD contains case-colliding path components')
            directory = count < len(parts)
            if prefix_types.setdefault(prefix, directory) != directory:
                raise GitSourceError('HEAD contains a file-directory path collision')
        if path.casefold() in seen:
            raise GitSourceError('HEAD contains case-colliding source paths')
        seen.add(path.casefold())
        exclusion = _tree_exclusion(parts, mode)
        if exclusion is not None:
            excluded.append(exclusion)
            continue
        if kind != b'blob' or mode not in (b'100644', b'100755') or not size.isdigit():
            raise GitSourceError('HEAD contains an unsupported source object')
        if re.fullmatch(rb'[0-9a-f]{' + str(len(commit)).encode() + rb'}', oid) is None:
            raise GitSourceError('HEAD contains an invalid blob identity')
        length = int(size)
        total += length
        if length > MAX_REPOSITORY_FILE_BYTES or total > MAX_REPOSITORY_BYTES or len(entries) >= MAX_REPOSITORY_FILES:
            raise GitSourceError('HEAD exceeds the existing source admission limits')
        entries.append((path, oid, length))
    data = reader._run(['cat-file', '--batch'], data=b''.join(oid + b'\n' for _, oid, _ in entries),
        limit=MAX_REPOSITORY_BYTES + MAX_REPOSITORY_FILES * 128)
    files, cursor = {}, 0
    for path, oid, length in entries:
        reader.check_deadline()
        end = data.find(b'\n', cursor)
        if end < 0 or data[cursor:end] != oid + b' blob ' + str(length).encode():
            raise GitSourceError('Git returned mismatched blob framing')
        cursor = end + 1
        content = data[cursor:cursor + length]
        cursor += length
        if len(content) != length or data[cursor:cursor + 1] != b'\n':
            raise GitSourceError('Git returned incomplete blob content')
        cursor += 1
        object_bytes = b'blob ' + str(length).encode() + b'\0' + content
        fingerprint = hashlib.sha1(object_bytes).hexdigest() if len(oid) == 40 else hashlib.sha256(object_bytes).hexdigest()
        if fingerprint.encode() != oid:
            raise GitSourceError('Retained Git blob bytes do not match their object identity')
        try:
            _validate_text(content, path)
        except _NonTextFile:
            excluded.append(path)
        else:
            files[path] = content
    if cursor != len(data) or reader.head() != commit:
        raise GitSourceError('Git output or HEAD changed during source capture')
    return GitBaseline(commit, files, tuple(sorted(set(excluded))))


def read_head(source: Path, owned_root: Path) -> GitBaseline:
    """Read an unavailable-or-complete local baseline, never a silent partial tree."""
    try:
        return _read_head(source, owned_root)
    except GitSourceError:
        raise
    except (OSError, UnicodeError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise GitSourceError('Local HEAD source could not be safely retained') from exc


def head_identity(source: Path, owned_root: Path) -> str:
    """Recheck local metadata and HEAD, without retaining the blobs again."""
    try:
        return _GitReader(source, owned_root).head()
    except GitSourceError:
        raise
    except (OSError, UnicodeError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise GitSourceError('Local HEAD identity could not be safely checked') from exc
