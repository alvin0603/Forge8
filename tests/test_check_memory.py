"""Mock-only tests for the opt-in Windows committed-memory limit."""

import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import forge8.checks as checks


LIMIT = 1024**3


class CheckMemoryTests(unittest.TestCase):
    def test_default_accepts_both_platforms_without_memory_limit(self):
        for platform in ("nt", "posix"):
            with self.subTest(platform=platform), patch.object(checks, "os", SimpleNamespace(name=platform)):
                runner = checks.CheckRunner(Mock(), Mock())
                self.assertIsNone(runner.process_memory_limit_bytes)

    def test_limit_requires_positive_size_t_integer(self):
        invalid = (True, False, 0, -1, 1.0, "1024", [], checks._SIZE_T(-1).value + 1)
        with patch.object(checks, "os", SimpleNamespace(name="nt")):
            for value in invalid:
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "positive integer"):
                    checks.CheckRunner(Mock(), Mock(), process_memory_limit_bytes=value)
            for value in (1, LIMIT, checks._SIZE_T(-1).value):
                with self.subTest(value=value):
                    runner = checks.CheckRunner(Mock(), Mock(), process_memory_limit_bytes=value)
                    self.assertEqual(runner.process_memory_limit_bytes, value)

    def test_limit_is_refused_on_non_windows(self):
        with patch.object(checks, "os", SimpleNamespace(name="posix")):
            with self.assertRaisesRegex(ValueError, "only on Windows"):
                checks.CheckRunner(Mock(), Mock(), process_memory_limit_bytes=LIMIT)

    def test_native_structure_sets_only_requested_memory_and_kill_flags(self):
        captured = []

        def set_information(job, info_class, pointer, size):
            info = ctypes.cast(pointer, ctypes.POINTER(checks._JobObjectExtendedLimitInformation)).contents
            captured.append((job.value, info_class, size, info.BasicLimitInformation.LimitFlags,
                             info.ProcessMemoryLimit, info.JobMemoryLimit))
            return 1

        api = checks._CtypesWindowsApi.__new__(checks._CtypesWindowsApi)
        api.kernel32 = SimpleNamespace(SetInformationJobObject=set_information)
        api.set_kill_on_close(10)
        api.set_kill_on_close(10, process_memory_limit_bytes=LIMIT)
        size = ctypes.sizeof(checks._JobObjectExtendedLimitInformation)
        self.assertEqual(captured, [(10, 9, size, 0x2000, 0, 0), (10, 9, size, 0x2100, LIMIT, 0)])

    def test_job_default_preserves_single_argument_api_call(self):
        api = Mock()
        api.create_job.return_value = 10
        job = checks._WindowsJob.create(api)
        api.set_kill_on_close.assert_called_once_with(10)
        self.assertIsNone(job.close())

    def test_limit_is_set_before_assignment_and_resume(self):
        api = Mock()
        api.create_job.return_value = 10
        api.open_process_for_job.return_value = 20
        api.find_primary_thread.return_value = 30
        job = checks._WindowsJob.create(api, process_memory_limit_bytes=LIMIT)
        job.assign_and_resume(123)
        self.assertEqual(api.mock_calls, [
            call.create_job(),
            call.set_kill_on_close(10, process_memory_limit_bytes=LIMIT),
            call.open_process_for_job(123), call.assign_process(10, 20),
            call.close_handle(20), call.find_primary_thread(123), call.resume_primary_thread(30),
        ])
        self.assertIsNone(job.close())

    def test_limit_setup_failure_closes_job_before_any_assignment(self):
        api = Mock()
        api.create_job.return_value = 10
        api.set_kill_on_close.side_effect = checks._WindowsJobError("memory setup failed")
        with self.assertRaisesRegex(checks._WindowsJobError, "memory setup"):
            checks._WindowsJob.create(api, process_memory_limit_bytes=LIMIT)
        self.assertEqual(api.mock_calls, [
            call.create_job(), call.set_kill_on_close(10, process_memory_limit_bytes=LIMIT),
            call.close_handle(10),
        ])

    def test_factory_preserves_default_and_forwards_opt_in(self):
        with patch.object(checks, "_CtypesWindowsApi") as native, patch.object(checks._WindowsJob, "create") as create:
            self.assertIs(checks._new_windows_job(), create.return_value)
            create.assert_called_once_with(native.return_value)
            create.reset_mock()
            self.assertIs(checks._new_windows_job(process_memory_limit_bytes=LIMIT), create.return_value)
            create.assert_called_once_with(native.return_value, process_memory_limit_bytes=LIMIT)

    def test_runner_forwards_opt_in_before_suspended_spawn(self):
        # Stop at an owned simulated spawn failure; no process/thread is created.
        for limit in (None, LIMIT):
            with self.subTest(limit=limit), patch.object(checks, "os", SimpleNamespace(name="nt")):
                runner = checks.CheckRunner(Mock(), Mock(), process_memory_limit_bytes=limit)
                job = Mock()
                job.close.return_value = None
                definition = SimpleNamespace(id="owned", argv=("owned-placeholder",), cwd=".")
                events = Mock()
                with (
                    patch.object(checks, "_resolve_cwd", return_value="owned-cwd"),
                    patch.object(checks, "_sanitized_environment", return_value={}),
                    patch.object(checks, "_new_windows_job", return_value=job) as create,
                    patch.object(checks.subprocess, "Popen", side_effect=OSError("owned stop")) as spawn,
                    patch.object(runner, "_finish") as finish,
                ):
                    events.attach_mock(create, "create")
                    events.attach_mock(spawn, "spawn")
                    self.assertIs(runner._run_definition(definition), finish.return_value)
                if limit is None:
                    create.assert_called_once_with()
                else:
                    create.assert_called_once_with(process_memory_limit_bytes=LIMIT)
                self.assertEqual([event[0] for event in events.mock_calls[:2]], ["create", "spawn"])
                self.assertEqual(spawn.call_args.kwargs["creationflags"],
                                 checks._CREATE_SUSPENDED | checks._CREATE_NEW_PROCESS_GROUP)
                job.close.assert_called_once_with()
                job.assign_and_resume.assert_not_called()
                self.assertEqual(finish.call_args.kwargs["status"], "launch_error")


if __name__ == "__main__":
    unittest.main()
