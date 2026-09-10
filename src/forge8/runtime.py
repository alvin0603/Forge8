"""Pinned runtime manifests and integrity verification."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Runtime manifest contains a non-finite JSON number: {value}")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Runtime manifest contains duplicate JSON key: {key!r}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class IntegrityResult:
    ok: bool
    checked: tuple[str, ...]
    missing: tuple[str, ...]
    mismatched: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": list(self.checked),
            "missing": list(self.missing),
            "mismatched": list(self.mismatched),
        }


def _check_cancel(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise KeyboardInterrupt("integrity verification cancelled")


def _parallel_hash_eligible(path: Path, chunk_size: int) -> bool:
    # Performance routing for the conventional Windows-drive paths in WSL, not
    # a filesystem identity/security check. All existing artifact gates remain.
    return (sys.platform == "linux" and hasattr(os, "pread")
        and chunk_size == 4 * 1024 * 1024
        and re.fullmatch(r"/mnt/[a-z]/.+", path.as_posix()) is not None
        and "microsoft" in platform.release().lower())


def _sha256_pread(stream, chunk_size: int, *, cancel_requested=None) -> str:
    """Bounded ordered reads of one open file; join all workers before returning."""
    _check_cancel(cancel_requested)
    if type(chunk_size) is not int or not 0 < chunk_size <= 4 * 1024 * 1024:
        raise ValueError("positional hash chunks must be between 1 byte and 4 MiB")
    descriptor = stream.fileno()
    before = os.fstat(descriptor)
    stopped = threading.Event()
    digest = hashlib.sha256()

    def read_block(offset: int, length: int) -> bytes | bytearray:
        if stopped.is_set():
            raise InterruptedError("integrity read stopped")
        data = os.pread(descriptor, length, offset)
        if len(data) == length:
            return data
        # Some filesystems legitimately return short reads. Assemble them in a
        # fixed buffer, not an arbitrarily long list of tiny byte objects.
        assembled = bytearray(length)
        received = 0
        while received < length:
            if not data:
                raise OSError("file ended during integrity verification")
            assembled[received:received + len(data)] = data
            received += len(data)
            if received == length:
                break
            if stopped.is_set():
                raise InterruptedError("integrity read stopped")
            data = os.pread(descriptor, length - received, offset + received)
        return assembled

    pending = deque()
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="forge8-hash")
    try:
        offset = 0
        while offset < before.st_size or pending:
            _check_cancel(cancel_requested)  # Caller callbacks stay on this thread.
            while len(pending) < 4 and offset < before.st_size:
                length = min(chunk_size, before.st_size - offset)
                pending.append(pool.submit(read_block, offset, length))
                offset += length
            future = pending[0]
            while not wait((future,), timeout=0.05).done:
                _check_cancel(cancel_requested)
            _check_cancel(cancel_requested)
            digest.update(future.result())
            pending.popleft()
            del future  # Drop consumed bytes before submitting a replacement.
        _check_cancel(cancel_requested)
        if os.pread(descriptor, 1, before.st_size):
            raise OSError("file grew during integrity verification")
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, key) != getattr(after, key) for key in fields):
            raise OSError("file changed during integrity verification")
        _check_cancel(cancel_requested)
        return digest.hexdigest()
    finally:
        # A running OS read is not forcibly interruptible. Wait for it before the
        # caller closes/reuses this descriptor, including on KeyboardInterrupt.
        interrupted = False
        while True:
            try:
                stopped.set()
                for future in pending:
                    future.cancel()
                while any(not future.done() for future in pending):
                    wait(pending, timeout=0.05)
                pool.shutdown(wait=True, cancel_futures=True)
                break
            except KeyboardInterrupt:
                interrupted = True
        if interrupted:
            raise KeyboardInterrupt("integrity verification cancelled during teardown")


def sha256_file(
    path: Path,
    chunk_size: int = 4 * 1024 * 1024,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> str:
    """Hash all bytes, or interrupt between reads without returning a partial digest."""

    _check_cancel(cancel_requested)
    digest = hashlib.sha256()
    parallel_digest = None
    with path.open("rb") as stream:
        if _parallel_hash_eligible(path, chunk_size):
            metadata = os.fstat(stream.fileno())
            if stat.S_ISREG(metadata.st_mode) and metadata.st_size >= 64 * 1024 * 1024:
                parallel_digest = _sha256_pread(stream, chunk_size, cancel_requested=cancel_requested)
        if parallel_digest is None:
            while True:
                _check_cancel(cancel_requested)
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
    _check_cancel(cancel_requested)
    return parallel_digest if parallel_digest is not None else digest.hexdigest()


def _relative_manifest_path(value: Any, label: str) -> str:
    """Validate one portable path before it can be joined to an artifact root."""

    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"Runtime manifest {label} must be a non-empty POSIX relative path")
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or any(":" in part for part in raw_parts)
    ):
        raise ValueError(f"Runtime manifest {label} must be a normalized relative path")
    return path.as_posix()


def _sha256_pin(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"Runtime manifest {label} must be a 64-character SHA-256")
    return value.lower()


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError(f"Runtime manifest must contain a JSON object: {path}")
    if payload.get("schema_version") != 2:
        raise ValueError(f"Unsupported runtime manifest schema in {path}")
    for key in (
        "build",
        "commit",
        "assets",
        "install_dir",
        "required_files",
        "installed_files",
    ):
        if key not in payload:
            raise ValueError(f"Runtime manifest is missing {key!r}")

    payload["install_dir"] = _relative_manifest_path(
        payload["install_dir"], "install_dir"
    )

    assets = payload["assets"]
    if not isinstance(assets, list) or not assets:
        raise ValueError("Runtime manifest assets must be a non-empty array")
    asset_names: list[str] = []
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise ValueError(f"Runtime manifest assets[{index}] must be an object")
        if "filename" not in asset or "sha256" not in asset:
            raise ValueError(
                f"Runtime manifest assets[{index}] must pin filename and sha256"
            )
        asset["filename"] = _relative_manifest_path(
            asset["filename"], f"assets[{index}].filename"
        )
        asset["sha256"] = _sha256_pin(asset["sha256"], f"assets[{index}].sha256")
        asset_names.append(asset["filename"])
    if len(asset_names) != len(set(asset_names)):
        raise ValueError("Runtime manifest assets must not contain duplicate filenames")

    required_files = payload["required_files"]
    if not isinstance(required_files, list) or not required_files:
        raise ValueError("Runtime manifest required_files must be a non-empty array")
    normalized_required = [
        _relative_manifest_path(value, f"required_files[{index}]")
        for index, value in enumerate(required_files)
    ]
    if len(normalized_required) != len(set(normalized_required)):
        raise ValueError("Runtime manifest required_files must not contain duplicates")
    payload["required_files"] = normalized_required

    installed_files = payload["installed_files"]
    if not isinstance(installed_files, list) or not installed_files:
        raise ValueError("Runtime manifest installed_files must be a non-empty array")
    normalized_pins: list[dict[str, Any]] = []
    pinned_names: list[str] = []
    expected_pin_keys = {"filename", "size_bytes", "sha256"}
    for index, pin in enumerate(installed_files):
        if not isinstance(pin, dict):
            raise ValueError(f"Runtime manifest installed_files[{index}] must be an object")
        unknown = sorted(set(pin) - expected_pin_keys)
        missing = sorted(expected_pin_keys - set(pin))
        if unknown or missing:
            details: list[str] = []
            if missing:
                details.append(f"missing {', '.join(missing)}")
            if unknown:
                details.append(f"unknown {', '.join(unknown)}")
            raise ValueError(
                f"Runtime manifest installed_files[{index}] has invalid fields: "
                + "; ".join(details)
            )
        filename = _relative_manifest_path(
            pin["filename"], f"installed_files[{index}].filename"
        )
        size_bytes = pin["size_bytes"]
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError(
                f"Runtime manifest installed_files[{index}].size_bytes must be a non-negative integer"
            )
        normalized_pins.append(
            {
                "filename": filename,
                "size_bytes": size_bytes,
                "sha256": _sha256_pin(
                    pin["sha256"], f"installed_files[{index}].sha256"
                ),
            }
        )
        pinned_names.append(filename)
    if len(pinned_names) != len(set(pinned_names)):
        raise ValueError("Runtime manifest installed_files must not contain duplicate filenames")

    missing_pins = sorted(set(normalized_required) - set(pinned_names))
    if missing_pins:
        raise ValueError(
            "Runtime manifest required_files are missing installed_files pins: "
            + ", ".join(missing_pins)
        )
    payload["installed_files"] = normalized_pins
    return payload


def _artifact_path(root: Path, relative: str) -> Path:
    """Resolve a validated manifest path and reject link-based root escapes."""

    candidate = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
            raise ValueError(f"artifact path contains a symlink or reparse point: {relative}")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"artifact escapes its root: {relative}") from exc
    return resolved


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _installed_inventory(
    root: Path, *, cancel_requested: Callable[[], bool] | None = None
) -> tuple[dict[str, Path], tuple[str, ...]]:
    """Walk an install tree without following links and classify every entry."""

    files: dict[str, Path] = {}
    unsafe: list[str] = []
    _check_cancel(cancel_requested)
    if not root.is_dir():
        return files, ()

    def visit(directory: Path, prefix: PurePosixPath | None = None) -> None:
        _check_cancel(cancel_requested)
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            location = "." if prefix is None else prefix.as_posix()
            unsafe.append(f"cannot scan installed path {location}: {type(exc).__name__}: {exc}")
            return
        for entry in entries:
            _check_cancel(cancel_requested)
            relative = PurePosixPath(entry.name) if prefix is None else prefix / entry.name
            relative_text = relative.as_posix()
            try:
                # CPython 3.10 on Windows reports st_nlink=0 through
                # DirEntry.stat(follow_symlinks=False) for ordinary files,
                # while Path.lstat()/fstat correctly report 1. Use lstat so
                # the hardlink gate remains fail-closed without rejecting
                # every native-Windows install tree.
                metadata = Path(entry.path).lstat()
            except OSError as exc:
                unsafe.append(
                    f"cannot stat installed path {relative_text}: {type(exc).__name__}: {exc}"
                )
                continue
            mode = metadata.st_mode
            if stat.S_ISLNK(mode) or _is_reparse_point(metadata):
                unsafe.append(f"installed path is a symlink or reparse point: {relative_text}")
                continue
            if stat.S_ISDIR(mode):
                visit(Path(entry.path), relative)
                continue
            if not stat.S_ISREG(mode):
                unsafe.append(f"installed path is not a regular file: {relative_text}")
                continue
            files[relative_text] = Path(entry.path)
            if metadata.st_nlink != 1:
                unsafe.append(f"installed file is hard-linked: {relative_text}")

    visit(root)
    _check_cancel(cancel_requested)
    return files, tuple(unsafe)


def verify_runtime(
    manifest_path: Path,
    runtime_root: Path,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> IntegrityResult:
    _check_cancel(cancel_requested)
    cancel_kwargs = {} if cancel_requested is None else {"cancel_requested": cancel_requested}
    manifest = load_manifest(manifest_path)
    root = runtime_root.expanduser().resolve(strict=False)
    checked: list[str] = []
    missing: list[str] = []
    mismatched: list[str] = []

    for asset in manifest["assets"]:
        _check_cancel(cancel_requested)
        filename = asset["filename"]
        try:
            path = _artifact_path(root, filename)
        except ValueError as exc:
            mismatched.append(f"{filename}: {exc}")
            continue
        if not path.is_file():
            missing.append(filename)
            continue
        actual = sha256_file(path, **cancel_kwargs)
        checked.append(filename)
        if actual.lower() != asset["sha256"]:
            mismatched.append(f"{filename}: expected {asset['sha256']}, got {actual}")

    _check_cancel(cancel_requested)
    install_dir: Path | None
    try:
        install_dir = _artifact_path(root, manifest["install_dir"])
    except ValueError as exc:
        mismatched.append(f"{manifest['install_dir']}: {exc}")
        install_dir = None
    pins = {pin["filename"]: pin for pin in manifest["installed_files"]}
    actual_files, unsafe_entries = (
        ({}, ()) if install_dir is None else _installed_inventory(install_dir, **cancel_kwargs)
    )
    _check_cancel(cancel_requested)
    mismatched.extend(
        f"{manifest['install_dir']}/{message}" for message in unsafe_entries
    )
    expected_names = set(pins)
    actual_names = set(actual_files)
    for filename in sorted(expected_names - actual_names):
        missing.append(f"{manifest['install_dir']}/{filename}")
    for filename in sorted(actual_names - expected_names):
        mismatched.append(
            f"unexpected installed file: {manifest['install_dir']}/{filename}"
        )

    unsafe_paths = {
        message.rsplit(": ", 1)[-1]
        for message in unsafe_entries
        if message.startswith(
            ("installed path is", "installed file is")
        )
    }
    for filename in sorted(expected_names & actual_names):
        _check_cancel(cancel_requested)
        relative = f"{manifest['install_dir']}/{filename}"
        if filename in unsafe_paths:
            continue
        installed = actual_files[filename]
        checked.append(relative)
        pin = pins[filename]
        actual_size = installed.stat().st_size
        expected_size = pin["size_bytes"]
        if actual_size != expected_size:
            mismatched.append(
                f"{relative}: expected {expected_size} bytes, got {actual_size}"
            )
            continue
        actual = sha256_file(installed, **cancel_kwargs)
        if actual.lower() != pin["sha256"]:
            mismatched.append(f"{relative}: expected {pin['sha256']}, got {actual}")

    _check_cancel(cancel_requested)
    return IntegrityResult(
        ok=not missing and not mismatched,
        checked=tuple(checked),
        missing=tuple(missing),
        mismatched=tuple(mismatched),
    )


def verify_model(
    manifest_path: Path,
    model_root: Path,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> IntegrityResult:
    _check_cancel(cancel_requested)
    cancel_kwargs = {} if cancel_requested is None else {"cancel_requested": cancel_requested}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError(f"Unsupported model manifest schema in {manifest_path}")
    checked: list[str] = []
    missing: list[str] = []
    mismatched: list[str] = []
    for entry in manifest["files"]:
        _check_cancel(cancel_requested)
        filename = str(entry["filename"])
        path = model_root / filename
        if not path.is_file():
            missing.append(filename)
            continue
        expected_size = int(entry["size_bytes"])
        if path.stat().st_size != expected_size:
            mismatched.append(f"{filename}: expected {expected_size} bytes, got {path.stat().st_size}")
            continue
        actual = sha256_file(path, **cancel_kwargs)
        checked.append(filename)
        if actual.lower() != str(entry["sha256"]).lower():
            mismatched.append(f"{filename}: expected {entry['sha256']}, got {actual}")
    _check_cancel(cancel_requested)
    return IntegrityResult(
        ok=not missing and not mismatched,
        checked=tuple(checked),
        missing=tuple(missing),
        mismatched=tuple(mismatched),
    )
