"""Generic Python-repository adapter for Forge8's single operator loop.

This module is deliberately an adapter, not another agent framework.  It turns
one sanitized repository into :class:`PreparedWorkspaceTask`, provides a
server-free failing-baseline preflight, and supplies structural clean-copy check
and replay verification callbacks to the existing operator.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Literal

from .capsule import FileChanges, VerificationResult
from .checks import CHECK_REGISTRY, CheckResult, CheckRunner
from .operator import CheckExecutor, PreparedWorkspaceTask
from .package import PackagedChange, build_workspace_patch
from .repository import (
    RepositoryFileFingerprint,
    RepositorySnapshot,
    prepare_repository_snapshot,
    validate_allowed_write_paths,
)
from .workspace import ArtifactStore, WorkspacePolicy, WorkspaceTools


MAX_REPAIR_GOAL_CHARS = 1_200
_MAX_RESULT_TEXT_CHARS = 8_000
_MAX_CHECK_PREVIEW_CHARS = 2_000
BaselineStatus = Literal["repairable", "already_passing", "invalid"]


class RepositoryRepairError(ValueError):
    """Raised when a repository repair cannot be prepared safely."""


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


@dataclass(frozen=True, slots=True)
class BaselinePreflight:
    """Server-free admission evidence for one exact check subset."""

    status: BaselineStatus
    repairable: bool
    checks: tuple[CheckResult, ...]
    reason: str | None


@dataclass(frozen=True, slots=True)
class PreparedRepositoryRepair:
    """The generic repository ingress plus its shared operator task."""

    run_root: Path
    task: PreparedWorkspaceTask
    ingress: RepositorySnapshot
    allowed_write_roots: tuple[str, ...]
    check_ids: tuple[str, ...]
    check_timeout_seconds: float
    check_output_bytes: int

    def baseline_preflight(self) -> BaselinePreflight:
        """Prove a selected check fails before a local model/server is started."""

        return _run_baseline_preflight(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clip(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = "\n…[bounded by Forge8]"
    if limit <= len(marker):
        return marker[-limit:], True
    return value[: limit - len(marker)] + marker, True


def _fingerprint_map(
    fingerprints: tuple[RepositoryFileFingerprint, ...],
) -> dict[str, str]:
    return {item.path: item.sha256 for item in fingerprints}


def _snapshot_matches_ingress(
    snapshot: RepositorySnapshot,
    ingress: RepositorySnapshot,
    *,
    expected_excluded: tuple[str, ...],
) -> bool:
    return (
        snapshot.fingerprints == ingress.fingerprints
        and snapshot.excluded == expected_excluded
    )


def _strict_goal(goal: object) -> str:
    if not isinstance(goal, str) or not goal.strip() or "\x00" in goal:
        raise RepositoryRepairError("goal must be non-blank text without NUL bytes")
    if len(goal) > MAX_REPAIR_GOAL_CHARS:
        raise RepositoryRepairError(
            f"goal exceeds the {MAX_REPAIR_GOAL_CHARS}-character limit"
        )
    try:
        goal.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RepositoryRepairError("goal must be valid UTF-8 text") from exc
    return goal


def _strict_task_id(task_id: object) -> str:
    if (
        not isinstance(task_id, str)
        or not task_id
        or task_id != task_id.strip()
        or "\x00" in task_id
    ):
        raise RepositoryRepairError(
            "task_id must be an exact non-empty string without surrounding whitespace"
        )
    try:
        task_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RepositoryRepairError("task_id must be valid UTF-8 text") from exc
    return task_id


def _strict_check_ids(check_ids: Iterable[str]) -> tuple[str, ...]:
    if isinstance(check_ids, (str, bytes)):
        raise RepositoryRepairError("check_ids must be a non-empty iterable of registry IDs")
    try:
        selected = tuple(check_ids)
    except TypeError as exc:
        raise RepositoryRepairError("check_ids must be iterable") from exc
    if not selected:
        raise RepositoryRepairError("at least one check ID is required")
    for check_id in selected:
        if not isinstance(check_id, str) or not check_id or "\x00" in check_id:
            raise RepositoryRepairError("check IDs must be exact non-empty strings")
        if check_id not in CHECK_REGISTRY:
            raise RepositoryRepairError(f"unknown check ID: {check_id!r}")
    if len(set(selected)) != len(selected):
        raise RepositoryRepairError("check_ids must not contain duplicates")
    return selected


def _strict_check_limits(
    timeout_seconds: object, output_bytes: object
) -> tuple[float, int]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise RepositoryRepairError("check_timeout_seconds must be positive")
    if (
        isinstance(output_bytes, bool)
        or not isinstance(output_bytes, int)
        or output_bytes < 1
    ):
        raise RepositoryRepairError("check_output_bytes must be a positive integer")
    return float(timeout_seconds), output_bytes


def _prepare_run_root(raw_root: str | os.PathLike[str]) -> Path:
    supplied = Path(raw_root).expanduser()
    if os.path.lexists(os.fspath(supplied)):
        try:
            metadata = supplied.lstat()
        except OSError as exc:
            raise RepositoryRepairError(f"cannot inspect run root {supplied}: {exc}") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or _is_reparse_point(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise RepositoryRepairError("run root must be a regular directory, not a link")
        root = supplied.resolve(strict=True)
        if any(root.iterdir()):
            raise RepositoryRepairError(f"repair run root is not empty: {root}")
    else:
        try:
            parent = supplied.parent.resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise RepositoryRepairError(
                f"repair run root parent is unavailable: {supplied.parent}"
            ) from exc
        if not parent.is_dir() or not supplied.name:
            raise RepositoryRepairError("repair run root must name a new directory")
        root = parent / supplied.name
        try:
            root.mkdir(exist_ok=False)
        except OSError as exc:
            raise RepositoryRepairError(f"cannot create repair run root {root}: {exc}") from exc
    if root == Path(root.anchor):
        raise RepositoryRepairError("repair run root cannot be a filesystem root")
    return root


def _assert_disjoint_source_and_run_root(
    source_repo: str | os.PathLike[str],
    raw_run_root: str | os.PathLike[str],
) -> None:
    try:
        source = Path(source_repo).expanduser().resolve(strict=True)
        supplied_run = Path(raw_run_root).expanduser()
        if os.path.lexists(os.fspath(supplied_run)):
            run = supplied_run.resolve(strict=True)
        else:
            run = supplied_run.parent.resolve(strict=True) / supplied_run.name
    except (OSError, ValueError) as exc:
        raise RepositoryRepairError(
            "cannot resolve source repository and repair run root"
        ) from exc
    if source == run or source in run.parents or run in source.parents:
        raise RepositoryRepairError(
            "repair run root and original source repository must not overlap"
        )


def _new_artifact_store(parent: Path, *, prefix: str) -> ArtifactStore:
    parent.mkdir(parents=True, exist_ok=True)
    unique = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    return ArtifactStore(unique)


def _empty_check_result(
    check_id: str,
    artifacts: ArtifactStore,
    *,
    output_limit_bytes: int,
    started: float,
    error: str,
) -> CheckResult:
    bounded_error, _ = _clip(error, _MAX_CHECK_PREVIEW_CHARS)
    empty = artifacts.put_bytes(b"")
    return CheckResult(
        check_id=check_id,
        status="unavailable",
        ok=False,
        return_code=None,
        timed_out=False,
        duration_seconds=max(0.0, time.monotonic() - started),
        command=(),
        cwd=None,
        environment_keys=(),
        stdout_artifact=empty,
        stderr_artifact=empty,
        stdout_preview="",
        stderr_preview="",
        stdout_observed_bytes=0,
        stderr_observed_bytes=0,
        stdout_captured_bytes=0,
        stderr_captured_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        stdout_preview_truncated=False,
        stderr_preview_truncated=False,
        output_limit_bytes=output_limit_bytes,
        partial_output=False,
        capture_errors=(),
        error=bounded_error,
    )


def _directory_inventory(root: Path) -> tuple[str, ...]:
    directories: list[str] = []
    for candidate in sorted(root.rglob("*")):
        metadata = candidate.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(candidate.relative_to(root).as_posix())
    return tuple(directories)


def _invalidate_check(result: CheckResult, message: str) -> CheckResult:
    bounded, _ = _clip(message, _MAX_CHECK_PREVIEW_CHARS)
    return replace(result, status="unavailable", ok=False, error=bounded)


class _DisposableCheckExecutor:
    """Run every check on a fresh sanitized copy of the current workspace."""

    def __init__(
        self,
        candidate: Path,
        check_ids: tuple[str, ...],
        artifacts: ArtifactStore,
        *,
        timeout_seconds: float,
        output_bytes: int,
    ) -> None:
        self.candidate = candidate.resolve(strict=True)
        self.check_ids = check_ids
        self.artifacts = artifacts
        self.timeout_seconds = timeout_seconds
        self.output_bytes = output_bytes

    def run(self, check_id: str) -> CheckResult:
        started = time.monotonic()
        if check_id not in self.check_ids:
            return _empty_check_result(
                str(check_id),
                self.artifacts,
                output_limit_bytes=self.output_bytes,
                started=started,
                error=f"check is outside the exact task subset: {check_id!r}",
            )
        try:
            with tempfile.TemporaryDirectory(prefix="forge8-repair-check-") as temporary:
                temporary_root = Path(temporary)
                clean_root = temporary_root / "workspace"
                candidate_before = prepare_repository_snapshot(self.candidate, clean_root)
                if candidate_before.excluded:
                    raise RepositoryRepairError(
                        "candidate contains excluded paths: "
                        + repr(list(candidate_before.excluded))
                    )
                directories_before = _directory_inventory(clean_root)
                policy = WorkspacePolicy(
                    clean_root,
                    allow_write=False,
                    allow_hidden=False,
                    max_output_chars=16_000,
                )
                result = CheckRunner(
                    policy,
                    self.artifacts,
                    max_output_bytes=self.output_bytes,
                    max_timeout_seconds=self.timeout_seconds,
                ).run(check_id)
                clean_after = prepare_repository_snapshot(
                    clean_root, temporary_root / "workspace-after"
                )
                candidate_after = prepare_repository_snapshot(
                    self.candidate, temporary_root / "candidate-after"
                )
                if (
                    clean_after.excluded
                    or clean_after.fingerprints != candidate_before.fingerprints
                    or _directory_inventory(Path(clean_after.snapshot_root))
                    != directories_before
                ):
                    return _invalidate_check(
                        result,
                        "check mutated its disposable workspace; result is not admissible",
                    )
                if (
                    candidate_after.excluded != candidate_before.excluded
                    or candidate_after.fingerprints != candidate_before.fingerprints
                ):
                    return _invalidate_check(
                        result,
                        "candidate changed while its disposable check executed",
                    )
                return result
        except Exception as exc:
            return _empty_check_result(
                check_id,
                self.artifacts,
                output_limit_bytes=self.output_bytes,
                started=started,
                error=(
                    "candidate sanitation failed before check execution: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )


def _check_executor_factory(
    candidate: Path,
    check_ids: tuple[str, ...],
    timeout_seconds: float,
    output_bytes: int,
):
    expected = candidate.resolve(strict=True)

    def factory(policy: WorkspacePolicy, artifacts: ArtifactStore) -> CheckExecutor:
        if policy.root != expected:
            raise RepositoryRepairError("operator supplied an unexpected candidate workspace")
        return _DisposableCheckExecutor(
            expected,
            check_ids,
            artifacts,
            timeout_seconds=timeout_seconds,
            output_bytes=output_bytes,
        )

    return factory


def _path_allowed(path: str, roots: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path)
    return any(
        candidate == PurePosixPath(root) or PurePosixPath(root) in candidate.parents
        for root in roots
    )


def _file_changes(changes: tuple[PackagedChange, ...]) -> FileChanges:
    return FileChanges(
        added=tuple(change.path for change in changes if change.kind == "added"),
        modified=tuple(change.path for change in changes if change.kind == "modified"),
        removed=tuple(change.path for change in changes if change.kind == "removed"),
    )


def _checked_file_bytes(root: Path, path: str) -> bytes:
    target = root.joinpath(*PurePosixPath(path).parts)
    metadata = target.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RepositoryRepairError(f"replay path is not a regular file: {path}")
    if metadata.st_nlink != 1:
        raise RepositoryRepairError(f"replay path is hard-linked: {path}")
    return target.read_bytes()


def _assert_file_snapshot(
    data: bytes, expected_sha256: str, expected_size: int, path: str
) -> None:
    if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise RepositoryRepairError(f"guarded replay preimage mismatch: {path}")


def _replay_changes(
    source: Path,
    candidate_capture: Path,
    replay: Path,
    changes: tuple[PackagedChange, ...],
    artifact_root: Path,
) -> None:
    tools = WorkspaceTools(
        WorkspacePolicy(
            replay,
            allow_write=True,
            allow_hidden=False,
            max_read_bytes=256 * 1024,
            max_write_bytes=256 * 1024,
        ),
        ArtifactStore(artifact_root),
    )
    for change in changes:
        target = replay.joinpath(*PurePosixPath(change.path).parts)
        if change.before is None:
            if os.path.lexists(target):
                raise RepositoryRepairError(
                    f"guarded replay expected a missing target: {change.path}"
                )
            guard = "missing"
        else:
            before = _checked_file_bytes(source, change.path)
            _assert_file_snapshot(
                before,
                change.before.sha256,
                change.before.size_bytes,
                change.path,
            )
            replay_before = _checked_file_bytes(replay, change.path)
            _assert_file_snapshot(
                replay_before,
                change.before.sha256,
                change.before.size_bytes,
                change.path,
            )
            guard = change.before.sha256

        if change.after is None:
            if change.before is None:
                raise RepositoryRepairError(f"invalid empty replay delta: {change.path}")
            target.unlink()
            if os.path.lexists(target):
                raise RepositoryRepairError(f"guarded replay removal failed: {change.path}")
            continue

        after = _checked_file_bytes(candidate_capture, change.path)
        _assert_file_snapshot(
            after,
            change.after.sha256,
            change.after.size_bytes,
            change.path,
        )
        try:
            content = after.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepositoryRepairError(
                f"guarded replay effect is not UTF-8: {change.path}"
            ) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        written = tools.write_text(change.path, content, expected_sha256=guard)
        if not written.ok:
            raise RepositoryRepairError(
                f"guarded replay write failed for {change.path}: {written.error}"
            )
        replay_after = _checked_file_bytes(replay, change.path)
        _assert_file_snapshot(
            replay_after,
            change.after.sha256,
            change.after.size_bytes,
            change.path,
        )


def _check_summary(result: CheckResult) -> tuple[dict[str, object], bool]:
    stdout, stdout_clipped = _clip(result.stdout_preview, _MAX_CHECK_PREVIEW_CHARS)
    stderr, stderr_clipped = _clip(result.stderr_preview, _MAX_CHECK_PREVIEW_CHARS)
    error = None
    error_clipped = False
    if result.error is not None:
        error, error_clipped = _clip(result.error, _MAX_CHECK_PREVIEW_CHARS)
    return (
        {
            "check_id": result.check_id,
            "status": result.status,
            "ok": result.ok,
            "return_code": result.return_code,
            "timed_out": result.timed_out,
            "stdout_preview": stdout,
            "stderr_preview": stderr,
            "output_truncated": result.output_truncated,
            "error": error,
        },
        stdout_clipped
        or stderr_clipped
        or error_clipped
        or result.output_truncated,
    )


def _verification_result(
    *,
    task_id: str,
    started_at: str,
    started: float,
    status: str,
    ok: bool,
    stage: str,
    message: str,
    changes: FileChanges,
    candidate_fingerprints: dict[str, str],
    checks: tuple[CheckResult, ...] = (),
    policy_violations: tuple[str, ...] = (),
) -> VerificationResult:
    rendered_checks: list[dict[str, object]] = []
    output_truncated = False
    for check in checks:
        rendered, truncated = _check_summary(check)
        rendered_checks.append(rendered)
        output_truncated = output_truncated or truncated
    bounded_message, message_clipped = _clip(message, _MAX_RESULT_TEXT_CHARS)
    summary = {
        "task_id": task_id,
        "passed": ok,
        "stage": stage,
        "message": bounded_message,
        "changes": changes.as_dict(),
        "checks": rendered_checks,
    }
    stdout, stdout_clipped = _clip(
        json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n",
        _MAX_RESULT_TEXT_CHARS,
    )
    failing_check = next((check for check in checks if not check.ok), None)
    launch_error = None
    if failing_check is not None and failing_check.status in {"launch_error", "unavailable"}:
        launch_error = failing_check.error
    return VerificationResult(
        capsule_id=task_id,
        status=status,
        ok=ok,
        started_at=started_at,
        ended_at=_utc_now(),
        duration_seconds=max(0.0, time.monotonic() - started),
        return_code=0 if ok else None if failing_check is None else failing_check.return_code,
        timed_out=any(check.timed_out for check in checks),
        launch_error=launch_error,
        changes=changes,
        policy_violations=policy_violations,
        required_artifacts_missing=(),
        stdout=stdout,
        stderr="" if ok else bounded_message,
        output_truncated=(
            output_truncated or message_clipped or stdout_clipped
        ),
        verifier_summary=summary,
        candidate_fingerprints=candidate_fingerprints,
        isolation="process_only",
        network_isolation_enforced=False,
    )


def _verify_repository_candidate(
    *,
    task_id: str,
    original_source: Path,
    immutable_source: Path,
    candidate: Path,
    ingress: RepositorySnapshot,
    allowed_roots: tuple[str, ...],
    check_ids: tuple[str, ...],
    timeout_seconds: float,
    output_bytes: int,
    artifact_root: Path,
) -> VerificationResult:
    started_at = _utc_now()
    started = time.monotonic()
    empty_changes = FileChanges((), (), ())
    changes = empty_changes
    candidate_fingerprints: dict[str, str] = {}
    checks: tuple[CheckResult, ...] = ()

    def fail(stage: str, message: str, *, status: str = "policy_failed") -> VerificationResult:
        bounded_message, _ = _clip(message, _MAX_RESULT_TEXT_CHARS)
        violations = (bounded_message,) if status == "policy_failed" else ()
        return _verification_result(
            task_id=task_id,
            started_at=started_at,
            started=started,
            status=status,
            ok=False,
            stage=stage,
            message=bounded_message,
            changes=changes,
            candidate_fingerprints=candidate_fingerprints,
            checks=checks,
            policy_violations=violations,
        )

    try:
        with tempfile.TemporaryDirectory(prefix="forge8-repair-verify-") as temporary:
            root = Path(temporary)
            live_before = prepare_repository_snapshot(original_source, root / "live-before")
            if not _snapshot_matches_ingress(
                live_before, ingress, expected_excluded=ingress.excluded
            ):
                return fail("live_source_before", "live source changed after repository ingress")

            source_before = prepare_repository_snapshot(
                immutable_source, root / "source-before"
            )
            if not _snapshot_matches_ingress(
                source_before, ingress, expected_excluded=()
            ):
                return fail(
                    "immutable_source_before",
                    "immutable source snapshot no longer matches repository ingress",
                )

            candidate_before = prepare_repository_snapshot(candidate, root / "candidate-before")
            candidate_fingerprints = _fingerprint_map(candidate_before.fingerprints)
            if candidate_before.excluded:
                return fail(
                    "candidate_sanitation",
                    "candidate introduced excluded paths: "
                    + repr(list(candidate_before.excluded)),
                )

            _, packaged_changes = build_workspace_patch(
                Path(source_before.snapshot_root),
                Path(candidate_before.snapshot_root),
            )
            changes = _file_changes(packaged_changes)
            if not changes.all:
                return fail(
                    "candidate_diff",
                    "candidate has no source-to-candidate file changes",
                    status="no_changes",
                )
            disallowed = tuple(
                path for path in changes.all if not _path_allowed(path, allowed_roots)
            )
            if disallowed:
                return fail(
                    "candidate_diff",
                    "candidate changed paths outside the write allowlist: "
                    + repr(list(disallowed)),
                )

            replay_snapshot = prepare_repository_snapshot(
                immutable_source, root / "replay"
            )
            if not _snapshot_matches_ingress(
                replay_snapshot, ingress, expected_excluded=()
            ):
                return fail(
                    "replay_source",
                    "immutable source changed before guarded replay",
                )
            replay_root = Path(replay_snapshot.snapshot_root)
            _replay_changes(
                Path(source_before.snapshot_root),
                Path(candidate_before.snapshot_root),
                replay_root,
                packaged_changes,
                root / "replay-artifacts",
            )
            replay_capture = prepare_repository_snapshot(
                replay_root, root / "replay-capture"
            )
            if replay_capture.excluded or (
                replay_capture.fingerprints != candidate_before.fingerprints
            ):
                return fail(
                    "guarded_replay",
                    "guarded replay bytes do not exactly match the candidate inventory",
                )

            verifier_artifacts = _new_artifact_store(
                artifact_root, prefix="attempt-"
            )
            executor = _DisposableCheckExecutor(
                replay_root,
                check_ids,
                verifier_artifacts,
                timeout_seconds=timeout_seconds,
                output_bytes=output_bytes,
            )
            checks = tuple(executor.run(check_id) for check_id in check_ids)

            replay_after_checks = prepare_repository_snapshot(
                replay_root, root / "replay-after-checks"
            )
            if replay_after_checks.excluded or (
                replay_after_checks.fingerprints != candidate_before.fingerprints
            ):
                return fail(
                    "replay_after_checks",
                    "guarded replay changed while exact checks executed",
                )

            live_after = prepare_repository_snapshot(original_source, root / "live-after")
            if not _snapshot_matches_ingress(
                live_after, ingress, expected_excluded=ingress.excluded
            ):
                return fail("live_source_after", "live source changed during verification")
            source_after = prepare_repository_snapshot(
                immutable_source, root / "source-after"
            )
            if not _snapshot_matches_ingress(
                source_after, ingress, expected_excluded=()
            ):
                return fail(
                    "immutable_source_after",
                    "immutable source snapshot changed during verification",
                )
            candidate_after = prepare_repository_snapshot(
                candidate, root / "candidate-after"
            )
            final_fingerprints = _fingerprint_map(candidate_after.fingerprints)
            if candidate_after.excluded or (
                candidate_after.fingerprints != candidate_before.fingerprints
            ):
                candidate_fingerprints = final_fingerprints
                return fail(
                    "candidate_after",
                    "candidate changed or introduced excluded paths during verification",
                )
            candidate_fingerprints = final_fingerprints

            if not all(check.status == "passed" and check.ok for check in checks):
                return fail(
                    "checks",
                    "one or more exact checks did not pass on guarded replay",
                    status="failed",
                )
            return _verification_result(
                task_id=task_id,
                started_at=started_at,
                started=started,
                status="passed",
                ok=True,
                stage="complete",
                message="guarded replay and all exact checks passed",
                changes=changes,
                candidate_fingerprints=candidate_fingerprints,
                checks=checks,
            )
    except Exception as exc:
        return fail(
            "verification_error",
            f"verification failed closed: {type(exc).__name__}: {exc}",
        )


def _run_baseline_preflight(prepared: PreparedRepositoryRepair) -> BaselinePreflight:
    original_source = Path(prepared.ingress.source_root)
    immutable_source = prepared.task.source_workspace
    candidate = prepared.task.candidate_workspace
    try:
        with tempfile.TemporaryDirectory(prefix="forge8-repair-baseline-") as temporary:
            root = Path(temporary)
            live_before = prepare_repository_snapshot(original_source, root / "live-before")
            source_before = prepare_repository_snapshot(
                immutable_source, root / "source-before"
            )
            candidate_before = prepare_repository_snapshot(
                candidate, root / "candidate-before"
            )
            if not _snapshot_matches_ingress(
                live_before,
                prepared.ingress,
                expected_excluded=prepared.ingress.excluded,
            ) or not _snapshot_matches_ingress(
                source_before, prepared.ingress, expected_excluded=()
            ) or not _snapshot_matches_ingress(
                candidate_before, prepared.ingress, expected_excluded=()
            ):
                return BaselinePreflight(
                    "invalid", False, (), "repository state changed before baseline preflight"
                )

            artifacts = _new_artifact_store(
                prepared.run_root / "input" / "baseline-artifacts",
                prefix="attempt-",
            )
            executor = _DisposableCheckExecutor(
                immutable_source,
                prepared.check_ids,
                artifacts,
                timeout_seconds=prepared.check_timeout_seconds,
                output_bytes=prepared.check_output_bytes,
            )
            checks = tuple(executor.run(check_id) for check_id in prepared.check_ids)

            live_after = prepare_repository_snapshot(original_source, root / "live-after")
            source_after = prepare_repository_snapshot(
                immutable_source, root / "source-after"
            )
            candidate_after = prepare_repository_snapshot(
                candidate, root / "candidate-after"
            )
            unchanged = (
                _snapshot_matches_ingress(
                    live_after,
                    prepared.ingress,
                    expected_excluded=prepared.ingress.excluded,
                )
                and _snapshot_matches_ingress(
                    source_after, prepared.ingress, expected_excluded=()
                )
                and _snapshot_matches_ingress(
                    candidate_after, prepared.ingress, expected_excluded=()
                )
            )
            if not unchanged:
                return BaselinePreflight(
                    "invalid", False, checks, "repository state changed during baseline preflight"
                )
    except Exception as exc:
        message, _ = _clip(
            f"baseline preflight failed closed: {type(exc).__name__}: {exc}",
            _MAX_RESULT_TEXT_CHARS,
        )
        return BaselinePreflight("invalid", False, (), message)

    invalid = tuple(check for check in checks if check.status not in {"passed", "failed"})
    if invalid:
        reason = "baseline checks were not deterministic pass/fail results: " + ", ".join(
            f"{check.check_id}={check.status}"
            + (f" ({check.error})" if check.error else "")
            for check in invalid
        )
        return BaselinePreflight("invalid", False, checks, reason)
    if not any(check.status == "failed" for check in checks):
        return BaselinePreflight(
            "already_passing", False, checks, "selected checks already pass at baseline"
        )
    return BaselinePreflight("repairable", True, checks, None)


def prepare_repository_repair(
    source_repo: str | os.PathLike[str],
    run_root: str | os.PathLike[str],
    goal: str,
    allowed_write_paths: Iterable[str],
    check_ids: Iterable[str],
    task_id: str,
    check_timeout_seconds: float = 120.0,
    check_output_bytes: int = 512 * 1024,
) -> PreparedRepositoryRepair:
    """Prepare one arbitrary small Python repository for the shared operator."""

    selected_goal = _strict_goal(goal)
    selected_task_id = _strict_task_id(task_id)
    selected_checks = _strict_check_ids(check_ids)
    timeout_seconds, output_bytes = _strict_check_limits(
        check_timeout_seconds, check_output_bytes
    )
    _assert_disjoint_source_and_run_root(source_repo, run_root)
    root = _prepare_run_root(run_root)
    input_root = root / "input"
    try:
        input_root.mkdir(exist_ok=False)
    except OSError as exc:
        raise RepositoryRepairError(f"cannot create repair input root: {exc}") from exc

    try:
        ingress = prepare_repository_snapshot(source_repo, input_root / "source")
        source = Path(ingress.snapshot_root)
        canonical_roots = validate_allowed_write_paths(source, allowed_write_paths)
        if "python_pytest" in selected_checks and any(
            path.casefold() == "conftest.py" for path in canonical_roots
        ):
            raise RepositoryRepairError(
                "root conftest.py cannot be writable for python_pytest"
            )
        candidate_ingress = prepare_repository_snapshot(source, input_root / "workspace")
    except (OSError, ValueError) as exc:
        if isinstance(exc, RepositoryRepairError):
            raise
        raise RepositoryRepairError(f"cannot prepare repository repair: {exc}") from exc
    if candidate_ingress.fingerprints != ingress.fingerprints or candidate_ingress.excluded:
        raise RepositoryRepairError("candidate workspace does not match immutable ingress")

    candidate = Path(candidate_ingress.snapshot_root)
    source_fingerprints = _fingerprint_map(ingress.fingerprints)
    verifier_artifacts = input_root / "verification-artifacts"

    def verify(candidate_workspace: Path) -> VerificationResult:
        return _verify_repository_candidate(
            task_id=selected_task_id,
            original_source=Path(ingress.source_root),
            immutable_source=source,
            candidate=candidate_workspace,
            ingress=ingress,
            allowed_roots=canonical_roots,
            check_ids=selected_checks,
            timeout_seconds=timeout_seconds,
            output_bytes=output_bytes,
            artifact_root=verifier_artifacts,
        )

    prompt = (
        "Repair the staged Python repository so every selected fixed check passes.\n"
        "The user goal is guidance; only deterministic checks and replay verification "
        "can prove success.\n\nUSER GOAL\n"
        + selected_goal
    )
    task = PreparedWorkspaceTask(
        task_id=selected_task_id,
        prompt_text=prompt,
        source_workspace=source,
        candidate_workspace=candidate,
        source_fingerprints=source_fingerprints,
        allowed_write_roots=canonical_roots,
        selected_check_ids=selected_checks,
        write_enabled=True,
        policy_metadata={
            "network": "deny",
            "workspace_access": "read_write",
            "allowed_write_roots": list(canonical_roots),
            "stdlib_only": all(
                check_id == "python_unittest" for check_id in selected_checks
            ),
            "isolation": "process_only",
            "network_isolation_enforced": False,
            "source_excluded": list(ingress.excluded),
        },
        verify=verify,
        require_failing_baseline=True,
        require_nonempty_changes=True,
        check_executor_factory=_check_executor_factory(
            candidate,
            selected_checks,
            timeout_seconds,
            output_bytes,
        ),
    )
    return PreparedRepositoryRepair(
        run_root=root,
        task=task,
        ingress=ingress,
        allowed_write_roots=canonical_roots,
        check_ids=selected_checks,
        check_timeout_seconds=timeout_seconds,
        check_output_bytes=output_bytes,
    )


__all__ = [
    "BaselinePreflight",
    "MAX_REPAIR_GOAL_CHARS",
    "PreparedRepositoryRepair",
    "RepositoryRepairError",
    "prepare_repository_repair",
]
