"""Desk-owned native model generations; request completion is not shutdown.

The ordinary supervisor and one-shot acceptance gate are deliberately unchanged.
Session logs remain mutable until close; immutable request manifests are bound by
a separate close receipt. None of these records claims semantic correctness or
secure erasure. Only the desk owner may acquire a lease or release a generation.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import time
import urllib.request
from pathlib import Path

from .explain import _canonical_json, _file_state, _require_run_entry, _write_exclusive
from .inference import _open_local_request
from .operator import AcceptanceGateResult
from .runtime import sha256_file
from .server import LocalServerSupervisor, ServerPreparation, prepare_server


class ResidentPreparationError(ValueError):
    """Carry a failed trusted preflight only before a generation/secret exists."""

    def __init__(self, preparation: ServerPreparation):
        if not isinstance(preparation, ServerPreparation) or preparation.ok:
            raise ValueError("expected a failed server preparation")
        super().__init__("resident model asset preparation failed")
        self.preparation = preparation


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate session document field")
        result[key] = value
    return result


def _no_constant(_value):
    raise ValueError("nonfinite session document value")


def _document(data):
    return json.loads(data, object_pairs_hook=_strict_pairs, parse_constant=_no_constant)


def _regular_bytes(path: Path, limit: int = 1_048_576) -> bytes:
    _require_run_entry(path, directory=False, label="session-bound file")
    if path.resolve(strict=True) != path or path.stat().st_size > limit:
        raise ValueError("session-bound file is redirected or oversized")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("session-bound file is oversized")
    return data


def _anchors(asset_root: Path, options: dict) -> tuple[dict, str]:
    records = {}
    model_id = None
    for name, supplied in options.items():
        path = asset_root / supplied
        if not path.is_absolute() or asset_root not in path.parents:
            raise ValueError("session asset anchor is outside its asset root")
        data = _regular_bytes(path)
        records[name] = {"path": str(path), "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}
        if name == "model_manifest_path":
            model = _document(data)
            model_id = model.get("id") if type(model) is dict else None
    if (type(model_id) is not str or not model_id.strip() or len(model_id) > 200
            or not model_id.isprintable()):
        raise ValueError("session model id is invalid")
    return records, model_id


def _slot_idle(supervisor) -> bool:
    """Observe our existing native child plus its one authenticated idle slot."""
    process = supervisor._process
    if (process is None or process.poll() is not None or not supervisor.api_key
            or supervisor.plan.profile.parallel != 1
            or "--slots" not in supervisor.plan.profile.flags):
        return False
    request = urllib.request.Request(supervisor.plan.endpoint.rstrip("/") + "/slots",
        headers={"Authorization": "Bearer " + supervisor.api_key, "Accept": "application/json"})
    with _open_local_request(request, timeout=1.0) as response:
        data = response.read(65_537)
    if len(data) > 65_536:
        return False
    slots = _document(data)
    return (type(slots) is list and len(slots) == 1 and type(slots[0]) is dict
        and type(slots[0].get("id")) is int and slots[0]["id"] == 0
        and slots[0].get("is_processing") is False and process.poll() is None)


def _log_pin(root: Path, path: Path) -> dict:
    _require_run_entry(path, directory=False, label="session server log")
    if path.resolve(strict=True) != path:
        raise ValueError("session log is redirected")
    before = path.stat()
    if before.st_size > 64 * 1024 * 1024:
        raise ValueError("session log exceeds its binding budget")
    digest = sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("session log changed while binding")
    return {"path": path.relative_to(root).as_posix(), "size_bytes": before.st_size, "sha256": digest}


class ResidentLease:
    """One serialized request's access to an owned model generation.

    Call checkpoint even on errors, after clearing the transport key; finish in
    finally. abort is suitable for CancellableTransport's joined watcher. It
    stops the child, not the request thread, and never releases lease ownership.
    """

    def __init__(self, owner, generation, cancel_event):
        self._owner, self._generation = owner, generation
        self.cancel_event = cancel_event
        self.preparation = generation["preparation"]
        self.plan = self.preparation.plan
        self.supervisor = generation["supervisor"]
        self.session_id = generation["id"]
        self.model_id = generation["model_id"]
        self._checked = self._transport_cleared = self._finished = False

    def abort(self):
        with self._owner._condition:
            if self._finished or self._owner._lease is not self:
                raise ValueError("resident lease is no longer active")
        return self._owner._stop(self._generation)

    def checkpoint(self, *, transport_completed: bool, transport_secret_cleared: bool):
        if type(transport_completed) is not bool or type(transport_secret_cleared) is not bool:
            raise ValueError("request completion observations must be boolean")
        with self._owner._condition:
            if self._finished or self._owner._lease is not self:
                raise ValueError("resident lease is no longer active")
        self._transport_cleared = transport_secret_cleared
        idle = False
        if transport_completed and transport_secret_cleared and not self.cancel_event.is_set():
            try:
                idle = _slot_idle(self.supervisor)
            except Exception:
                pass  # Never expose a server-controlled response or credential.
        self._checked = idle
        if not idle:
            self.abort()
        return AcceptanceGateResult(idle, None if idle else "resident request completion was not proven", {
            "schema_version": 1, "scope": "resident_request", "server_session_id": self.session_id,
            "request_completed": transport_completed, "slot_idle": idle,
            "transport_secret_cleared": transport_secret_cleared,
            "session_finalization": "pending",
        })

    def finish(self, manifest_path: Path | None, *, source_checks_ok: bool) -> bool:
        return self._owner._finish(self, manifest_path, source_checks_ok)


class ResidentModel:
    def __init__(self, root: Path, *, idle_timeout: float = 600, reader: str = "qwen35"):
        root = Path(root)
        _require_run_entry(root, directory=True, label="resident owner root")
        if not root.is_absolute() or root.resolve(strict=True) != root or root == Path(root.anchor):
            raise ValueError("resident owner requires an absolute regular non-root directory")
        if (type(idle_timeout) not in (int, float) or not math.isfinite(idle_timeout)
                or not 0 < idle_timeout <= 86_400):
            raise ValueError("idle timeout must be finite, positive and at most 86400 seconds")
        if reader not in {"qwen35", "gemma12b"}:
            raise ValueError("unknown resident reader")
        self.root, self.reader, self.idle_timeout = root, reader, float(idle_timeout)
        self._condition = threading.Condition(threading.RLock())
        self._generation = self._lease = None
        self._failed_generation = None  # Retain an uncertain owned handle; never restart over it.
        self._state = "unloaded"
        self._closed = self._cleanup_unknown = False
        self._validation_failed = False
        self._deadline = None
        self._receipts = []
        self._timer = threading.Thread(target=self._idle_loop, name="forge8-model-idle", daemon=True)
        self._timer.start()

    def acquire(self, asset_root: Path, *, runtime_manifest_path: Path,
                model_manifest_path: Path, profile_path: Path,
                cancel_event: threading.Event, progress=None) -> ResidentLease:
        options = {"runtime_manifest_path": Path(runtime_manifest_path),
            "model_manifest_path": Path(model_manifest_path), "profile_path": Path(profile_path)}
        assets = Path(asset_root).resolve(strict=True)
        if assets == self.root or assets in self.root.parents or self.root in assets.parents:
            raise ValueError("resident assets and state must not overlap. Choose an absolute "
                "FORGE8_STATE_HOME outside the assets (Linux filesystem in WSL), then reopen "
                "the desk. read --idle-timeout 0 uses one-shot mode instead; browsing alone "
                "does not acquire a resident model.")
        identity = (str(assets), *(str(value) for value in options.values()))
        with self._condition:
            if self._closed or self._cleanup_unknown:
                raise ValueError("resident owner is closed or cleanup is unknown")
            if self._state not in {"unloaded", "idle"} or self._lease is not None:
                raise ValueError("resident model is busy")
            generation = self._generation
            if generation is not None and identity != generation["identity"]:
                raise ValueError("release the existing model before changing its identity")
            if generation is not None and len(generation["requests"]) >= 256:
                raise ValueError("release the model before asking more questions in a new session")
            self._state, self._deadline = "checking", None
        try:
            if cancel_event.is_set():
                raise KeyboardInterrupt("resident acquisition cancelled")
            if generation is None:
                if progress is not None:
                    progress("hashing the pinned local runtime and model")
                anchors, model_id = _anchors(assets, options)
                preparation = prepare_server(assets, **options, cancel_requested=cancel_event.is_set)
                if not preparation.ok:
                    raise ResidentPreparationError(preparation)
                if _anchors(assets, options)[0] != anchors:
                    raise ValueError("resident model asset preparation failed")
                parent = self.root / "model-sessions"
                parent.mkdir(mode=0o700, exist_ok=True)
                _require_run_entry(parent, directory=True, label="model sessions")
                session_id = "model-" + secrets.token_hex(12)
                directory = parent / session_id
                directory.mkdir(mode=0o700)
                supervisor = LocalServerSupervisor(preparation.plan,
                    log_directory=directory / "server-logs", log_root=directory,
                    cancel_requested=cancel_event.is_set)
                generation = {"id": session_id, "root": directory, "assets": assets,
                    "options": options, "identity": identity, "anchors": anchors,
                    "preparation": preparation, "prepared_bytes": _canonical_json(preparation.as_dict()),
                    "model_id": model_id, "supervisor": supervisor, "stop_lock": threading.Lock(),
                    "close_lock": threading.Lock(),
                    "requests": [], "registration_ok": True, "transports_cleared": True, "closed": None}
                with self._condition:
                    self._generation = generation
                    self._state = "loading"
                if progress is not None:
                    progress("starting the verified local model")
                start = supervisor.start()
                log_directory = directory / "server-logs"
                generation["log_names"] = sorted(path.name for path in log_directory.iterdir()) if log_directory.exists() else []
                _write_exclusive(directory / "start.json", _canonical_json({
                    "schema_version": 1, "kind": "forge8.model_session.start", "id": session_id,
                    "assets": anchors, "preparation": preparation.as_dict(), "start": start.as_dict(),
                    "session_finalization": "pending"}))
                generation["start_pin"] = _file_state(directory, directory / "start.json")
                if not start.ok:
                    raise ValueError("resident native model did not become ready")
            else:
                if progress is not None:
                    progress("reusing the verified resident model; checking its idle slot")
                if not _slot_idle(generation["supervisor"]):
                    raise ValueError("resident model is no longer alive and idle")
            if cancel_event.is_set():
                raise KeyboardInterrupt("resident acquisition cancelled")
            lease = ResidentLease(self, generation, cancel_event)
            with self._condition:
                self._lease, self._state = lease, "busy"
            return lease
        except BaseException as exc:
            if generation is not None:
                with self._condition:
                    self._state = "releasing"
                self._close_generation(generation, "acquire_failed")
            with self._condition:
                self._state = "cleanup_unknown" if self._cleanup_unknown else "unloaded"
                self._condition.notify_all()
            if generation is not None:
                if isinstance(exc, KeyboardInterrupt):
                    raise KeyboardInterrupt("resident model acquisition was interrupted") from None
                raise RuntimeError("resident model acquisition failed; inspect private session evidence") from None
            raise

    def _stop(self, generation):
        # An abort watcher can enter here while the worker still owns its lease.
        # No desk callback or condition lock is held over process API waits.
        with generation["stop_lock"]:
            return generation["supervisor"].stop()

    def _finish(self, lease, manifest_path, source_checks_ok):
        with self._condition:
            if lease._finished:
                return lease._checked
            if self._lease is not lease:
                raise ValueError("resident lease is not owned by this manager")
            lease._finished = True
            generation = lease._generation
            self._state = "busy"
        valid = (source_checks_ok is True and lease._checked and not lease.cancel_event.is_set())
        try:
            if manifest_path is None:
                valid = False
            else:
                path = Path(manifest_path)
                # Request roots are existing siblings of the desk run root.
                # Admit only the exact manifest file, never arbitrary directories.
                if (path.name not in {"manifest.json", "discovery.json"}
                        or path.parent.parent != self.root.parent
                        or path.parent == self.root):
                    raise ValueError("request manifest is outside the owned runs tree")
                data = _regular_bytes(path)
                document = _document(data)
                expected_kind = ("forge8.explanation.request.manifest" if path.name == "manifest.json"
                    else "forge8.discovery.request")
                if (type(document) is not dict or document.get("server_session_id") != lease.session_id
                        or document.get("kind") != expected_kind or type(document.get("schema_version")) is not int
                        or document["schema_version"] != 1
                        or document.get("session_finalization") != "pending"):
                    raise ValueError("request artifact does not bind this resident session")
                pin = {"path": str(path), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                if any(item["path"] == pin["path"] for item in generation["requests"]):
                    raise ValueError("request artifact was already registered")
                generation["requests"].append(pin)
        except BaseException:
            valid = False
            generation["registration_ok"] = False
        finally:
            generation["transports_cleared"] &= lease._transport_cleared
        if not valid:
            with self._condition:
                self._state = "releasing"
            self._close_generation(generation, "request_failed")
        with self._condition:
            self._lease = None
            self._state = "cleanup_unknown" if self._cleanup_unknown else "idle" if valid else "unloaded"
            self._deadline = time.monotonic() + self.idle_timeout if valid else None
            self._condition.notify_all()
        lease._checked = valid
        return valid

    def _close_generation(self, generation, reason):
        with generation["close_lock"]:
            return self._close_generation_locked(generation, reason)

    def _close_generation_locked(self, generation, reason):
        # Reservation happens under _condition; callers never invoke this while
        # holding it. Only the active owner or timer can finalize a generation.
        if generation["closed"] is not None:
            return generation["closed"]
        errors = []
        shutdown = None
        try:
            shutdown = self._stop(generation)
        except BaseException:
            errors.append("owned server shutdown raised an exception")
        supervisor = generation["supervisor"]
        reclaimed = (shutdown is not None and shutdown.ok
            and shutdown.status in {"already_exited", "terminated", "killed", "not_started"}
            and (shutdown.status == "not_started" or shutdown.return_code is not None))
        cleanup = reclaimed and supervisor.api_key is None and generation["transports_cleared"]
        assets_ok = requests_ok = logs_ok = start_ok = False
        logs = []
        if reclaimed:
            try:
                before = _anchors(generation["assets"], generation["options"])[0]
                prepared = prepare_server(generation["assets"], **generation["options"])
                after = _anchors(generation["assets"], generation["options"])[0]
                assets_ok = (before == after == generation["anchors"] and prepared.ok
                    and _canonical_json(prepared.as_dict()) == generation["prepared_bytes"])
            except BaseException:
                errors.append("session asset identity could not be rechecked")
            try:
                start_ok = _file_state(generation["root"], generation["root"] / "start.json") == generation["start_pin"]
                requests_ok = generation["registration_ok"]
                for pin in generation["requests"]:
                    data = _regular_bytes(Path(pin["path"]))
                    requests_ok &= len(data) == pin["size_bytes"] and hashlib.sha256(data).hexdigest() == pin["sha256"]
                directory = generation["root"] / "server-logs"
                if directory.exists():
                    _require_run_entry(directory, directory=True, label="session server logs")
                    paths = sorted(directory.iterdir())
                    if len(paths) > 2 or [path.name for path in paths] != generation.get("log_names"):
                        raise ValueError("session server log inventory changed")
                    logs = [_log_pin(generation["root"], path) for path in paths]
                elif generation.get("log_names"):
                    raise ValueError("session server log directory is missing")
                logs_ok = True
            except BaseException:
                errors.append("session artifact identity could not be rechecked")
        receipt = {"schema_version": 1, "kind": "forge8.model_session.close", "id": generation["id"],
            "reason": reason, "ok": bool(cleanup and assets_ok and requests_ok and logs_ok and start_ok),
            "shutdown": None if shutdown is None else shutdown.as_dict(),
            "process_reclaimed": bool(reclaimed),
            "supervisor_secret_cleared": supervisor.api_key is None,
            "transport_secrets_cleared": generation["transports_cleared"],
            "asset_identity_unchanged": assets_ok, "request_manifests_unchanged": requests_ok,
            "start_record_unchanged": start_ok, "start_record": generation.get("start_pin"),
            "requests": generation["requests"],
            "server_logs": logs, "errors": errors}
        try:
            _write_exclusive(generation["root"] / "closed.json", _canonical_json(receipt))
        except BaseException:
            receipt = {**receipt, "ok": False, "errors": [*errors, "session close receipt could not be published"]}
        generation["closed"] = receipt
        summary = {key: receipt[key] for key in ("id", "reason", "ok", "asset_identity_unchanged",
            "request_manifests_unchanged", "supervisor_secret_cleared", "transport_secrets_cleared", "process_reclaimed")}
        summary["receipt_path"] = str(generation["root"] / "closed.json")
        with self._condition:
            self._cleanup_unknown |= not cleanup
            self._validation_failed |= not receipt["ok"]
            if not cleanup:
                self._failed_generation = generation
            self._receipts.append(summary)
            self._receipts = self._receipts[-5:]
            if self._generation is generation:
                self._generation = None
            self._condition.notify_all()
        return receipt

    def status(self) -> dict:
        with self._condition:
            return {"state": self._state, "id": None if self._generation is None else self._generation["id"],
                "reader": self.reader, "idle_remaining_seconds": (None if self._deadline is None
                    else max(0, round(self._deadline - time.monotonic(), 1))),
                "release_allowed": self._state == "idle" and not self._closed,
                "validation_failed": self._validation_failed,
                "receipts": [dict(item) for item in self._receipts]}

    def release(self, expected_id: str, reason: str = "explicit") -> dict:
        if reason != "explicit":
            raise ValueError("public release reason must be explicit")
        with self._condition:
            generation = self._generation
            if generation is None or expected_id != generation["id"]:
                raise ValueError("resident model identity is stale")
            if self._state != "idle" or self._lease is not None:
                raise ValueError("resident model is busy")
            self._state, self._deadline = "releasing", None
        self._close_generation(generation, reason)
        with self._condition:
            self._state = "cleanup_unknown" if self._cleanup_unknown else "unloaded"
            self._condition.notify_all()
        return self.status()

    def _idle_loop(self):
        while True:
            with self._condition:
                if self._closed:
                    return
                remaining = None if self._state != "idle" or self._deadline is None else self._deadline - time.monotonic()
                if remaining is None or remaining > 0:
                    self._condition.wait(remaining)
                    continue
                generation = self._generation
                self._state, self._deadline = "releasing", None
            self._close_generation(generation, "idle_timeout")
            with self._condition:
                self._state = "cleanup_unknown" if self._cleanup_unknown else "unloaded"
                self._condition.notify_all()

    def close(self) -> dict:
        with self._condition:
            if self._lease is not None or self._state in {"checking", "loading", "busy"}:
                raise ValueError("join the active reading worker before closing its model owner")
            self._closed = True
            self._condition.notify_all()
        self._timer.join()
        with self._condition:
            generation = self._generation
        if generation is not None:
            self._close_generation(generation, "desk_close")
        with self._condition:
            self._state = "cleanup_unknown" if self._cleanup_unknown else "unloaded"
            self._deadline = None
        return self.status()
