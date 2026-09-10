"""Deterministic delivery bundles for verified staging workspaces."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .capsule import FileChanges, VerificationResult
from .trace import TraceSeal


class PackageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: str
    sha256: str
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PackagedChange:
    kind: str
    path: str
    before: FileSnapshot | None
    after: FileSnapshot | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "before": None if self.before is None else self.before.as_dict(),
            "after": None if self.after is None else self.after.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class DeliveryBundle:
    root: str
    patch_path: str
    manifest_path: str
    verification_path: str
    handoff_path: str
    changes: tuple[PackagedChange, ...]
    patch_sha256: str
    verified: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "patch_path": self.patch_path,
            "manifest_path": self.manifest_path,
            "verification_path": self.verification_path,
            "handoff_path": self.handoff_path,
            "changes": [change.as_dict() for change in self.changes],
            "patch_sha256": self.patch_sha256,
            "verified": self.verified,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _regular_files(root: Path) -> dict[str, bytes]:
    resolved = root.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise PackageError(f"workspace is not a directory: {resolved}")
    files: dict[str, bytes] = {}
    for path in sorted(resolved.rglob("*")):
        relative = path.relative_to(resolved).as_posix()
        if any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or character in {"\u0085", "\u2028", "\u2029", "`"}
            for character in relative
        ):
            raise PackageError(f"cannot package an unsafe patch path: {relative!r}")
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise PackageError(f"cannot package a symbolic link: {relative}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise PackageError(f"cannot package a special file: {relative}")
        if path.stat().st_nlink > 1:
            raise PackageError(f"cannot package a hard-linked file: {relative}")
        files[relative] = path.read_bytes()
    return files


def _snapshot(path: str, data: bytes) -> FileSnapshot:
    return FileSnapshot(path=path, sha256=_sha256(data), size_bytes=len(data))


def _decode_text(path: str, data: bytes) -> str:
    if b"\x00" in data:
        raise PackageError(f"binary change is unsupported in M0: {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackageError(f"non-UTF-8 change is unsupported in M0: {path}") from exc


def _split_lf_lines(text: str) -> list[str]:
    """Split only on LF, as unified-diff consumers do for repository bytes."""

    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def build_workspace_patch(
    source_workspace: Path,
    candidate_workspace: Path,
) -> tuple[str, tuple[PackagedChange, ...]]:
    """Build a stable unified diff and hash manifest for all changed files."""

    before = _regular_files(source_workspace)
    after = _regular_files(candidate_workspace)
    return _build_workspace_patch_from_files(before, after)


def _build_workspace_patch_from_files(
    before: dict[str, bytes],
    after: dict[str, bytes],
) -> tuple[str, tuple[PackagedChange, ...]]:
    """Render one patch from an already captured pair of byte inventories."""

    paths = sorted(set(before) | set(after))
    patch_parts: list[str] = []
    changes: list[PackagedChange] = []
    for path in paths:
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        if old is None:
            kind = "added"
        elif new is None:
            kind = "removed"
        else:
            kind = "modified"
        change = PackagedChange(
            kind=kind,
            path=path,
            before=None if old is None else _snapshot(path, old),
            after=None if new is None else _snapshot(path, new),
        )
        changes.append(change)

        old_text = "" if old is None else _decode_text(path, old)
        new_text = "" if new is None else _decode_text(path, new)
        old_lines = _split_lf_lines(old_text)
        new_lines = _split_lf_lines(new_text)
        from_path = "/dev/null" if old is None else f"a/{path}"
        to_path = "/dev/null" if new is None else f"b/{path}"
        diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=from_path,
                tofile=to_path,
                lineterm="\n",
                n=3,
            )
        )
        # ``difflib`` preserves input newlines but its header lines use the
        # requested terminator.  A no-newline final line otherwise glues the
        # following diff token to content, so record the standard marker.
        rendered: list[str] = []
        for index, line in enumerate(diff):
            rendered.append(line)
            if (
                index >= 2
                and not line.endswith("\n")
                and line[:1] in {" ", "+", "-"}
            ):
                rendered.append("\n\\ No newline at end of file\n")
        patch_parts.extend(rendered)
    return "".join(patch_parts), tuple(changes)


def _file_changes(changes: tuple[PackagedChange, ...]) -> FileChanges:
    return FileChanges(
        added=tuple(change.path for change in changes if change.kind == "added"),
        modified=tuple(change.path for change in changes if change.kind == "modified"),
        removed=tuple(change.path for change in changes if change.kind == "removed"),
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _unclaimed_delivery_root(destination: Path) -> Path:
    supplied = destination.expanduser()
    if os.path.lexists(os.fspath(supplied)):
        raise PackageError(f"delivery destination already exists: {supplied}")
    try:
        parent = supplied.parent.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise PackageError(f"delivery destination parent is unavailable: {supplied.parent}") from exc
    if not parent.is_dir():
        raise PackageError(f"delivery destination parent is not a directory: {parent}")
    root = parent / supplied.name
    if not supplied.name or root == Path(root.anchor):
        raise PackageError("delivery destination must name a new non-root directory")
    return root


def write_delivery_bundle(
    source_workspace: Path,
    candidate_workspace: Path,
    destination: Path,
    *,
    accepted: bool,
    verification: VerificationResult,
    source_fingerprints: Mapping[str, str] | None = None,
    trace_seal: TraceSeal,
    run_metadata: dict[str, Any],
) -> DeliveryBundle:
    """Write a reviewable patch, evidence, manifest, and human handoff.

    ``accepted`` is the operator's terminal decision, not an alias for one
    verifier invocation. Failed runs may still be packaged for diagnosis even
    if a final verifier happens to pass, but they must never become promotable.
    """

    if type(accepted) is not bool:
        raise PackageError("accepted must be a boolean terminal decision")
    if accepted and not verification.ok:
        raise PackageError("an accepted delivery requires a passing verifier")

    root = _unclaimed_delivery_root(destination)

    # Capture the exact bytes used to render the patch once.  For an accepted
    # delivery, bind those bytes and their change set back to the verifier's
    # evidence before creating any delivery artifact.  This closes the ordinary
    # verify-to-package mutation window without pretending to provide OS-level
    # isolation from a concurrently hostile process.
    before = _regular_files(source_workspace)
    after = _regular_files(candidate_workspace)
    patch, changes = _build_workspace_patch_from_files(before, after)
    if accepted:
        if source_fingerprints is None:
            raise PackageError("an accepted delivery requires source fingerprints")
        expected_source_fingerprints = dict(source_fingerprints)
        actual_source_fingerprints = {
            path: _sha256(data) for path, data in sorted(before.items())
        }
        if actual_source_fingerprints != expected_source_fingerprints:
            raise PackageError("source bytes do not match ingress fingerprints")
        if not changes:
            raise PackageError("an accepted delivery requires a non-empty change set")
        actual_changes = _file_changes(changes)
        if actual_changes != verification.changes:
            raise PackageError("packaged changes do not match verifier evidence")
        actual_fingerprints = {
            path: _sha256(data) for path, data in sorted(after.items())
        }
        if actual_fingerprints != verification.candidate_fingerprints:
            raise PackageError("candidate bytes do not match verifier fingerprints")

    try:
        root.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise PackageError(
            f"delivery destination appeared during publication: {root}"
        ) from exc
    except OSError as exc:
        raise PackageError(f"cannot claim delivery destination {root}: {exc}") from exc
    patch_bytes = patch.encode("utf-8")
    patch_sha = _sha256(patch_bytes)
    patch_path = root / "changes.patch"
    verification_path = root / "verification.json"
    manifest_path = root / "manifest.json"
    handoff_path = root / "HANDOFF.md"

    manifest = {
        "schema_version": 1,
        "kind": "forge8.delivery",
        "verified": accepted,
        "patch": {
            "path": patch_path.name,
            "sha256": patch_sha,
            "size_bytes": len(patch_bytes),
        },
        "changes": [change.as_dict() for change in changes],
        "verification": {
            "path": verification_path.name,
            "status": verification.status,
            "capsule_id": verification.capsule_id,
        },
        "trace": trace_seal.as_dict(),
        "run": run_metadata,
    }
    promotion = (
        (
            "Terminal acceptance and the selected deterministic checks passed."
        )
        if accepted
        else (
            "This run was not accepted even though its final verifier passed. "
            "Do not apply this patch; inspect the run status and trace."
            if verification.ok
            else "Verification did not pass. Do not apply this patch; use the bundle for diagnosis."
        )
    )
    changed_lines = "\n".join(
        (
            f"- `{change.path}` - {change.kind}\n"
            "  - Before SHA-256: "
            f"`{change.before.sha256 if change.before is not None else 'absent'}`\n"
            "  - After SHA-256: "
            f"`{change.after.sha256 if change.after is not None else 'absent'}`"
        )
        for change in changes
    ) or "- No file changes were produced."
    byte_state_guidance = (
        (
            "This proof is bound to the exact Before and After byte states below. "
            "Before applying, require every target path to match Before; after "
            "applying, require every target path to match After. `absent` means "
            "the path must not exist. `git apply --check` alone does not establish "
            "those byte identities. Review the patch and rerun the selected checks "
            "after the byte checks pass."
        )
        if accepted
        else (
            "The byte states below are diagnostic evidence only. Do not apply this "
            "patch."
        )
    )
    isolation = str(run_metadata.get("isolation", "not recorded"))
    network_enforced = run_metadata.get("network_isolation_enforced")
    network_text = (
        "yes"
        if network_enforced is True
        else "no"
        if network_enforced is False
        else "not recorded"
    )
    handoff = (
        "# Forge8 handoff\n\n"
        f"- Result: **{'VERIFIED' if accepted else 'NOT VERIFIED'}**\n"
        f"- Task: `{verification.capsule_id}`\n"
        f"- Verifier: `{verification.status}`\n"
        f"- Isolation: `{isolation}`\n"
        f"- Network isolation enforced: `{network_text}`\n"
        f"- Patch SHA-256: `{patch_sha}`\n\n"
        f"{promotion}\n\n"
        f"{byte_state_guidance}\n\n"
        "Use the JSON booleans `manifest.json: verified` and "
        "`verification.json: ok` for the machine-readable promotion decision. "
        "Do not decide by searching this Markdown for the word VERIFIED.\n\n"
        "This result covers only the selected checks and Forge8's package/lifecycle "
        "gates; it is not proof of every meaning in the natural-language goal. Project "
        "tests run with the current user's privileges, so use trusted code only.\n\n"
        "## Changed files\n\n"
        f"{changed_lines}\n"
    ).encode("utf-8")

    try:
        _atomic_write(patch_path, patch_bytes)
        _atomic_write(verification_path, _json_bytes(verification.as_dict()))
        _atomic_write(manifest_path, _json_bytes(manifest))
        _atomic_write(handoff_path, handoff)
    except BaseException:
        # Leave any fully written files as honest partial evidence; the absence
        # of manifest.json means the bundle is not complete/promotable.
        raise
    return DeliveryBundle(
        root=str(root),
        patch_path=str(patch_path),
        manifest_path=str(manifest_path),
        verification_path=str(verification_path),
        handoff_path=str(handoff_path),
        changes=changes,
        patch_sha256=patch_sha,
        verified=accepted,
    )
