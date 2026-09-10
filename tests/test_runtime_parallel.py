"""Tiny owned-file tests for ordered reads; no model, target code or large assets."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge8 import runtime


_CHUNK = 4 * 1024 * 1024
_THRESHOLD = 64 * 1024 * 1024
_FIELDS = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")


class RuntimeParallelHashTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="forge8-pread-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "owned-by-test.bin"
        self.content = b"0123456789abc"
        self.path.write_bytes(self.content)
        self.pools = []
        self.release_at_cleanup = None
        self.interrupt_shutdown_once = False
        test = self

        class ObservedExecutor(ThreadPoolExecutor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.pending_results = self.max_pending = 0
                self.shutdown_seen = False
                self.shutdown_calls = 0
                test.pools.append(self)

            def submit(self, *args, **kwargs):
                self.pending_results += 1
                self.max_pending = max(self.max_pending, self.pending_results)
                future = super().submit(*args, **kwargs)
                original_result = future.result
                original_cancel = future.cancel
                consumed = False

                def result(*args, **kwargs):
                    nonlocal consumed
                    try:
                        return original_result(*args, **kwargs)
                    finally:
                        if future.done() and not consumed:
                            consumed = True
                            self.pending_results -= 1

                future.result = result

                def cancel():
                    if test.release_at_cleanup is not None:
                        test.release_at_cleanup.set()
                    return original_cancel()

                future.cancel = cancel
                return future

            def shutdown(self, *args, **kwargs):
                self.shutdown_seen = True
                self.shutdown_calls += 1
                if test.release_at_cleanup is not None:
                    test.release_at_cleanup.set()
                if test.interrupt_shutdown_once:
                    test.interrupt_shutdown_once = False
                    raise KeyboardInterrupt("injected shutdown interruption")
                return super().shutdown(*args, **kwargs)

        self.executor_type = ObservedExecutor

    def read_at(self, _descriptor, size, offset):
        # Separate real handles emulate pread on native Windows too; no shared seek.
        with self.path.open("rb") as source:
            source.seek(offset)
            return source.read(size)

    def hash_owned(self, pread=None, *, cancel_requested=None):
        first_pool = len(self.pools)
        with self.path.open("rb") as stream:
            stream.seek(1)
            with patch.object(runtime.os, "pread", side_effect=pread or self.read_at, create=True), \
                    patch.object(runtime, "ThreadPoolExecutor", self.executor_type):
                try:
                    return runtime._sha256_pread(stream, 3, cancel_requested=cancel_requested)
                finally:
                    self.assertFalse(stream.closed, "caller still owns the descriptor")
                    self.assertEqual(stream.tell(), 1, "pread must not move the shared offset")
                    for pool in self.pools[first_pool:]:
                        self.assertTrue(pool.shutdown_seen)
                        self.assertLessEqual(pool.max_pending, 4)
                        self.assertTrue(all(not worker.is_alive() for worker in pool._threads),
                            "hash returned/raised with an owned worker still alive")

    def test_out_of_order_reads_hash_in_offset_order_with_bounded_pending_results(self):
        second_finished = threading.Event()
        completed = []
        lock = threading.Lock()

        def reordered(descriptor, size, offset):
            data = self.read_at(descriptor, size, offset)
            if offset == 0:
                if not second_finished.wait(5):
                    raise AssertionError("second read never progressed beside the first")
            with lock:
                completed.append(offset)
            if offset == 3:
                second_finished.set()
            return data

        self.assertEqual(self.hash_owned(reordered), hashlib.sha256(self.content).hexdigest())
        self.assertLess(completed.index(3), completed.index(0))
        self.assertEqual(self.pools[-1].max_pending, 4)
        self.assertIn(12, completed)  # A final one-byte block, not rounded-up input.
        self.assertIn(len(self.content), completed)  # Explicit EOF growth check.

    def test_one_byte_short_reads_are_reassembled_exactly(self):
        positions = []
        lock = threading.Lock()

        def short(descriptor, size, offset):
            data = self.read_at(descriptor, min(size, 1), offset)
            if data:
                with lock:
                    positions.append(offset)
            return data

        self.assertEqual(self.hash_owned(short), hashlib.sha256(self.content).hexdigest())
        self.assertEqual(sorted(positions), list(range(len(self.content))))

    def test_empty_file_still_checks_eof_and_preserves_caller_stream(self):
        self.path.write_bytes(b"")
        reads = []

        def empty(descriptor, size, offset):
            reads.append((size, offset))
            return self.read_at(descriptor, size, offset)

        self.assertEqual(self.hash_owned(empty), hashlib.sha256(b"").hexdigest())
        self.assertEqual(reads, [(1, 0)])

    def test_premature_eof_and_real_truncation_never_return_partial_digest(self):
        for truncate in (False, True):
            with self.subTest(truncate=truncate):
                self.path.write_bytes(self.content)
                changed = threading.Event()

                def eof(descriptor, size, offset):
                    if not truncate:
                        return b"" if offset == 3 else self.read_at(descriptor, size, offset)
                    if offset == 0:
                        with self.path.open("r+b") as source:
                            source.truncate(len(self.content) - 1)
                        changed.set()
                    elif not changed.wait(5):
                        raise AssertionError("truncation event never arrived")
                    return self.read_at(descriptor, size, offset)

                with self.assertRaises(OSError):
                    self.hash_owned(eof)

    def test_eof_growth_and_every_metadata_identity_change_are_rejected(self):
        def growth(descriptor, size, offset):
            if offset == len(self.content):
                with self.path.open("ab") as source:
                    source.write(b"!")
            return self.read_at(descriptor, size, offset)

        with self.assertRaisesRegex(OSError, "grew"):
            self.hash_owned(growth)
        self.path.write_bytes(self.content)
        actual = self.path.stat()
        before = SimpleNamespace(**{name: getattr(actual, name) for name in _FIELDS})
        for field in _FIELDS:
            with self.subTest(field=field):
                after = SimpleNamespace(**vars(before))
                setattr(after, field, getattr(after, field) + 1)
                with patch.object(runtime.os, "fstat", side_effect=[before, after]), \
                        self.assertRaisesRegex(OSError, "changed"):
                    self.hash_owned()

    def test_read_error_cancellation_and_callback_exception_join_blocked_workers(self):
        caller = threading.get_ident()
        for failure in ("read", "cancel", "callback"):
            with self.subTest(failure=failure):
                entered = threading.Event()
                release = threading.Event()
                self.release_at_cleanup = release
                marker = OSError("owned read failed") if failure == "read" else ValueError("callback failed")
                callback_threads = []

                def blocked(descriptor, size, offset):
                    blocked_offset = 3 if failure == "read" else 0
                    if offset == blocked_offset:
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError("cleanup did not release the blocked test read")
                    elif failure == "read" and offset == 0:
                        if not entered.wait(5):
                            raise AssertionError("other worker was not running before failure")
                        raise marker
                    return self.read_at(descriptor, size, offset)

                def cancelled():
                    callback_threads.append(threading.get_ident())
                    if failure == "callback" and entered.is_set():
                        raise marker
                    return failure == "cancel" and entered.is_set()

                expected = {"read": OSError, "cancel": KeyboardInterrupt, "callback": ValueError}[failure]
                try:
                    with self.assertRaises(expected) as error:
                        self.hash_owned(blocked, cancel_requested=cancelled)
                    self.assertTrue(entered.is_set())
                    self.assertTrue(release.is_set())
                    self.assertEqual(set(callback_threads), {caller})
                    if failure != "cancel":
                        self.assertIs(error.exception, marker)
                finally:
                    release.set()
                    self.release_at_cleanup = None

    def test_shutdown_interruption_is_reraised_only_after_workers_are_joined(self):
        self.interrupt_shutdown_once = True
        with self.assertRaises(KeyboardInterrupt):
            self.hash_owned()
        self.assertEqual(self.pools[-1].shutdown_calls, 2)

    def test_worker_timeout_is_an_error_not_a_polling_timeout(self):
        marker = TimeoutError("actual positional read timed out")

        def timed_out(_descriptor, _size, _offset):
            raise marker

        with self.assertRaises(TimeoutError) as error:
            self.hash_owned(timed_out)
        self.assertIs(error.exception, marker)

    def test_invalid_internal_chunk_budget_does_not_create_a_pool(self):
        with self.path.open("rb") as stream, \
                patch.object(runtime, "ThreadPoolExecutor", self.executor_type):
            for chunk in (0, -1, True, 1.5, _CHUNK + 1):
                with self.subTest(chunk=chunk), self.assertRaises(ValueError):
                    runtime._sha256_pread(stream, chunk)
        self.assertEqual(self.pools, [])

    def test_cancellation_after_eof_cannot_return_completed_digest(self):
        cancelled = threading.Event()

        def cancel_at_eof(descriptor, size, offset):
            data = self.read_at(descriptor, size, offset)
            if offset == len(self.content):
                cancelled.set()
            return data

        with self.assertRaises(KeyboardInterrupt):
            self.hash_owned(cancel_at_eof, cancel_requested=cancelled.is_set)

    def test_eligibility_is_narrow_performance_routing_not_host_dependent(self):
        wsl = "6.6.87.2-Microsoft-standard-WSL2"
        cases = [("linux", wsl, "/mnt/d/model.gguf", _CHUNK, True),
            ("linux", wsl, "/mnt/c/model.gguf", _CHUNK, True),
            ("win32", wsl, "/mnt/d/model.gguf", _CHUNK, False),
            ("linux", "6.8.0-generic", "/mnt/d/model.gguf", _CHUNK, False),
            ("linux", wsl, "/home/user/model.gguf", _CHUNK, False),
            ("linux", wsl, "mnt/d/model.gguf", _CHUNK, False),
            ("linux", wsl, "/mnt/dd/model.gguf", _CHUNK, False),
            ("linux", wsl, "/mnt/d", _CHUNK, False),
            ("linux", wsl, "/mnt/d/model.gguf", 3, False)]
        with patch.object(runtime.os, "pread", create=True):
            for system, release, path, chunk, expected in cases:
                with self.subTest(system=system, path=path, chunk=chunk), \
                        patch.object(runtime.sys, "platform", system), \
                        patch.object(runtime.platform, "release", return_value=release):
                    self.assertIs(runtime._parallel_hash_eligible(PurePosixPath(path), chunk), expected)
        with patch.object(runtime, "os", SimpleNamespace()), \
                patch.object(runtime.sys, "platform", "linux"), \
                patch.object(runtime.platform, "release", return_value=wsl):
            self.assertFalse(runtime._parallel_hash_eligible(PurePosixPath("/mnt/d/model.gguf"), _CHUNK))

    def test_public_fastpath_requires_open_regular_file_at_size_boundary(self):
        expected = hashlib.sha256(self.content).hexdigest()
        for mode, size, selected in ((stat.S_IFREG, _THRESHOLD, True),
                (stat.S_IFREG, _THRESHOLD - 1, False), (stat.S_IFDIR, _THRESHOLD, False)):
            with self.subTest(mode=mode, size=size):
                streams = []

                def fast(stream, chunk, *, cancel_requested=None):
                    self.assertFalse(stream.closed)
                    self.assertEqual(os.fstat(stream.fileno()).st_size, size)
                    self.assertEqual(chunk, _CHUNK)
                    streams.append(stream)
                    return expected

                with patch.object(runtime, "_parallel_hash_eligible", return_value=True), \
                        patch.object(runtime.os, "fstat", return_value=SimpleNamespace(st_mode=mode, st_size=size)), \
                        patch.object(runtime, "_sha256_pread", side_effect=fast) as parallel:
                    self.assertEqual(runtime.sha256_file(self.path), expected)
                self.assertEqual(parallel.call_count, int(selected))
                self.assertTrue(all(stream.closed for stream in streams))

    def test_windows_routing_keeps_serial_and_public_cancel_checks_after_close(self):
        with patch.object(runtime.sys, "platform", "win32"), \
                patch.object(runtime, "_sha256_pread") as parallel:
            self.assertEqual(runtime.sha256_file(self.path), hashlib.sha256(self.content).hexdigest())
            parallel.assert_not_called()
        streams = []

        def fast(stream, _chunk, *, cancel_requested=None):
            streams.append(stream)
            return hashlib.sha256(self.content).hexdigest()

        with patch.object(runtime, "_parallel_hash_eligible", return_value=True), \
                patch.object(runtime.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_size=_THRESHOLD)), \
                patch.object(runtime, "_sha256_pread", side_effect=fast), \
                self.assertRaises(KeyboardInterrupt):
            runtime.sha256_file(self.path, cancel_requested=lambda: bool(streams and streams[0].closed))
        self.assertEqual(len(streams), 1)
        self.assertTrue(streams[0].closed)


if __name__ == "__main__":
    unittest.main()
