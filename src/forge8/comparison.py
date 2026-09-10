"""Version-bound, source-only comparison capsules; no project source execution.

The two original UTF-8 byte sequences retain their line numbers under before/
and after/. Provenance is itself part of the bounded source snapshot, but never
contains that snapshot's own hash. Operational integrity does not prove behavior.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
from pathlib import Path, PurePosixPath
import re
import tempfile

from .changes import build_change_catalogue
from .explain import _canonical_json, _snapshot_identity, _snapshot_inventory_sha256
from .git_source import head_identity, read_head
from .operator import AcceptanceGateResult
from .repository import (
    MAX_REPOSITORY_BYTES, MAX_REPOSITORY_FILES, MAX_REPOSITORY_FILE_BYTES,
    RepositoryError, RepositoryFileFingerprint, RepositorySnapshot,
    _scan_repository, _strict_destination, _strict_existing_directory,
    _validate_portable_component, prepare_repository_snapshot,
)

_SCOPE = "retained comparison sources"
_PROVENANCE = "comparison.json"


class ComparisonError(RepositoryError):
    """A failed comparison capture, never an empty successful change list."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprints(files: dict[str, bytes]) -> tuple[RepositoryFileFingerprint, ...]:
    return tuple(RepositoryFileFingerprint(path, len(data), _sha(data))
        for path, data in sorted(files.items()))


def _target(root: Path, side: str, original: str) -> Path:
    """Contain even a malformed adapter result before creating any source file."""
    relative = PurePosixPath(original)
    if (not relative.parts or relative.is_absolute() or relative.as_posix() != original
            or any(part in (".", "..") for part in relative.parts)):
        raise ComparisonError("comparison_path_invalid")
    for part in relative.parts:
        _validate_portable_component(part, original)
    return root / side / Path(*relative.parts)


def _live_guard(source: Path, owned_root: Path, original: RepositorySnapshot,
                head: str, original_version: str) -> AcceptanceGateResult:
    evidence = {"head": head, "original_snapshot_sha256": original_version,
        "source_unchanged": False, "head_unchanged": False, "semantics_verified": False}
    try:
        if _strict_existing_directory(source, "comparison source") != source:
            raise ComparisonError("comparison_source_redirected")
        with tempfile.TemporaryDirectory(prefix=".comparison-check-", dir=owned_root) as raw:
            scratch = Path(raw)
            current = prepare_repository_snapshot(source, scratch / "current", for_explanation=True)
            evidence["source_unchanged"] = (current.fingerprints == original.fingerprints
                and current.excluded == original.excluded)
            evidence["head_unchanged"] = head_identity(source, scratch) == head
        if not evidence["source_unchanged"]:
            return AcceptanceGateResult(False, "comparison_source_changed", evidence)
        if not evidence["head_unchanged"]:
            return AcceptanceGateResult(False, "comparison_head_changed", evidence)
        return AcceptanceGateResult(True, None, evidence)
    except (OSError, ValueError, RuntimeError):
        return AcceptanceGateResult(False, "comparison_source_check_unavailable", evidence)


@dataclass(frozen=True, slots=True)
class Comparison:
    source: Path
    reading_source: Path
    snapshot: RepositorySnapshot
    catalogue: dict
    head: str
    original_snapshot_sha256: str
    _original: RepositorySnapshot = field(repr=False)
    _capsule_sha256: str = field(repr=False)
    _catalogue_sha256: str = field(repr=False)
    _before_excluded: tuple[str, ...] = field(repr=False)

    def view(self) -> dict:
        return {"head": self.head, "original_snapshot_sha256": self.original_snapshot_sha256,
            "catalogue": copy.deepcopy(self.catalogue), "scope": _SCOPE,
            "excluded": {"before": list(self._before_excluded), "after": list(self._original.excluded)},
            "sides": {"before": "before/", "after": "after/"}}

    def guard(self) -> AcceptanceGateResult:
        """Recheck original source/HEAD and exact retained capsule before acceptance.

        These are bounded observations, not a hostile-concurrent-mutation lock.
        Frozen dataclass fields do not freeze dict contents; their digest is also
        checked, while view() returns an independent catalogue copy.
        """
        evidence = {"head": self.head, "original_snapshot_sha256": self.original_snapshot_sha256,
            "capsule_sha256": self._capsule_sha256, "scope": _SCOPE,
            "capsule_unchanged": False, "catalogue_unchanged": False, "semantics_verified": False}
        try:
            parent = _strict_existing_directory(self.reading_source.parent, "comparison state")
            if (parent != self.reading_source.parent or self.source == parent
                    or self.source in parent.parents
                    or _strict_existing_directory(self.reading_source, "comparison capsule") != self.reading_source):
                raise ComparisonError("comparison_state_redirected")
            scan = _scan_repository(self.reading_source)
            evidence["capsule_unchanged"] = (scan.fingerprints == self.snapshot.fingerprints
                and scan.excluded == self.snapshot.excluded == ()
                and _snapshot_inventory_sha256(self.snapshot) == self._capsule_sha256)
            evidence["catalogue_unchanged"] = _sha(_canonical_json(self.catalogue)) == self._catalogue_sha256
            if not evidence["capsule_unchanged"]:
                return AcceptanceGateResult(False, "comparison_capsule_changed", evidence)
            if not evidence["catalogue_unchanged"]:
                return AcceptanceGateResult(False, "comparison_catalogue_changed", evidence)
            live = _live_guard(self.source, parent, self._original, self.head, self.original_snapshot_sha256)
            evidence.update(live.evidence)
            return AcceptanceGateResult(live.ok, live.reason, evidence)
        except (OSError, ValueError, RuntimeError):
            return AcceptanceGateResult(False, "comparison_integrity_check_unavailable", evidence)


def _prepare_comparison(source: Path, destination: Path) -> Comparison:
    original_source = _strict_existing_directory(source, "comparison source")
    published = _strict_destination(destination)
    if (original_source == published or original_source in published.parents
            or published in original_source.parents):
        raise ComparisonError("comparison_source_overlap")
    with tempfile.TemporaryDirectory(prefix=".comparison-capture-", dir=published.parent) as raw:
        working = Path(raw)
        baseline = read_head(original_source, working)
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", baseline.commit):
            raise ComparisonError("comparison_head_invalid")
        original = prepare_repository_snapshot(original_source, working / "current", for_explanation=True)
        original_version, _ = _snapshot_identity(original)
        current = _scan_repository(Path(original.snapshot_root))
        if current.fingerprints != original.fingerprints or current.excluded:
            raise ComparisonError("comparison_current_snapshot_changed")
        after = {item.fingerprint.path: item.content for item in current.files}
        before = dict(baseline.files)
        retained = {"before": before, "after": after}
        # Admission is joint: duplicating a large repository is not permission to
        # double the source limits. Provenance costs one file and its actual bytes.
        if (len(before) + len(after) + 1 > MAX_REPOSITORY_FILES
                or sum(len(data) for files in retained.values() for data in files.values()) > MAX_REPOSITORY_BYTES
                or any(len(data) > MAX_REPOSITORY_FILE_BYTES for files in retained.values() for data in files.values())):
            raise ComparisonError("comparison_source_limit")
        catalogue = build_change_catalogue(
            {path: data.decode("utf-8") for path, data in before.items()},
            {path: data.decode("utf-8") for path, data in after.items()})
        catalogue_sha = _sha(_canonical_json(catalogue))
        before_fingerprints = _fingerprints(before)
        provenance = {"schema_version": 1, "kind": "forge8.comparison.provenance",
            "head": baseline.commit, "scope": _SCOPE, "source_executed": False,
            "semantics_verified": False, "catalogue_sha256": catalogue_sha,
            "before": {"prefix": "before/", "files": [item.as_dict() for item in before_fingerprints],
                "excluded": list(baseline.excluded)},
            "after": {"prefix": "after/", "snapshot_sha256": original_version,
                "files": [item.as_dict() for item in original.fingerprints], "excluded": list(original.excluded)}}
        metadata = _canonical_json(provenance)
        if len(metadata) > MAX_REPOSITORY_FILE_BYTES:
            raise ComparisonError("comparison_provenance_limit")
        if len(metadata) + sum(len(data) for files in retained.values() for data in files.values()) > MAX_REPOSITORY_BYTES:
            raise ComparisonError("comparison_source_limit")
        capsule = working / "capsule"
        capsule.mkdir()
        expected = {_PROVENANCE: metadata}
        for side, files in retained.items():
            (capsule / side).mkdir()
            for path, data in sorted(files.items()):
                target = _target(capsule, side, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(data)
                expected[side + "/" + path] = data
        with (capsule / _PROVENANCE).open("xb") as stream:
            stream.write(metadata)
        staged = _scan_repository(capsule)
        if staged.fingerprints != _fingerprints(expected) or staged.excluded:
            raise ComparisonError("comparison_capsule_copy_changed")
        gate = _live_guard(original_source, working, original, baseline.commit, original_version)
        if not gate.ok:
            raise ComparisonError(gate.reason)
        snapshot = prepare_repository_snapshot(capsule, published, for_explanation=False)
        # Atomic snapshot creation already verified all copied bytes. Derive the
        # identity without introducing a fallible extra read after publication.
        capsule_version = _snapshot_inventory_sha256(snapshot)
        return Comparison(original_source, published, snapshot, catalogue, baseline.commit,
            original_version, original, capsule_version, catalogue_sha, tuple(baseline.excluded))


def prepare_comparison(source: Path, destination: Path) -> Comparison:
    """Atomically publish a complete retained comparison or leave no destination.

    Destination is new, its existing parent is owned state outside the project,
    and callers place that parent beside (not within) their runs directory.
    Reading subjects are never executed; only the bounded native Git adapter runs.
    """
    try:
        return _prepare_comparison(source, destination)
    except ComparisonError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise ComparisonError("comparison_preparation_unavailable") from exc
