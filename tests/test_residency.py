"""Model-free resident ownership tests; fake native process and HTTP only."""

import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge8 import residency
from forge8.server import ServerShutdownResult


class ResidentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.assets, self.runs = self.root / "assets", self.root / "runs"
        self.assets.mkdir()
        self.runs.mkdir()
        self.desk = self.runs / "desk"
        self.desk.mkdir()
        for name in ("runtime.json", "model.json", "profile.json"):
            (self.assets / name).write_text('{"id":"pinned-model"}', encoding="utf-8")
        self.options = {"runtime_manifest_path": Path("runtime.json"),
            "model_manifest_path": Path("model.json"), "profile_path": Path("profile.json")}
        self.plan = SimpleNamespace(profile=SimpleNamespace(parallel=1, flags=("--slots",)),
            endpoint="http://127.0.0.1:8042", model_manifest_path=self.assets / "model.json")
        self.preparation = SimpleNamespace(ok=True, plan=self.plan,
            as_dict=lambda: {"status": "ready", "model_integrity": {"ok": True, "checked": ["original-pin"]}})
        self.supervisors = []
        owner = self

        class Supervisor:
            def __init__(self, plan, *, log_directory, log_root, cancel_requested):
                self.plan, self.logs = plan, log_directory
                self.api_key = None
                self._process = SimpleNamespace(poll=Mock(return_value=None), pid=123)
                self.start_result = self.shutdown_result = None
                self.stop_calls = 0
                owner.supervisors.append(self)

            def start(self):
                self.api_key = "test-private-secret"
                self.logs.mkdir()
                (self.logs / "stdout.log").write_text("safe native log", encoding="utf-8")
                self.start_result = SimpleNamespace(ok=True, as_dict=lambda: {"ok": True, "pid": 123})
                return self.start_result

            def stop(self):
                if self.shutdown_result is None:
                    self.stop_calls += 1
                    self._process.poll.return_value = 0
                    self.api_key = None
                    self.shutdown_result = ServerShutdownResult("terminated", 0, True, False)
                return self.shutdown_result

        self.prepare = self.start_patch("prepare_server", return_value=self.preparation)
        self.start_patch("LocalServerSupervisor", Supervisor)
        self.probe = self.start_patch("_slot_idle", return_value=True)
        self.owner = residency.ResidentModel(self.desk, idle_timeout=600)
        self.addCleanup(self.cleanup_owner)

    def start_patch(self, name, *args, **kwargs):
        patched = patch.object(residency, name, *args, **kwargs)
        self.addCleanup(patched.stop)
        return patched.start()

    def cleanup_owner(self):
        lease = self.owner._lease
        if lease is not None and not lease._finished:
            lease.checkpoint(transport_completed=False, transport_secret_cleared=True)
            lease.finish(None, source_checks_ok=False)
        self.owner.close()

    def acquire(self, **options):
        return self.owner.acquire(self.assets, **self.options,
            cancel_event=options.pop("cancel_event", threading.Event()), **options)

    def artifact(self, lease, name="request", *, discovery=False, **changes):
        directory = self.runs / name
        directory.mkdir()
        path = directory / ("discovery.json" if discovery else "manifest.json")
        value = {"schema_version": 1,
            "kind": "forge8.discovery.request" if discovery else "forge8.explanation.request.manifest",
            "server_session_id": lease.session_id, "session_finalization": "pending", "ok": False}
        value.update(changes)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def complete(self, lease, name="request", **options):
        gate = lease.checkpoint(transport_completed=True, transport_secret_cleared=True)
        self.assertTrue(gate.ok)
        path = self.artifact(lease, name, **options)
        self.assertTrue(lease.finish(path, source_checks_ok=True))
        return path

    def test_lazy_creation_and_two_requests_reuse_verified_native_generation(self):
        self.assertEqual(self.owner.status()["state"], "unloaded")
        self.prepare.assert_not_called()
        first = self.acquire()
        self.complete(first)
        self.assertEqual(self.owner.status()["state"], "idle")
        second = self.acquire()
        self.assertIs(second.supervisor, first.supervisor)
        self.assertEqual(first.session_id, second.session_id)
        self.complete(second, "second")
        self.assertEqual(self.prepare.call_count, 1)
        self.assertEqual(first.supervisor.stop_calls, 0)
        self.owner.release(first.session_id)
        self.assertEqual(self.prepare.call_count, 2)
        self.assertEqual(first.supervisor.stop_calls, 1)

    def test_checkpoint_is_not_shutdown_or_secret_erasure(self):
        lease = self.acquire()
        gate = lease.checkpoint(transport_completed=True, transport_secret_cleared=True)
        self.assertEqual(gate.evidence, {"schema_version": 1, "scope": "resident_request",
            "server_session_id": lease.session_id, "request_completed": True,
            "slot_idle": True, "transport_secret_cleared": True, "session_finalization": "pending"})
        self.assertIsNone(lease.supervisor.shutdown_result)
        self.assertEqual(lease.supervisor.api_key, "test-private-secret")
        self.assertNotIn("test-private-secret", json.dumps(gate.evidence))
        self.assertNotIn("test-private-secret", json.dumps(self.owner.status()))

    def test_source_failure_closes_generation_without_reusing_it(self):
        lease = self.acquire()
        lease.checkpoint(transport_completed=True, transport_secret_cleared=True)
        self.assertFalse(lease.finish(self.artifact(lease), source_checks_ok=False))
        self.assertEqual(self.owner.status()["state"], "unloaded")
        other = self.acquire()
        self.assertNotEqual(other.session_id, lease.session_id)

    def test_false_or_unknown_slot_reclaims_and_does_not_approve_request(self):
        for probe in (False, RuntimeError("test-private-secret")):
            with self.subTest(probe=type(probe).__name__):
                lease = self.acquire()
                self.probe.side_effect = probe if isinstance(probe, Exception) else None
                self.probe.return_value = probe
                gate = lease.checkpoint(transport_completed=True, transport_secret_cleared=True)
                self.assertFalse(gate.ok)
                self.assertNotIn("test-private-secret", json.dumps(gate.evidence))
                self.assertEqual(lease.supervisor.stop_calls, 1)
                self.assertFalse(lease.finish(None, source_checks_ok=False))
                self.probe.side_effect, self.probe.return_value = None, True

    def test_incomplete_transport_never_probes_idle(self):
        lease = self.acquire()
        self.assertFalse(lease.checkpoint(transport_completed=False, transport_secret_cleared=True).ok)
        self.probe.assert_not_called()
        self.assertFalse(lease.finish(None, source_checks_ok=False))

    def test_cancellation_aborts_once_and_requires_new_explicit_acquisition(self):
        lease = self.acquire()
        lease.cancel_event.set()
        lease.abort()
        lease.abort()
        self.assertEqual(lease.supervisor.stop_calls, 1)
        self.assertFalse(lease.checkpoint(transport_completed=True, transport_secret_cleared=True).ok)
        self.assertFalse(lease.finish(None, source_checks_ok=False))
        with self.assertRaises(ValueError):
            lease.abort()
        self.assertNotEqual(self.acquire().session_id, lease.session_id)

    def test_unknown_exit_permanently_latches_owner(self):
        lease = self.acquire()
        supervisor = lease.supervisor
        supervisor.shutdown_result = ServerShutdownResult("kill_timeout", None, True, True)
        lease.checkpoint(transport_completed=False, transport_secret_cleared=True)
        lease.finish(None, source_checks_ok=False)
        self.assertEqual(self.owner.status()["state"], "cleanup_unknown")
        with self.assertRaises(ValueError):
            self.acquire()

    def test_transport_secret_uncertainty_also_prevents_reuse(self):
        lease = self.acquire()
        lease.checkpoint(transport_completed=True, transport_secret_cleared=False)
        lease.finish(None, source_checks_ok=False)
        self.assertEqual(self.owner.status()["state"], "cleanup_unknown")
        self.assertFalse(self.owner.status()["receipts"][-1]["transport_secrets_cleared"])

    def test_close_receipt_binds_immutable_requests_start_and_final_logs(self):
        lease = self.acquire()
        path = self.complete(lease)
        original = path.read_bytes()
        self.owner.close()
        summary = self.owner.status()["receipts"][-1]
        receipt = json.loads(Path(summary["receipt_path"]).read_text(encoding="utf-8"))
        self.assertTrue(receipt["ok"])
        self.assertEqual(len(receipt["requests"]), 1)
        self.assertEqual(receipt["requests"][0]["path"], str(path))
        self.assertTrue(receipt["server_logs"])
        self.assertEqual(original, path.read_bytes())
        self.assertNotIn("test-private-secret", json.dumps(receipt))

    def test_mutated_registered_manifest_invalidates_close_receipt(self):
        lease = self.acquire()
        path = self.complete(lease)
        path.write_text("{}", encoding="utf-8")
        self.owner.release(lease.session_id)
        receipt = self.owner.status()["receipts"][-1]
        self.assertFalse(receipt["ok"])
        self.assertFalse(receipt["request_manifests_unchanged"])

    def test_changed_manifest_and_new_valid_assets_cannot_replace_start_identity(self):
        lease = self.acquire()
        self.complete(lease)
        (self.assets / "model.json").write_text('{"id":"replacement"}', encoding="utf-8")
        self.owner.release(lease.session_id)
        receipt = self.owner.status()["receipts"][-1]
        self.assertFalse(receipt["ok"])
        self.assertFalse(receipt["asset_identity_unchanged"])

    def test_changed_verified_inventory_invalidates_close_receipt(self):
        lease = self.acquire()
        self.complete(lease)
        self.prepare.return_value = SimpleNamespace(ok=True, plan=self.plan,
            as_dict=lambda: {"status": "ready", "model_integrity": {"ok": True, "checked": ["new-pin"]}})
        self.owner.release(lease.session_id)
        self.assertFalse(self.owner.status()["receipts"][-1]["asset_identity_unchanged"])

    def test_discovery_artifact_can_share_session_with_later_explanation(self):
        first = self.acquire()
        self.complete(first, discovery=True)
        second = self.acquire()
        self.complete(second, "answer")
        self.assertEqual(first.session_id, second.session_id)
        self.owner.close()
        summary = self.owner.status()["receipts"][-1]
        receipt = json.loads(Path(summary["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(receipt["requests"]), 2)

    def test_wrong_session_artifact_is_not_registered_as_valid(self):
        lease = self.acquire()
        lease.checkpoint(transport_completed=True, transport_secret_cleared=True)
        path = self.artifact(lease, server_session_id="model-unrelated")
        self.assertFalse(lease.finish(path, source_checks_ok=True))
        self.assertFalse(self.owner.status()["receipts"][-1]["request_manifests_unchanged"])

    def test_release_refuses_busy_stale_or_identity_switch(self):
        lease = self.acquire()
        with self.assertRaises(ValueError): self.owner.release(lease.session_id)
        with self.assertRaises(ValueError): self.owner.close()
        with self.assertRaises(ValueError): self.acquire()
        self.complete(lease)
        with self.assertRaises(ValueError): self.owner.release("model-stale")
        options = {**self.options, "profile_path": Path("other.json")}
        with self.assertRaises(ValueError):
            self.owner.acquire(self.assets, **options, cancel_event=threading.Event())
        self.assertEqual(self.owner.status()["state"], "idle")

    def test_idle_expiration_rechecks_state_and_releases_without_new_request(self):
        lease = self.acquire()
        # A stale deadline while actively leased must not stop the model.
        with self.owner._condition:
            self.owner._deadline = time.monotonic() - 1
            self.owner._condition.notify_all()
        self.complete(lease)
        self.assertEqual(lease.supervisor.stop_calls, 0)
        with self.owner._condition:
            self.owner._deadline = time.monotonic() - 1
            self.owner._condition.notify_all()
            self.assertTrue(self.owner._condition.wait_for(
                lambda: self.owner._state == "unloaded", timeout=3))
        self.assertEqual(lease.supervisor.stop_calls, 1)
        self.assertEqual(self.owner.status()["receipts"][-1]["reason"], "idle_timeout")

    def test_release_reservation_blocks_acquire_during_posthash(self):
        lease = self.acquire()
        self.complete(lease)
        entered, proceed = threading.Event(), threading.Event()

        def blocked_prepare(*args, **kwargs):
            entered.set()
            if not proceed.wait(3):
                raise RuntimeError("test did not release posthash")
            return self.preparation

        self.prepare.side_effect = blocked_prepare
        worker = threading.Thread(target=lambda: self.owner.release(lease.session_id))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.owner.status()["state"], "releasing")
            with self.assertRaises(ValueError): self.acquire()
        finally:
            proceed.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(lease.supervisor.stop_calls, 1)

    def test_request_cap_requires_explicit_release_not_silent_restart(self):
        lease = self.acquire()
        self.complete(lease)
        generation = self.owner._generation
        original = list(generation["requests"])
        generation["requests"] = original * 256
        with self.assertRaises(ValueError): self.acquire()
        self.assertEqual(lease.supervisor.stop_calls, 0)
        generation["requests"] = original

    def test_cancel_before_prepare_never_hashes_starts_or_creates_session(self):
        cancellation = threading.Event()
        cancellation.set()
        with self.assertRaises(KeyboardInterrupt): self.acquire(cancel_event=cancellation)
        self.prepare.assert_not_called()
        self.assertEqual(self.supervisors, [])
        self.assertEqual(self.owner.status()["state"], "unloaded")

    def test_anchor_change_during_preparation_never_starts_a_server(self):
        def changed(*args, **kwargs):
            (self.assets / "profile.json").write_text('{"id":"changed"}', encoding="utf-8")
            return self.preparation

        self.prepare.side_effect = changed
        with self.assertRaises(ValueError): self.acquire()
        self.assertEqual(self.supervisors, [])
        self.assertEqual(self.owner.status()["state"], "unloaded")

    def test_failed_reuse_does_not_retry_or_publish_server_response_text(self):
        lease = self.acquire()
        self.complete(lease)
        self.probe.side_effect = RuntimeError("test-private-secret")
        with self.assertRaises(RuntimeError) as caught:
            self.acquire()
        self.assertNotIn("test-private-secret", str(caught.exception))
        self.assertEqual(len(self.supervisors), 1)
        self.assertEqual(self.owner.status()["state"], "unloaded")

    def test_extra_log_inventory_invalidates_receipt_without_binding_arbitrary_file(self):
        lease = self.acquire()
        self.complete(lease)
        (lease.supervisor.logs / "extra.log").write_text("unexpected", encoding="utf-8")
        self.owner.release(lease.session_id)
        summary = self.owner.status()["receipts"][-1]
        self.assertFalse(summary["ok"])
        receipt = json.loads(Path(summary["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["server_logs"], [])

    def test_request_outside_sibling_runs_is_never_bound(self):
        lease = self.acquire()
        lease.checkpoint(transport_completed=True, transport_secret_cleared=True)
        path = self.root / "manifest.json"
        path.write_text("{}", encoding="utf-8")
        self.assertFalse(lease.finish(path, source_checks_ok=True))
        self.assertFalse(self.owner.status()["receipts"][-1]["request_manifests_unchanged"])

    def test_mutated_start_record_cannot_receive_valid_session_receipt(self):
        lease = self.acquire()
        self.complete(lease)
        directory = lease._generation["root"]
        (directory / "start.json").write_text("{}", encoding="utf-8")
        self.owner.release(lease.session_id)
        receipt = json.loads((directory / "closed.json").read_text(encoding="utf-8"))
        self.assertFalse(receipt["ok"])
        self.assertFalse(receipt["start_record_unchanged"])
        self.assertIsInstance(receipt["start_record"]["sha256"], str)


class SlotProbeTests(unittest.TestCase):
    def probe(self, data, *, exits=False):
        process = SimpleNamespace(poll=Mock(side_effect=[None, 0 if exits else None]))
        supervisor = SimpleNamespace(_process=process, api_key="private-key",
            plan=SimpleNamespace(profile=SimpleNamespace(parallel=1, flags=("--slots",)),
                endpoint="http://127.0.0.1:8042"))
        with patch.object(residency, "_open_local_request", return_value=io.BytesIO(data)) as opener:
            result = residency._slot_idle(supervisor)
        self.assertEqual(opener.call_args.args[0].full_url, "http://127.0.0.1:8042/slots")
        self.assertEqual(opener.call_args.args[0].get_header("Authorization"), "Bearer private-key")
        self.assertEqual(opener.call_args.kwargs, {"timeout": 1.0})
        return result

    def test_only_single_idle_zero_slot_from_living_owner_is_accepted(self):
        self.assertTrue(self.probe(b'[{"id":0,"is_processing":false,"generation_prompt":"ignored"}]'))
        for data in (b'[]', b'{}', b'[{"id":true,"is_processing":false}]',
                b'[{"id":0,"is_processing":0}]', b'[{"id":0,"is_processing":true}]',
                b'[{"id":1,"is_processing":false}]', b'[{"id":0}]', b' ' * 65_537):
            with self.subTest(data=data[:100]): self.assertFalse(self.probe(data))
        self.assertFalse(self.probe(b'[{"id":0,"is_processing":false}]', exits=True))

    def test_duplicate_keys_and_nonfinite_values_are_rejected(self):
        for data in (b'[{"id":0,"id":0,"is_processing":false}]',
                b'[{"id":0,"is_processing":false,"extra":NaN}]'):
            with self.subTest(data=data), self.assertRaises(ValueError): self.probe(data)


if __name__ == "__main__":
    unittest.main()
