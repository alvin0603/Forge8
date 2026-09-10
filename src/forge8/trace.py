"""Append-only, hash-chained run traces with an explicit terminal seal."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GENESIS_HASH = "0" * 64


class TraceError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TraceError(f"trace payload is not canonical JSON: {exc}") from exc
    return rendered.encode("utf-8")


def _event_hash(
    sequence: int,
    recorded_at: str,
    kind: str,
    payload: dict[str, Any],
    previous_hash: str,
) -> str:
    body = {
        "sequence": sequence,
        "recorded_at": recorded_at,
        "kind": kind,
        "payload": payload,
        "previous_hash": previous_hash,
    }
    return hashlib.sha256(_canonical(body)).hexdigest()


@dataclass(frozen=True, slots=True)
class TraceEvent:
    sequence: int
    recorded_at: str
    kind: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TraceSeal:
    schema_version: int
    event_count: int
    head_hash: str
    trace_sha256: str
    sealed_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TraceVerification:
    ok: bool
    event_count: int
    head_hash: str
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_events(path: Path) -> tuple[list[TraceEvent], list[str]]:
    events: list[TraceEvent] = []
    errors: list[str] = []
    if not path.exists():
        return events, errors
    try:
        # JSONL records end at physical LF, not Unicode separators inside strings.
        lines = path.read_bytes().decode("utf-8").split("\n")
        if lines[-1] == "":
            lines.pop()  # One terminal LF is not an extra empty record.
    except (OSError, UnicodeDecodeError) as exc:
        return events, [f"cannot read trace: {exc}"]
    previous = GENESIS_HASH
    for line_number, line in enumerate(lines, start=1):
        if not line:
            errors.append(f"line {line_number}: empty trace line")
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: invalid JSON: {exc.msg}")
            continue
        if not isinstance(value, dict):
            errors.append(f"line {line_number}: event is not an object")
            continue
        expected_keys = {
            "sequence",
            "recorded_at",
            "kind",
            "payload",
            "previous_hash",
            "event_hash",
        }
        if set(value) != expected_keys:
            errors.append(f"line {line_number}: unexpected event fields")
            continue
        try:
            event = TraceEvent(
                sequence=int(value["sequence"]),
                recorded_at=str(value["recorded_at"]),
                kind=str(value["kind"]),
                payload=dict(value["payload"]),
                previous_hash=str(value["previous_hash"]),
                event_hash=str(value["event_hash"]),
            )
        except (TypeError, ValueError) as exc:
            errors.append(f"line {line_number}: invalid event types: {exc}")
            continue
        if isinstance(value["sequence"], bool) or event.sequence != len(events) + 1:
            errors.append(f"line {line_number}: non-contiguous sequence")
        if not event.kind or "\x00" in event.kind:
            errors.append(f"line {line_number}: invalid event kind")
        if event.previous_hash != previous:
            errors.append(f"line {line_number}: previous hash mismatch")
        calculated = _event_hash(
            event.sequence,
            event.recorded_at,
            event.kind,
            event.payload,
            event.previous_hash,
        )
        if event.event_hash != calculated:
            errors.append(f"line {line_number}: event hash mismatch")
        events.append(event)
        previous = event.event_hash
    return events, errors


class TraceLedger:
    """Single-writer append-only ledger.

    The trace stays open while a run is active.  ``seal`` creates a separate
    terminal document binding both the logical chain head and exact JSONL bytes;
    further appends are then refused.
    """

    def __init__(self, trace_path: Path, seal_path: Path | None = None) -> None:
        self.trace_path = trace_path.expanduser().resolve()
        self.seal_path = (
            seal_path.expanduser().resolve()
            if seal_path is not None
            else self.trace_path.with_suffix(self.trace_path.suffix + ".seal.json")
        )
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        if self.seal_path.exists():
            raise TraceError(f"trace is already sealed: {self.seal_path}")
        events, errors = _read_events(self.trace_path)
        if errors:
            raise TraceError("cannot resume invalid trace: " + "; ".join(errors))
        self._count = len(events)
        self._head = events[-1].event_hash if events else GENESIS_HASH

    @property
    def event_count(self) -> int:
        return self._count

    @property
    def head_hash(self) -> str:
        return self._head

    def append(self, kind: str, payload: dict[str, Any]) -> TraceEvent:
        if self.seal_path.exists():
            raise TraceError("cannot append to a sealed trace")
        if not isinstance(kind, str) or not kind or "\x00" in kind:
            raise TraceError("event kind must be a non-empty string")
        if not isinstance(payload, dict):
            raise TraceError("event payload must be a JSON object")
        # Canonicalize now so serialization failures happen before opening the file.
        _canonical(payload)
        sequence = self._count + 1
        recorded_at = _utc_now()
        event_hash = _event_hash(sequence, recorded_at, kind, payload, self._head)
        event = TraceEvent(
            sequence=sequence,
            recorded_at=recorded_at,
            kind=kind,
            payload=payload,
            previous_hash=self._head,
            event_hash=event_hash,
        )
        encoded = _canonical(event.as_dict()) + b"\n"
        descriptor = os.open(
            self.trace_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._count = sequence
        self._head = event_hash
        return event

    def seal(self) -> TraceSeal:
        if self.seal_path.exists():
            raise TraceError("trace is already sealed")
        trace_bytes = self.trace_path.read_bytes() if self.trace_path.exists() else b""
        seal = TraceSeal(
            schema_version=1,
            event_count=self._count,
            head_hash=self._head,
            trace_sha256=hashlib.sha256(trace_bytes).hexdigest(),
            sealed_at=_utc_now(),
        )
        self.seal_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.seal_path.name}.", suffix=".tmp", dir=self.seal_path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_canonical(seal.as_dict()) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.seal_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return seal


def verify_trace(trace_path: Path, seal_path: Path) -> TraceVerification:
    trace = trace_path.expanduser().resolve()
    seal_file = seal_path.expanduser().resolve()
    events, errors = _read_events(trace)
    head = events[-1].event_hash if events else GENESIS_HASH
    try:
        raw_seal = json.loads(seal_file.read_text(encoding="utf-8"))
        seal = TraceSeal(
            schema_version=int(raw_seal["schema_version"]),
            event_count=int(raw_seal["event_count"]),
            head_hash=str(raw_seal["head_hash"]),
            trace_sha256=str(raw_seal["trace_sha256"]),
            sealed_at=str(raw_seal["sealed_at"]),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"cannot read trace seal: {exc}")
        return TraceVerification(False, len(events), head, tuple(errors))
    if seal.schema_version != 1:
        errors.append(f"unsupported seal schema: {seal.schema_version}")
    if seal.event_count != len(events):
        errors.append("seal event count mismatch")
    if seal.head_hash != head:
        errors.append("seal head hash mismatch")
    try:
        trace_sha256 = hashlib.sha256(trace.read_bytes()).hexdigest()
    except OSError as exc:
        errors.append(f"cannot hash trace: {exc}")
    else:
        if seal.trace_sha256 != trace_sha256:
            errors.append("seal trace byte hash mismatch")
    return TraceVerification(not errors, len(events), head, tuple(errors))
