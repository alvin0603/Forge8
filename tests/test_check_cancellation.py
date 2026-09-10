"""Mock-only cancellation/deadline tests: never launch processes or threads."""

from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import forge8.checks as checks


class CheckCancellationTests(unittest.TestCase):
    @contextmanager
    def execution(self, platform="posix", *, timeout=2.0):
        runner = checks.CheckRunner(Mock(), Mock(), max_timeout_seconds=timeout)
        definition = SimpleNamespace(id="owned", argv=("owned-placeholder",), cwd=".",
                                     timeout_seconds=timeout)
        events = []
        process = Mock(pid=123, returncode=0)
        process.stdout = Mock()
        process.stderr = Mock()
        process.stdout.close.side_effect = lambda: events.append("stdout-closed")
        process.stderr.close.side_effect = lambda: events.append("stderr-closed")
        process.wait.return_value = 0
        process.kill.side_effect = lambda: events.append("process-killed")
        job = Mock(handle=10)

        def terminate():
            events.append("job-terminated")
            job.handle = None
            return None

        job.terminate_and_close.side_effect = terminate
        job.close.return_value = None
        readers = [Mock(ident=None), Mock(ident=None)]
        for index, reader in enumerate(readers):
            reader.start.side_effect = lambda reader=reader, index=index: setattr(reader, "ident", index + 1)
        with (
            patch.object(checks, "os", SimpleNamespace(name=platform)),
            patch.object(checks, "_resolve_cwd", return_value="owned-cwd"),
            patch.object(checks, "_sanitized_environment", return_value={}),
            patch.object(checks, "_new_windows_job", return_value=job) as new_job,
            patch.object(checks.subprocess, "Popen", return_value=process) as spawn,
            patch.object(checks.threading, "Thread", side_effect=readers) as thread,
            patch.object(checks.time, "monotonic", return_value=100.0) as clock,
            patch.object(runner, "_kill", side_effect=lambda child: events.append("group-killed")) as kill,
            patch.object(runner, "_finish") as finish,
        ):
            yield SimpleNamespace(runner=runner, definition=definition, process=process, job=job,
                                  spawn=spawn, thread=thread, readers=readers, new_job=new_job,
                                  clock=clock, kill=kill, finish=finish, events=events)

    def test_default_wait_call_remains_unsliced_on_both_platforms(self):
        for platform in ("posix", "nt"):
            with self.subTest(platform=platform), self.execution(platform) as run:
                result = run.runner._run_definition(run.definition)
                self.assertIs(result, run.finish.return_value)
                run.process.wait.assert_called_once_with(timeout=2.0)
                run.clock.assert_called_once_with()
                self.assertEqual(run.finish.call_args.kwargs["status"], "passed")

    def test_already_cancelled_starts_no_process_or_job(self):
        for platform in ("posix", "nt"):
            with self.subTest(platform=platform), self.execution(platform) as run:
                with self.assertRaises(KeyboardInterrupt):
                    run.runner._run_definition(run.definition, cancel_requested=lambda: True)
                run.spawn.assert_not_called()
                run.new_job.assert_not_called()
                run.thread.assert_not_called()
                run.finish.assert_not_called()

    def test_cancel_immediately_before_launch_closes_unassigned_windows_job(self):
        for failure in (True, RuntimeError("owned callback failed")):
            with self.subTest(failure=failure), self.execution("nt") as run:
                callback = Mock(side_effect=[False, failure])
                expected = RuntimeError if isinstance(failure, Exception) else KeyboardInterrupt
                with self.assertRaises(expected):
                    run.runner._run_definition(run.definition, cancel_requested=callback)
                run.spawn.assert_not_called()
                run.job.close.assert_called_once_with()
                run.job.assign_and_resume.assert_not_called()
                run.finish.assert_not_called()

    def test_cancel_or_callback_failure_before_resume_cleans_suspended_child(self):
        for failure in (True, RuntimeError("owned callback failed")):
            with self.subTest(failure=failure), self.execution("nt") as run:
                callback = Mock(side_effect=[False, False, failure])
                expected = RuntimeError if isinstance(failure, Exception) else KeyboardInterrupt
                with self.assertRaises(expected):
                    run.runner._run_definition(run.definition, cancel_requested=callback)
                run.job.assign_and_resume.assert_not_called()
                run.job.terminate_and_close.assert_called_once_with()
                run.process.kill.assert_called_once_with()
                run.process.wait.assert_called_once_with(timeout=5.0)
                run.thread.assert_not_called()
                self.assertEqual(run.events, ["job-terminated", "process-killed", "stdout-closed", "stderr-closed"])
                run.finish.assert_not_called()

    def test_cancel_or_callback_failure_during_wait_reaps_before_pipe_close(self):
        for platform in ("posix", "nt"):
            for failure in (True, RuntimeError("owned callback failed")):
                with self.subTest(platform=platform, failure=failure), self.execution(platform) as run:
                    def wait(*, timeout):
                        if timeout == 5.0:
                            run.events.append("reaped")
                            return -9
                        raise checks.subprocess.TimeoutExpired(run.definition.argv, timeout)

                    def cancelled():
                        if run.process.wait.call_count:
                            if isinstance(failure, Exception):
                                raise failure
                            return True
                        return False

                    run.process.wait.side_effect = wait
                    expected = RuntimeError if isinstance(failure, Exception) else KeyboardInterrupt
                    with self.assertRaises(expected) as caught:
                        run.runner._run_definition(run.definition, cancel_requested=cancelled)
                    if isinstance(failure, Exception):
                        self.assertIs(caught.exception, failure)
                    self.assertEqual(run.process.wait.call_args_list, [call(timeout=0.1), call(timeout=5.0)])
                    self.assertEqual(run.events, ["job-terminated" if platform == "nt" else "group-killed",
                                                  "reaped", "stdout-closed", "stderr-closed"])
                    for reader in run.readers:
                        self.assertEqual(reader.join.call_args_list, [call(timeout=1.0), call(timeout=0.1)])
                    run.finish.assert_not_called()

    def test_cancel_at_successful_wait_return_does_not_publish_result(self):
        with self.execution() as run:
            callback = lambda: bool(run.process.wait.call_count)
            with self.assertRaises(KeyboardInterrupt):
                run.runner._run_definition(run.definition, cancel_requested=callback)
            self.assertEqual(run.process.wait.call_args_list, [call(timeout=0.1), call(timeout=5.0)])
            run.kill.assert_called_once_with(run.process)
            run.finish.assert_not_called()

    def test_absolute_deadline_does_not_restart_after_each_poll(self):
        with self.execution(timeout=0.25) as run:
            run.clock.side_effect = [100.0, 100.0, 100.1, 100.2, 100.25]

            def wait(*, timeout):
                if timeout == 5.0:
                    return -9
                raise checks.subprocess.TimeoutExpired(run.definition.argv, timeout)

            run.process.wait.side_effect = wait
            run.runner._run_definition(run.definition, cancel_requested=lambda: False)
            intervals = [item.kwargs["timeout"] for item in run.process.wait.call_args_list]
            self.assertEqual(len(intervals), 4)
            for actual, expected in zip(intervals, (0.1, 0.1, 0.05, 5.0)):
                self.assertAlmostEqual(actual, expected)
            run.kill.assert_called_once_with(run.process)
            self.assertEqual(run.finish.call_args.kwargs["status"], "timed_out")
            self.assertTrue(run.finish.call_args.kwargs["timed_out"])

    def test_interrupted_cleanup_failures_raise_safe_error_after_pipe_cleanup(self):
        for failure in ("posix_kill", "posix_reap", "nt_job", "nt_reap", "nt_final_job"):
            platform = "nt" if failure.startswith("nt") else "posix"
            with self.subTest(failure=failure), self.execution(platform) as run:
                run.process.wait.side_effect = [
                    checks.subprocess.TimeoutExpired("PRIVATE_ARGUMENTS", 0.1),
                    checks.subprocess.TimeoutExpired("PRIVATE_ARGUMENTS", 5.0) if failure.endswith("reap") else 0,
                ]
                if failure == "posix_kill":
                    run.kill.side_effect = OSError("PRIVATE_KILL_DIAGNOSTIC")
                elif failure == "nt_job":
                    def failed_job():
                        run.job.handle = None
                        return "PRIVATE_JOB_DIAGNOSTIC"
                    run.job.terminate_and_close.side_effect = failed_job
                elif failure == "nt_final_job":
                    # A remaining handle's final cleanup result must not be ignored.
                    run.job.terminate_and_close.side_effect = [None, "PRIVATE_FINAL_DIAGNOSTIC"]
                with self.assertRaises(checks.CheckCleanupError) as caught:
                    run.runner._run_definition(run.definition, cancel_requested=lambda: bool(run.process.wait.call_count))
                self.assertEqual(caught.exception.process_id, 123)
                self.assertEqual(str(caught.exception), "Cleanup could not be confirmed for check process 123")
                self.assertIsInstance(caught.exception.__cause__, KeyboardInterrupt)
                self.assertEqual(run.events[-2:], ["stdout-closed", "stderr-closed"])
                run.process.stdout.close.assert_called_once_with()
                run.process.stderr.close.assert_called_once_with()
                run.finish.assert_not_called()

    def test_suspended_child_cleanup_failures_are_not_reported_as_cancelled(self):
        for failure in ("job", "kill", "reap"):
            with self.subTest(failure=failure), self.execution("nt") as run:
                if failure == "job":
                    run.job.terminate_and_close.side_effect = RuntimeError("PRIVATE_JOB_DIAGNOSTIC")
                elif failure == "kill":
                    run.process.kill.side_effect = OSError("PRIVATE_KILL_DIAGNOSTIC")
                else:
                    run.process.wait.side_effect = checks.subprocess.TimeoutExpired("PRIVATE_ARGUMENTS", 5.0)
                with self.assertRaises(checks.CheckCleanupError) as caught:
                    run.runner._run_definition(run.definition, cancel_requested=Mock(side_effect=[False, False, True]))
                self.assertIsInstance(caught.exception.__cause__, KeyboardInterrupt)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(run.events[-2:], ["stdout-closed", "stderr-closed"])
                run.job.assign_and_resume.assert_not_called()
                run.thread.assert_not_called()
                run.finish.assert_not_called()

    def test_timeout_reap_or_completed_cleanup_failure_keeps_slot_unconfirmed(self):
        for platform in ("posix", "nt"):
            for stage in ("timeout", "completed"):
                with self.subTest(platform=platform, stage=stage), self.execution(platform) as run:
                    if stage == "timeout":
                        run.clock.side_effect = [100.0, 102.0]
                        run.process.wait.side_effect = checks.subprocess.TimeoutExpired("PRIVATE_ARGUMENTS", 5.0)
                    elif platform == "posix":
                        run.kill.side_effect = OSError("PRIVATE_KILL_DIAGNOSTIC")
                    else:
                        def failed_job():
                            run.job.handle = None
                            return "PRIVATE_JOB_DIAGNOSTIC"
                        run.job.terminate_and_close.side_effect = failed_job
                    with self.assertRaises(checks.CheckCleanupError) as caught:
                        run.runner._run_definition(run.definition, cancel_requested=lambda: False)
                    self.assertEqual(caught.exception.process_id, 123)
                    self.assertNotIn("PRIVATE", str(caught.exception))
                    if stage == "timeout":
                        self.assertIsInstance(caught.exception.__cause__, checks.subprocess.TimeoutExpired)
                        run.process.wait.assert_called_once_with(timeout=5.0)
                    self.assertEqual(run.events[-2:], ["stdout-closed", "stderr-closed"])
                    if platform == "nt":
                        run.job.terminate_and_close.assert_called_once_with()
                    else:
                        run.kill.assert_called_once_with(run.process)
                    run.finish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
