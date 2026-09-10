"""Hermetic task-capsule loading, policy checks, and verifier execution.

Capsules are deliberately model-agnostic.  A candidate workspace can come from
Forge8, another local agent, or a human; the same hidden verifier and file
policy decide whether the artifact is accepted.  This module provides the
portable process-level runner.  OS-level network and process isolation belongs
to the sandbox backend and is reported explicitly rather than implied here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal


_CAPSULE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_NETWORK_POLICIES = frozenset({"deny", "allow"})
_WORKSPACE_ACCESS = frozenset({"read_only", "read_write"})


class CapsuleError(ValueError):
    """Raised when a capsule is malformed or unsafe to load."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapsuleError(f"{label} must be a JSON object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise CapsuleError(f"{label} must be a non-empty string")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CapsuleError(f"{label} must be a positive integer")
    return value


def _relative_path(value: Any, label: str) -> PurePosixPath:
    raw = _require_string(value, label)
    if "\\" in raw:
        raise CapsuleError(f"{label} must use POSIX separators")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CapsuleError(f"{label} must be a normalized relative path")
    return path


def _inside(root: Path, relative: PurePosixPath, *, kind: Literal["file", "dir", "any"]) -> Path:
    candidate = (root / Path(*relative.parts)).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CapsuleError(f"capsule path escapes its root: {relative}") from exc
    if kind == "file" and not candidate.is_file():
        raise CapsuleError(f"capsule file is missing: {relative}")
    if kind == "dir" and not candidate.is_dir():
        raise CapsuleError(f"capsule directory is missing: {relative}")
    return candidate


@dataclass(frozen=True, slots=True)
class CapsulePolicy:
    network: str
    workspace_access: str
    allowed_write_roots: tuple[str, ...]
    stdlib_only: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CapsuleBudget:
    wall_time_seconds: int
    max_processes: int
    max_output_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VerifierSpec:
    command: tuple[str, ...]
    success_exit_code: int
    network_required: bool

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["command"] = list(self.command)
        return payload


@dataclass(frozen=True, slots=True)
class CapsuleManifest:
    root: Path
    schema_version: str
    capsule_id: str
    title: str
    domain: str
    task_type: str
    difficulty: str
    prompt: str
    workspace: str
    policy: CapsulePolicy
    budget: CapsuleBudget
    verifier: VerifierSpec
    required_artifacts: tuple[str, ...]
    tags: tuple[str, ...]

    @property
    def prompt_path(self) -> Path:
        return _inside(self.root, PurePosixPath(self.prompt), kind="file")

    @property
    def workspace_path(self) -> Path:
        return _inside(self.root, PurePosixPath(self.workspace), kind="dir")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.capsule_id,
            "title": self.title,
            "domain": self.domain,
            "task_type": self.task_type,
            "difficulty": self.difficulty,
            "prompt": self.prompt,
            "workspace": self.workspace,
            "policy": self.policy.as_dict(),
            "budget": self.budget.as_dict(),
            "verifier": self.verifier.as_dict(),
            "required_artifacts": list(self.required_artifacts),
            "tags": list(self.tags),
        }


def load_capsule(path: str | os.PathLike[str]) -> CapsuleManifest:
    """Load and fully validate one capsule manifest and its local references."""

    supplied = Path(path).expanduser().resolve(strict=True)
    manifest_path = supplied / "capsule.json" if supplied.is_dir() else supplied
    if not manifest_path.is_file() or manifest_path.name != "capsule.json":
        raise CapsuleError(f"expected a capsule.json file or capsule directory: {supplied}")
    root = manifest_path.parent.resolve(strict=True)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapsuleError(f"cannot read capsule manifest {manifest_path}: {exc}") from exc
    payload = _require_object(payload, "capsule manifest")

    schema_version = _require_string(payload.get("schema_version"), "schema_version")
    if schema_version != "0.1":
        raise CapsuleError(f"unsupported capsule schema_version: {schema_version}")
    capsule_id = _require_string(payload.get("id"), "id")
    if not _CAPSULE_ID.fullmatch(capsule_id):
        raise CapsuleError("id must be a lowercase dotted identifier")

    prompt = _relative_path(payload.get("prompt"), "prompt")
    workspace = _relative_path(payload.get("workspace"), "workspace")
    _inside(root, prompt, kind="file")
    workspace_path = _inside(root, workspace, kind="dir")

    raw_policy = _require_object(payload.get("policy"), "policy")
    network = _require_string(raw_policy.get("network"), "policy.network")
    if network not in _NETWORK_POLICIES:
        raise CapsuleError(f"unsupported network policy: {network}")
    workspace_access = _require_string(
        raw_policy.get("workspace_access"), "policy.workspace_access"
    )
    if workspace_access not in _WORKSPACE_ACCESS:
        raise CapsuleError(f"unsupported workspace access: {workspace_access}")
    if not isinstance(raw_policy.get("stdlib_only"), bool):
        raise CapsuleError("policy.stdlib_only must be a boolean")
    raw_write_roots = raw_policy.get("allowed_write_roots")
    if not isinstance(raw_write_roots, list):
        raise CapsuleError("policy.allowed_write_roots must be an array")
    write_roots = tuple(
        _relative_path(value, f"policy.allowed_write_roots[{index}]")
        for index, value in enumerate(raw_write_roots)
    )
    for write_root in write_roots:
        if write_root != workspace and workspace not in write_root.parents:
            raise CapsuleError(f"allowed write root is outside the workspace: {write_root}")
    if workspace_access == "read_only" and write_roots:
        raise CapsuleError("read-only capsules cannot declare allowed write roots")
    if workspace_access == "read_write" and not write_roots:
        raise CapsuleError("read-write capsules need at least one allowed write root")

    raw_budget = _require_object(payload.get("budget"), "budget")
    budget = CapsuleBudget(
        wall_time_seconds=_positive_int(
            raw_budget.get("wall_time_seconds"), "budget.wall_time_seconds"
        ),
        max_processes=_positive_int(raw_budget.get("max_processes"), "budget.max_processes"),
        max_output_bytes=_positive_int(
            raw_budget.get("max_output_bytes"), "budget.max_output_bytes"
        ),
    )

    raw_verifier = _require_object(payload.get("verifier"), "verifier")
    raw_command = raw_verifier.get("command")
    if not isinstance(raw_command, list) or not raw_command:
        raise CapsuleError("verifier.command must be a non-empty array")
    command = tuple(
        _require_string(value, f"verifier.command[{index}]")
        for index, value in enumerate(raw_command)
    )
    success_exit_code = raw_verifier.get("success_exit_code")
    if isinstance(success_exit_code, bool) or not isinstance(success_exit_code, int):
        raise CapsuleError("verifier.success_exit_code must be an integer")
    network_required = raw_verifier.get("network_required")
    if not isinstance(network_required, bool):
        raise CapsuleError("verifier.network_required must be a boolean")
    if network == "deny" and network_required:
        raise CapsuleError("a network-denied capsule cannot require network verification")

    raw_required = payload.get("required_artifacts")
    if not isinstance(raw_required, list):
        raise CapsuleError("required_artifacts must be an array")
    required = tuple(
        _relative_path(value, f"required_artifacts[{index}]")
        for index, value in enumerate(raw_required)
    )
    for artifact in required:
        if artifact != workspace and workspace not in artifact.parents:
            raise CapsuleError(f"required artifact is outside the workspace: {artifact}")

    raw_tags = payload.get("tags")
    if not isinstance(raw_tags, list) or not all(
        isinstance(tag, str) and tag and "\x00" not in tag for tag in raw_tags
    ):
        raise CapsuleError("tags must be an array of non-empty strings")

    # Traverse now so unsafe fixture links are rejected before any copy or run.
    _fingerprint_tree(workspace_path)
    verifier_dir = root / "verifier"
    if verifier_dir.exists():
        _fingerprint_tree(verifier_dir.resolve(strict=True))

    return CapsuleManifest(
        root=root,
        schema_version=schema_version,
        capsule_id=capsule_id,
        title=_require_string(payload.get("title"), "title"),
        domain=_require_string(payload.get("domain"), "domain"),
        task_type=_require_string(payload.get("task_type"), "task_type"),
        difficulty=_require_string(payload.get("difficulty"), "difficulty"),
        prompt=prompt.as_posix(),
        workspace=workspace.as_posix(),
        policy=CapsulePolicy(
            network=network,
            workspace_access=workspace_access,
            allowed_write_roots=tuple(item.as_posix() for item in write_roots),
            stdlib_only=raw_policy["stdlib_only"],
        ),
        budget=budget,
        verifier=VerifierSpec(command, success_exit_code, network_required),
        required_artifacts=tuple(item.as_posix() for item in required),
        tags=tuple(raw_tags),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint_tree(root: Path) -> dict[str, str]:
    """Hash a regular-file tree and reject symlinks and special files."""

    root = root.resolve(strict=True)
    if not root.is_dir():
        raise CapsuleError(f"expected directory: {root}")
    fingerprints: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise CapsuleError(f"symlinks are not allowed in capsule trees: {relative}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise CapsuleError(f"special files are not allowed in capsule trees: {relative}")
        fingerprints[relative] = _sha256_file(path)
    return fingerprints


def _copy_regular_tree(source: Path, destination: Path) -> None:
    _fingerprint_tree(source)
    if destination.exists():
        raise CapsuleError(f"destination already exists: {destination}")
    shutil.copytree(source, destination, symlinks=False)


@dataclass(frozen=True, slots=True)
class PreparedCapsule:
    capsule_id: str
    workspace: str
    prompt: str
    source_fingerprints: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def prepare_capsule(manifest: CapsuleManifest, destination: Path) -> PreparedCapsule:
    """Copy only the model-visible prompt/workspace, never the hidden verifier."""

    destination = destination.expanduser().resolve()
    if destination.exists():
        raise CapsuleError(f"destination already exists: {destination}")
    destination.mkdir(parents=True)
    workspace_destination = destination / "workspace"
    try:
        _copy_regular_tree(manifest.workspace_path, workspace_destination)
        prompt_text = manifest.prompt_path.read_text(encoding="utf-8")
        (destination / "prompt.md").write_text(prompt_text, encoding="utf-8")
        fingerprints = _fingerprint_tree(workspace_destination)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return PreparedCapsule(
        capsule_id=manifest.capsule_id,
        workspace=str(workspace_destination),
        prompt=str(destination / "prompt.md"),
        source_fingerprints=fingerprints,
    )


@dataclass(frozen=True, slots=True)
class FileChanges:
    added: tuple[str, ...]
    modified: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def all(self) -> tuple[str, ...]:
        return self.added + self.modified + self.removed

    def as_dict(self) -> dict[str, Any]:
        return {"added": list(self.added), "modified": list(self.modified), "removed": list(self.removed)}


def _changes(before: dict[str, str], after: dict[str, str]) -> FileChanges:
    old = set(before)
    new = set(after)
    return FileChanges(
        added=tuple(sorted(new - old)),
        modified=tuple(sorted(path for path in old & new if before[path] != after[path])),
        removed=tuple(sorted(old - new)),
    )


def _workspace_relative_roots(manifest: CapsuleManifest) -> tuple[PurePosixPath, ...]:
    workspace = PurePosixPath(manifest.workspace)
    roots: list[PurePosixPath] = []
    for raw in manifest.policy.allowed_write_roots:
        path = PurePosixPath(raw)
        roots.append(PurePosixPath(".") if path == workspace else path.relative_to(workspace))
    return tuple(roots)


def _path_allowed(path: str, roots: tuple[PurePosixPath, ...]) -> bool:
    candidate = PurePosixPath(path)
    for root in roots:
        if str(root) == "." or candidate == root or root in candidate.parents:
            return True
    return False


def _last_json_object(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _bounded_text(value: str | bytes | None, limit: int) -> tuple[str, bool]:
    if value is None:
        return "", False
    if isinstance(value, bytes):
        raw = value
    else:
        raw = value.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return raw.decode("utf-8", errors="replace"), False
    suffix = b"\n...[output truncated by Forge8]"
    return (raw[: max(0, limit - len(suffix))] + suffix).decode("utf-8", errors="replace"), True


@dataclass(frozen=True, slots=True)
class VerificationResult:
    capsule_id: str
    status: str
    ok: bool
    started_at: str
    ended_at: str
    duration_seconds: float
    return_code: int | None
    timed_out: bool
    launch_error: str | None
    changes: FileChanges
    policy_violations: tuple[str, ...]
    required_artifacts_missing: tuple[str, ...]
    stdout: str
    stderr: str
    output_truncated: bool
    verifier_summary: dict[str, Any] | None
    candidate_fingerprints: dict[str, str]
    isolation: str = "process_only"
    network_isolation_enforced: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "capsule_id": self.capsule_id,
            "status": self.status,
            "ok": self.ok,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "return_code": self.return_code,
            "timed_out": self.timed_out,
            "launch_error": self.launch_error,
            "changes": self.changes.as_dict(),
            "policy_violations": list(self.policy_violations),
            "required_artifacts_missing": list(self.required_artifacts_missing),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "output_truncated": self.output_truncated,
            "verifier_summary": self.verifier_summary,
            "candidate_fingerprints": self.candidate_fingerprints,
            "isolation": self.isolation,
            "network_isolation_enforced": self.network_isolation_enforced,
        }


def verify_candidate(
    manifest: CapsuleManifest,
    candidate_workspace: Path,
    *,
    timeout_seconds: float | None = None,
    environment: dict[str, str] | None = None,
) -> VerificationResult:
    """Policy-check a candidate, then run the capsule's trusted verifier.

    The verifier is copied beside a copy of the candidate so it never mutates
    either the submitted workspace or the source fixture.  This portable runner
    does not enforce network/process isolation; callers must wrap it in a
    sandbox backend before describing a run as sealed.
    """

    started_at = _utc_now()
    started = time.monotonic()
    candidate = candidate_workspace.expanduser().resolve(strict=True)
    before = _fingerprint_tree(manifest.workspace_path)
    after = _fingerprint_tree(candidate)
    changes = _changes(before, after)
    allowed_roots = _workspace_relative_roots(manifest)
    violations = tuple(
        f"write outside allowed roots: {path}"
        for path in changes.all
        if not _path_allowed(path, allowed_roots)
    )

    workspace_prefix = PurePosixPath(manifest.workspace)
    missing: list[str] = []
    for raw in manifest.required_artifacts:
        artifact = PurePosixPath(raw)
        relative = PurePosixPath(".") if artifact == workspace_prefix else artifact.relative_to(workspace_prefix)
        target = candidate if str(relative) == "." else candidate / Path(*relative.parts)
        if not target.is_file() or target.is_symlink():
            missing.append(raw)

    if violations or missing:
        ended_at = _utc_now()
        return VerificationResult(
            capsule_id=manifest.capsule_id,
            status="policy_failed",
            ok=False,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=max(0.0, time.monotonic() - started),
            return_code=None,
            timed_out=False,
            launch_error=None,
            changes=changes,
            policy_violations=violations,
            required_artifacts_missing=tuple(missing),
            stdout="",
            stderr="",
            output_truncated=False,
            verifier_summary=None,
            candidate_fingerprints=after,
        )

    selected_timeout = float(
        manifest.budget.wall_time_seconds
        if timeout_seconds is None
        else timeout_seconds
    )
    if selected_timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    stdout_raw: str | bytes | None = ""
    stderr_raw: str | bytes | None = ""
    return_code: int | None = None
    timed_out = False
    launch_error: str | None = None

    with tempfile.TemporaryDirectory(prefix="forge8-verify-") as temporary:
        run_root = Path(temporary)
        submitted_workspace = run_root / manifest.workspace
        submitted_workspace.parent.mkdir(parents=True, exist_ok=True)
        _copy_regular_tree(candidate, submitted_workspace)
        verifier_source = manifest.root / "verifier"
        if not verifier_source.is_dir():
            raise CapsuleError(f"capsule verifier directory is missing: {verifier_source}")
        _copy_regular_tree(verifier_source, run_root / "verifier")
        argv = list(manifest.verifier.command)
        if argv[0].casefold() in {"python", "python3", "python.exe", "python3.exe"}:
            # Capsule authors name the runtime semantically. Execute with the
            # already trusted Forge8 interpreter so the same capsule works on
            # Linux and native Windows without PATH aliases or store shims.
            argv[0] = str(Path(sys.executable).resolve())
        child_environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
        }
        if environment:
            child_environment.update(environment)
        try:
            completed = subprocess.run(
                argv,
                cwd=run_root,
                env=child_environment,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=selected_timeout,
                check=False,
            )
            return_code = completed.returncode
            stdout_raw = completed.stdout
            stderr_raw = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout_raw = exc.stdout or exc.output
            stderr_raw = exc.stderr
        except OSError as exc:
            launch_error = f"{type(exc).__name__}: {exc}"

    output_limit = manifest.budget.max_output_bytes
    stdout, stdout_truncated = _bounded_text(stdout_raw, output_limit)
    stderr, stderr_truncated = _bounded_text(stderr_raw, output_limit)
    passed = (
        not timed_out
        and launch_error is None
        and return_code == manifest.verifier.success_exit_code
    )
    status = (
        "passed"
        if passed
        else "timed_out"
        if timed_out
        else "launch_error"
        if launch_error is not None
        else "failed"
    )
    ended_at = _utc_now()
    return VerificationResult(
        capsule_id=manifest.capsule_id,
        status=status,
        ok=passed,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=max(0.0, time.monotonic() - started),
        return_code=return_code,
        timed_out=timed_out,
        launch_error=launch_error,
        changes=changes,
        policy_violations=violations,
        required_artifacts_missing=tuple(missing),
        stdout=stdout,
        stderr=stderr,
        output_truncated=stdout_truncated or stderr_truncated,
        verifier_summary=_last_json_object(stdout),
        candidate_fingerprints=after,
    )
