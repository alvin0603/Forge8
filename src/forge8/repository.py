"""Fail-closed source snapshots for repair and read-only explanation.

The operator must never work directly in a user's repository.  This module
builds an immutable-content snapshot without following links, records the exact
included inventory, and validates the only paths a later operator may modify.
It intentionally supports a narrow source shape: small UTF-8 repositories whose
tests and control files can be copied and checked without dependency discovery.
Explanation may explicitly exclude ordinary development configuration and
non-text files; excluded content is never copied or represented as understood.
"""

from __future__ import annotations

import hashlib
import ctypes
import errno
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


MAX_REPOSITORY_FILES = 1_000
MAX_REPOSITORY_BYTES = 16 * 1024 * 1024
MAX_REPOSITORY_FILE_BYTES = 256 * 1024

_WINDOWS_ABSOLUTE = re.compile(r"^[a-zA-Z]:[\\/]")
_EXCLUDED_NAMES = frozenset(
    {
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
    }
)
_EXPLAIN_EXCLUDED_DIRECTORIES = frozenset({".github", ".vscode", ".devcontainer"})
_EXPLAIN_EXCLUDED_FILES = frozenset(
    {".editorconfig", ".pre-commit-config.yaml", ".readthedocs.yaml", ".readthedocs.yml",
     ".markdownlint.yaml", ".python-version", ".coveragerc"}
)
_CREDENTIAL_PARTS = frozenset({".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker"})
_CREDENTIAL_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
)
_CREDENTIAL_SUFFIXES = frozenset({".pem", ".p12", ".pfx", ".key"})
_WINDOWS_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"/\\|?*')
_BINARY_CONTROL_CODEPOINTS = frozenset(
    {*range(0x00, 0x09), 0x0B, *range(0x0E, 0x20), 0x7F}
)


class RepositoryError(ValueError):
    """Raised when a repository cannot be admitted safely."""


class _NonTextFile(RepositoryError):
    """A safely read regular file cannot be represented as bounded UTF-8 text."""


@dataclass(frozen=True, slots=True)
class RepositoryFileFingerprint:
    """Content identity for one included regular file."""

    path: str
    size_bytes: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """Published sanitized snapshot and its deterministic source inventory."""

    source_root: str
    snapshot_root: str
    fingerprints: tuple[RepositoryFileFingerprint, ...]
    excluded: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "source_root": self.source_root,
            "snapshot_root": self.snapshot_root,
            "fingerprints": [item.as_dict() for item in self.fingerprints],
            "excluded": list(self.excluded),
        }


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    fingerprint: RepositoryFileFingerprint
    content: bytes


@dataclass(frozen=True, slots=True)
class _RepositoryScan:
    files: tuple[_ScannedFile, ...]
    directories: tuple[str, ...]
    excluded: tuple[str, ...]

    @property
    def fingerprints(self) -> tuple[RepositoryFileFingerprint, ...]:
        return tuple(item.fingerprint for item in self.files)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except (OSError, ValueError) as exc:
        raise RepositoryError(f"cannot inspect {label} {path}: {exc}") from exc


def _strict_existing_directory(raw_path: str | os.PathLike[str], label: str) -> Path:
    supplied = Path(raw_path).expanduser()
    metadata = _lstat(supplied, label)
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
        raise RepositoryError(f"{label} must not be a symlink or reparse point")
    if not stat.S_ISDIR(metadata.st_mode):
        raise RepositoryError(f"{label} is not a directory: {supplied}")
    try:
        resolved = supplied.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise RepositoryError(f"cannot resolve {label} {supplied}: {exc}") from exc
    if resolved == Path(resolved.anchor):
        raise RepositoryError(f"{label} cannot be a filesystem root")
    return resolved


def _strict_destination(raw_path: str | os.PathLike[str]) -> Path:
    supplied = Path(raw_path).expanduser()
    try:
        if os.path.lexists(os.fspath(supplied)):
            raise RepositoryError(f"snapshot destination already exists: {supplied}")
    except (OSError, ValueError) as exc:
        if isinstance(exc, RepositoryError):
            raise
        raise RepositoryError(f"cannot inspect snapshot destination {supplied}: {exc}") from exc
    if supplied.name in {"", ".", ".."}:
        raise RepositoryError("snapshot destination must name a new directory")
    parent = _strict_existing_directory(supplied.parent, "snapshot destination parent")
    destination = parent / supplied.name
    if destination == Path(destination.anchor):
        raise RepositoryError("snapshot destination cannot be a filesystem root")
    return destination


def _credential_like(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in _CREDENTIAL_PARTS
        or lowered in _CREDENTIAL_NAMES
        or lowered.startswith(".env.")
        or Path(lowered).suffix in _CREDENTIAL_SUFFIXES
    )


def _validate_portable_component(name: str, relative: str) -> None:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RepositoryError(f"path is not valid UTF-8: {relative}") from exc
    if any(character in _WINDOWS_FORBIDDEN_CHARS for character in name) or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in name
    ):
        raise RepositoryError(f"non-portable path component is not allowed: {relative}")
    if unicodedata.normalize("NFC", name) != name:
        raise RepositoryError(f"non-canonical Unicode path is not allowed: {relative}")
    lowered = name.casefold()
    windows_stem = lowered.rstrip(" .").split(".", 1)[0]
    if lowered != lowered.rstrip(" .") or windows_stem in _WINDOWS_RESERVED:
        raise RepositoryError(f"Windows-reserved path is not allowed: {relative}")


def _validate_text(content: bytes, relative: str) -> None:
    if b"\x00" in content:
        raise _NonTextFile(f"NUL byte is not allowed in repository text: {relative}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _NonTextFile(f"file is not valid UTF-8 text: {relative}") from exc
    if any(ord(character) in _BINARY_CONTROL_CODEPOINTS for character in text):
        raise _NonTextFile(f"binary control data is not allowed: {relative}")


def _read_regular_file(path: Path, metadata: os.stat_result, relative: str) -> bytes:
    if metadata.st_nlink != 1:
        raise RepositoryError(f"hard-linked files are not allowed: {relative}")
    if metadata.st_size > MAX_REPOSITORY_FILE_BYTES:
        raise RepositoryError(
            f"file exceeds the {MAX_REPOSITORY_FILE_BYTES}-byte limit: {relative}"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RepositoryError(f"cannot open repository file {relative}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
            or opened.st_nlink != 1
        ):
            raise RepositoryError(f"repository file changed type while reading: {relative}")
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or opened.st_size != metadata.st_size
            or opened.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise RepositoryError(f"repository file changed while reading: {relative}")
        chunks: list[bytes] = []
        observed = 0
        while observed <= MAX_REPOSITORY_FILE_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_REPOSITORY_FILE_BYTES + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
        if observed > MAX_REPOSITORY_FILE_BYTES:
            raise RepositoryError(
                f"file exceeds the {MAX_REPOSITORY_FILE_BYTES}-byte limit: {relative}"
            )
        final = os.fstat(descriptor)
    except OSError as exc:
        raise RepositoryError(f"cannot read repository file {relative}: {exc}") from exc
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    if (
        (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
        or final.st_size != opened.st_size
        or final.st_mtime_ns != opened.st_mtime_ns
        or len(content) != final.st_size
    ):
        raise RepositoryError(f"repository file changed while reading: {relative}")
    _validate_text(content, relative)
    return content


def _scan_repository(root: Path, *, for_explanation: bool = False) -> _RepositoryScan:
    files: list[_ScannedFile] = []
    directories: list[str] = []
    excluded: list[str] = []
    total_bytes = 0
    examined_files = 0

    def visit(directory: Path, relative_directory: PurePosixPath | None) -> None:
        nonlocal total_bytes, examined_files
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError as exc:
            label = "." if relative_directory is None else relative_directory.as_posix()
            raise RepositoryError(f"cannot scan repository directory {label}: {exc}") from exc

        folded_names: set[str] = set()
        for entry in entries:
            relative_path = (
                PurePosixPath(entry.name)
                if relative_directory is None
                else relative_directory / entry.name
            )
            relative = relative_path.as_posix()
            _validate_portable_component(entry.name, relative)
            folded = entry.name.casefold()
            if folded in folded_names:
                raise RepositoryError(f"case-colliding repository entries are not allowed: {relative}")
            folded_names.add(folded)
            try:
                # Native CPython 3.10 can return st_nlink=0 specifically from
                # DirEntry.stat(follow_symlinks=False). Path.lstat preserves
                # no-follow type evidence and returns the usable link count.
                metadata = Path(entry.path).lstat()
            except OSError as exc:
                raise RepositoryError(f"cannot inspect repository path {relative}: {exc}") from exc
            mode = metadata.st_mode
            if stat.S_ISLNK(mode) or _is_reparse_point(metadata):
                raise RepositoryError(f"symlinks and reparse points are not allowed: {relative}")
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                raise RepositoryError(f"special files are not allowed: {relative}")
            if stat.S_ISREG(mode) and metadata.st_nlink != 1:
                raise RepositoryError(f"hard-linked files are not allowed: {relative}")

            explicitly_excluded = folded in _EXCLUDED_NAMES or (
                stat.S_ISREG(mode) and folded.endswith(".pyc")
            )
            if explicitly_excluded:
                excluded.append(relative)
                continue
            if _credential_like(entry.name):
                raise RepositoryError(f"credential-like path is not allowed: {relative}")
            if (
                for_explanation
                and (
                    (stat.S_ISDIR(mode) and folded in _EXPLAIN_EXCLUDED_DIRECTORIES)
                    or (stat.S_ISREG(mode) and folded in _EXPLAIN_EXCLUDED_FILES)
                )
            ):
                # Like build/VCS exclusions, these ordinary entries are not read
                # or traversed. Their contents are outside the admitted scope.
                excluded.append(relative)
                continue
            # A normal Git repository commonly carries this one control file.
            # Admit its exact bytes into the pinned snapshot; workspace and
            # allowed-write policy still keep every hidden path model-invisible
            # and immutable. Directories or differently cased names do not qualify.
            readonly_gitignore = (
                entry.name == ".gitignore" and stat.S_ISREG(mode)
            )
            if entry.name.startswith(".") and not readonly_gitignore:
                raise RepositoryError(f"hidden path is not allowed: {relative}")

            if stat.S_ISDIR(mode):
                directories.append(relative)
                visit(Path(entry.path), relative_path)
                continue

            # Non-text classification still costs I/O: exclusions must not turn
            # the source-size limits into an unbounded binary scan.
            examined_files += 1
            total_bytes += metadata.st_size
            if examined_files > MAX_REPOSITORY_FILES:
                raise RepositoryError(
                    f"repository exceeds the {MAX_REPOSITORY_FILES}-file limit"
                )
            if total_bytes > MAX_REPOSITORY_BYTES:
                raise RepositoryError(
                    f"repository exceeds the {MAX_REPOSITORY_BYTES}-byte total limit"
                )
            try:
                content = _read_regular_file(Path(entry.path), metadata, relative)
            except _NonTextFile as exc:
                if not for_explanation:
                    raise RepositoryError(str(exc)) from exc
                excluded.append(relative)
                continue
            files.append(
                _ScannedFile(
                    RepositoryFileFingerprint(
                        path=relative,
                        size_bytes=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                    ),
                    content,
                )
            )

    visit(root, None)
    return _RepositoryScan(
        files=tuple(sorted(files, key=lambda item: item.fingerprint.path)),
        directories=tuple(sorted(directories, key=lambda path: (path.count("/"), path))),
        excluded=tuple(sorted(excluded)),
    )


def _write_scanned_tree(scan: _RepositoryScan, destination: Path) -> None:
    for relative in scan.directories:
        (destination / Path(*PurePosixPath(relative).parts)).mkdir(parents=True, exist_ok=True)
    for item in scan.files:
        target = destination / Path(*PurePosixPath(item.fingerprint.path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as stream:
                stream.write(item.content)
        except OSError as exc:
            raise RepositoryError(f"cannot write snapshot file {item.fingerprint.path}: {exc}") from exc


def _publish_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish a directory without ever replacing a raced target."""

    if os.name == "nt":
        # Python's Windows rename is fail-if-exists (MoveFile semantics).
        retry_delays = (0.05, 0.10)
        for attempt in range(len(retry_delays) + 1):
            try:
                os.rename(source, destination)
                return
            except OSError as exc:
                if os.path.lexists(destination):
                    raise RepositoryError(
                        f"snapshot destination appeared during creation: {destination}"
                    ) from exc
                if (
                    not isinstance(exc, PermissionError)
                    or getattr(exc, "winerror", None) != 5
                    or attempt == len(retry_delays)
                ):
                    raise RepositoryError(
                        f"cannot publish repository snapshot {destination}: {exc}"
                    ) from exc
                time.sleep(retry_delays[attempt])
                if os.path.lexists(destination):
                    raise RepositoryError(
                        f"snapshot destination appeared during creation: {destination}"
                    ) from exc

    if not sys.platform.startswith("linux"):
        raise RepositoryError(
            "atomic no-replace snapshot publication is unavailable on this platform"
        )
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise RepositoryError(
            "atomic no-replace snapshot publication requires Linux renameat2"
        ) from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        rename_noreplace,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise RepositoryError(
            f"snapshot destination appeared during creation: {destination}"
        )
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise RepositoryError(
            "atomic no-replace snapshot publication is unsupported by this filesystem; "
            "set FORGE8_STATE_HOME to a native Linux filesystem directory "
            "(for WSL, e.g. $HOME/.local/state/forge8, not /mnt/c)"
        )
    raise RepositoryError(
        f"cannot publish repository snapshot {destination}: "
        f"{os.strerror(error_number)}"
    )


def prepare_repository_snapshot(
    source_root: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    for_explanation: bool = False,
) -> RepositorySnapshot:
    """Create and publish one sanitized immutable-content repository snapshot.

    The destination's parent must already exist and the destination itself must
    not.  Work is built in a private sibling directory, so rejected input and
    detected source races do not expose an incomplete destination.

    The default is strict repair ingress. ``for_explanation`` excludes the named
    development configuration above and safely classified non-text files,
    recording each path in ``excluded``. Excluded contents are not fingerprinted;
    their presence and classification, plus every admitted file's bytes, are
    rechecked. Size, path, credential, link, race, and I/O errors still fail closed.
    Never enable this option when checking an already-sanitized snapshot for added files.
    """

    source = _strict_existing_directory(source_root, "repository source")
    snapshot = _strict_destination(destination)
    if _is_relative_to(snapshot, source) or _is_relative_to(source, snapshot):
        raise RepositoryError("repository source and snapshot destination must not overlap")

    before = _scan_repository(source, for_explanation=for_explanation)
    prefix = f".{snapshot.name}.forge8-partial-"
    with tempfile.TemporaryDirectory(prefix=prefix, dir=snapshot.parent) as temporary:
        staging = Path(temporary)
        _write_scanned_tree(before, staging)
        copied = _scan_repository(staging)
        if copied.fingerprints != before.fingerprints or copied.directories != before.directories:
            raise RepositoryError("snapshot copy does not match the source inventory")
        after = _scan_repository(source, for_explanation=for_explanation)
        if (
            after.fingerprints != before.fingerprints
            or after.directories != before.directories
            or after.excluded != before.excluded
        ):
            raise RepositoryError("repository source changed while the snapshot was created")
        if os.path.lexists(snapshot):
            raise RepositoryError(f"snapshot destination appeared during creation: {snapshot}")
        _publish_directory_noreplace(staging, snapshot)

    return RepositorySnapshot(
        source_root=str(source),
        snapshot_root=str(snapshot),
        fingerprints=before.fingerprints,
        excluded=before.excluded,
    )


def _normalized_allowed_path(raw_path: object, index: int) -> PurePosixPath:
    label = f"allowed write path {index}"
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise RepositoryError(f"{label} must be a non-empty string")
    if "\\" in raw_path:
        raise RepositoryError(f"{label} must use POSIX separators")
    if raw_path.startswith(("/", "//")) or _WINDOWS_ABSOLUTE.match(raw_path):
        raise RepositoryError(f"{label} must be relative")
    parts = raw_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RepositoryError(f"{label} must be a normalized relative path")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or path.as_posix() != raw_path:
        raise RepositoryError(f"{label} must be a normalized relative path")
    for component in path.parts:
        _validate_portable_component(component, raw_path)
        if _credential_like(component):
            raise RepositoryError(f"credential-like allowed write path is not permitted: {raw_path}")
        if component.startswith("."):
            raise RepositoryError(f"hidden allowed write path is not permitted: {raw_path}")
        if component.casefold() == "tests":
            raise RepositoryError(f"test paths cannot be writable: {raw_path}")
    return path


def _validate_allowed_subtree(root: Path, relative_root: PurePosixPath) -> None:
    """Reject protected or unsafe descendants encompassed by a writable directory."""

    try:
        with os.scandir(root) as iterator:
            entries = tuple(iterator)
    except OSError as exc:
        raise RepositoryError(
            f"cannot inspect allowed write directory {relative_root.as_posix()}: {exc}"
        ) from exc
    for entry in entries:
        relative = relative_root / entry.name
        lowered = entry.name.casefold()
        if lowered == "tests":
            raise RepositoryError(f"test paths cannot be writable: {relative.as_posix()}")
        if entry.name.startswith("."):
            raise RepositoryError(
                f"hidden allowed write path is not permitted: {relative.as_posix()}"
            )
        if _credential_like(entry.name):
            raise RepositoryError(
                f"credential-like allowed write path is not permitted: {relative.as_posix()}"
            )
        try:
            metadata = Path(entry.path).lstat()
        except OSError as exc:
            raise RepositoryError(
                f"cannot inspect allowed write path {relative.as_posix()}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
            raise RepositoryError(
                f"allowed write path contains a symlink or reparse point: {relative.as_posix()}"
            )
        if not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISREG(metadata.st_mode):
            raise RepositoryError(
                f"allowed write path is not a regular file or directory: {relative.as_posix()}"
            )
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise RepositoryError(f"allowed write path is hard-linked: {relative.as_posix()}")
        if stat.S_ISDIR(metadata.st_mode):
            _validate_allowed_subtree(Path(entry.path), relative)


def _resolve_allowed_path(
    root: Path, requested: PurePosixPath
) -> tuple[PurePosixPath, Path, os.stat_result]:
    """Resolve with no-follow lookup and return the filesystem's actual casing."""

    current = root
    actual_parts: list[str] = []
    final_metadata: os.stat_result | None = None
    for component in requested.parts:
        try:
            with os.scandir(current) as iterator:
                matches = tuple(
                    entry for entry in iterator if entry.name.casefold() == component.casefold()
                )
        except OSError as exc:
            raise RepositoryError(
                f"cannot inspect allowed write path {requested.as_posix()}: {exc}"
            ) from exc
        if not matches:
            raise RepositoryError(
                f"cannot inspect allowed write path {requested.as_posix()}: path does not exist"
            )
        if len(matches) != 1:
            raise RepositoryError(
                f"allowed write path has ambiguous casing: {requested.as_posix()}"
            )
        entry = matches[0]
        actual_parts.append(entry.name)
        try:
            final_metadata = Path(entry.path).lstat()
        except OSError as exc:
            raise RepositoryError(
                f"cannot inspect allowed write path {requested.as_posix()}: {exc}"
            ) from exc
        if stat.S_ISLNK(final_metadata.st_mode) or _is_reparse_point(final_metadata):
            raise RepositoryError(
                f"allowed write path contains a symlink or reparse point: {requested.as_posix()}"
            )
        current = Path(entry.path)

    assert final_metadata is not None
    return PurePosixPath(*actual_parts), current, final_metadata


def validate_allowed_write_paths(
    snapshot: str | os.PathLike[str], paths: Iterable[str]
) -> tuple[str, ...]:
    """Validate and canonicalize non-overlapping writable roots in a snapshot."""

    root = _strict_existing_directory(snapshot, "repository snapshot")
    if isinstance(paths, (str, bytes)):
        raise RepositoryError("allowed write paths must be an iterable of path strings")
    try:
        supplied = tuple(paths)
    except TypeError as exc:
        raise RepositoryError("allowed write paths must be iterable") from exc
    if not supplied:
        raise RepositoryError("at least one allowed write path is required")

    normalized: list[PurePosixPath] = []
    folded_paths: list[tuple[str, ...]] = []
    for index, raw_path in enumerate(supplied):
        requested = _normalized_allowed_path(raw_path, index)
        path, current, final_metadata = _resolve_allowed_path(root, requested)
        folded = tuple(part.casefold() for part in path.parts)
        for existing, existing_folded in zip(normalized, folded_paths, strict=True):
            overlap = (
                folded == existing_folded
                or folded[: len(existing_folded)] == existing_folded
                or existing_folded[: len(folded)] == folded
            )
            if overlap:
                raise RepositoryError(
                    f"allowed write paths overlap or duplicate: {existing.as_posix()} and {path.as_posix()}"
                )

        if not stat.S_ISDIR(final_metadata.st_mode) and not stat.S_ISREG(final_metadata.st_mode):
            raise RepositoryError(f"allowed write path is not a regular file or directory: {path}")
        if stat.S_ISREG(final_metadata.st_mode) and final_metadata.st_nlink != 1:
            raise RepositoryError(f"allowed write path is hard-linked: {path}")
        if stat.S_ISDIR(final_metadata.st_mode):
            _validate_allowed_subtree(current, path)
        normalized.append(path)
        folded_paths.append(folded)

    return tuple(path.as_posix() for path in normalized)


__all__ = [
    "MAX_REPOSITORY_BYTES",
    "MAX_REPOSITORY_FILE_BYTES",
    "MAX_REPOSITORY_FILES",
    "RepositoryError",
    "RepositoryFileFingerprint",
    "RepositorySnapshot",
    "prepare_repository_snapshot",
    "validate_allowed_write_paths",
]
