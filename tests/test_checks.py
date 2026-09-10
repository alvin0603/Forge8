from __future__ import annotations

import ctypes
import json
import os
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from importlib import metadata
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

import forge8.checks as checks_module
from forge8.checks import CHECK_REGISTRY, CheckDefinition, CheckRunner, discover_checks
from forge8.workspace import ArtifactStore, WorkspacePolicy


def _has_exact_pytest_runtime() -> bool:
    try:
        return metadata.version("pytest") == "9.1.1"
    except metadata.PackageNotFoundError:
        return False


class _FakeWindowsApi:
    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple[object, ...]] = []

    def record(self, operation: str, *values: object) -> None:
        self.calls.append((operation, *values))
        if self.fail_on == operation:
            raise checks_module._WindowsJobError(f"{operation} failed")

    def create_job(self) -> int:
        self.record("create_job")
        return 10

    def set_kill_on_close(self, job_handle: int) -> None:
        self.record("set_kill_on_close", job_handle)

    def open_process_for_job(self, process_id: int) -> int:
        self.record("open_process_for_job", process_id)
        return 20

    def assign_process(self, job_handle: int, process_handle: int) -> None:
        self.record("assign_process", job_handle, process_handle)

    def find_primary_thread(self, process_id: int) -> int:
        self.record("find_primary_thread", process_id)
        return 30

    def resume_primary_thread(self, thread_id: int) -> None:
        self.record("resume_primary_thread", thread_id)

    def terminate_job(self, job_handle: int) -> None:
        self.record("terminate_job", job_handle)

    def close_handle(self, handle: int) -> None:
        self.record("close_handle", handle)


class WindowsJobControllerTests(unittest.TestCase):
    def test_extended_limit_layout_and_kill_on_close_flag(self) -> None:
        self.assertEqual(ctypes.sizeof(checks_module._ThreadEntry32), 28)
        expected_size = 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112
        self.assertEqual(
            ctypes.sizeof(checks_module._JobObjectExtendedLimitInformation),
            expected_size,
        )

        captured: dict[str, int] = {}

        def set_information(job, info_class, pointer, size):
            information = ctypes.cast(
                pointer,
                ctypes.POINTER(checks_module._JobObjectExtendedLimitInformation),
            ).contents
            captured["job"] = job.value
            captured["class"] = info_class
            captured["size"] = size
            captured["flags"] = information.BasicLimitInformation.LimitFlags
            return 1

        api = checks_module._CtypesWindowsApi.__new__(checks_module._CtypesWindowsApi)
        api.kernel32 = SimpleNamespace(SetInformationJobObject=set_information)
        api.set_kill_on_close(10)

        self.assertEqual(
            captured,
            {
                "job": 10,
                "class": 9,
                "size": expected_size,
                "flags": 0x00002000,
            },
        )

    def test_assign_happens_before_resume_and_cleanup_is_idempotent(self) -> None:
        api = _FakeWindowsApi()
        job = checks_module._WindowsJob.create(api)
        job.assign_and_resume(123)
        self.assertIsNone(job.terminate_and_close())
        self.assertIsNone(job.terminate_and_close())

        self.assertEqual(
            api.calls,
            [
                ("create_job",),
                ("set_kill_on_close", 10),
                ("open_process_for_job", 123),
                ("assign_process", 10, 20),
                ("close_handle", 20),
                ("find_primary_thread", 123),
                ("resume_primary_thread", 30),
                ("terminate_job", 10),
                ("close_handle", 10),
            ],
        )

    def test_configuration_failure_closes_job_before_launch(self) -> None:
        api = _FakeWindowsApi(fail_on="set_kill_on_close")
        with self.assertRaisesRegex(checks_module._WindowsJobError, "set_kill_on_close"):
            checks_module._WindowsJob.create(api)

        self.assertEqual(
            api.calls,
            [
                ("create_job",),
                ("set_kill_on_close", 10),
                ("close_handle", 10),
            ],
        )

    def test_assignment_failure_never_searches_or_resumes_thread(self) -> None:
        api = _FakeWindowsApi(fail_on="assign_process")
        job = checks_module._WindowsJob.create(api)
        with self.assertRaisesRegex(checks_module._WindowsJobError, "assign_process"):
            job.assign_and_resume(123)
        self.assertIsNone(job.terminate_and_close())

        self.assertEqual(
            api.calls,
            [
                ("create_job",),
                ("set_kill_on_close", 10),
                ("open_process_for_job", 123),
                ("assign_process", 10, 20),
                ("close_handle", 20),
                ("terminate_job", 10),
                ("close_handle", 10),
            ],
        )

    def test_resume_failure_is_contained_and_cleanup_errors_are_reported(self) -> None:
        api = _FakeWindowsApi(fail_on="resume_primary_thread")
        job = checks_module._WindowsJob.create(api)
        with self.assertRaisesRegex(checks_module._WindowsJobError, "resume_primary_thread"):
            job.assign_and_resume(123)
        api.fail_on = "terminate_job"
        cleanup_error = job.terminate_and_close()

        self.assertIn("terminate_job failed", cleanup_error or "")
        self.assertEqual(api.calls[-2:], [("terminate_job", 10), ("close_handle", 10)])


class CheckDefinitionTests(unittest.TestCase):
    def test_fixed_registry_is_machine_readable_and_process_only(self) -> None:
        definition = CHECK_REGISTRY["python_unittest"]
        payload = definition.as_dict()

        self.assertEqual(
            definition.argv[1:7],
            ("-I", "-B", "-S", "-X", "utf8", "-c"),
        )
        self.assertEqual(payload["id"], "python_unittest")
        self.assertEqual(payload["command"]["shell"], False)
        self.assertEqual(payload["isolation"], "process_only")
        self.assertFalse(payload["network_isolation_enforced"])
        self.assertFalse(payload["sealed"])
        self.assertEqual(payload["discovery_path"], "tests")
        self.assertEqual(json.loads(json.dumps(payload))["id"], "python_unittest")
        with self.assertRaises(TypeError):
            CHECK_REGISTRY["arbitrary"] = definition  # type: ignore[index]

    def test_pytest_registry_entry_is_fixed_and_disables_ambient_plugins(self) -> None:
        definition = CHECK_REGISTRY["python_pytest"]
        payload = definition.as_dict()

        self.assertEqual(definition.argv[0], CHECK_REGISTRY["python_unittest"].argv[0])
        self.assertEqual(definition.argv[1:4], ("-I", "-B", "-c"))
        bootstrap = definition.argv[4]
        self.assertIn("importlib.metadata.version(\"pytest\")", bootstrap)
        self.assertIn("sys.path.insert(0, os.getcwd())", bootstrap)
        self.assertIn("--disable-plugin-autoload", bootstrap)
        self.assertIn("no:cacheprovider", bootstrap)
        self.assertIn(repr(os.devnull), bootstrap)
        self.assertIn("'--rootdir=.'", bootstrap)
        self.assertIn("'--confcutdir=.'", bootstrap)
        self.assertIn("forge8[pytest]", bootstrap)
        self.assertNotIn("-S", definition.argv)
        self.assertEqual(payload["id"], "python_pytest")
        self.assertEqual(payload["discovery_path"], "tests")
        self.assertFalse(payload["command"]["shell"])

    def test_definition_rejects_escape_verifier_and_false_isolation_claims(self) -> None:
        base = CHECK_REGISTRY["python_unittest"]
        common = {
            "id": "bad",
            "label": "bad",
            "description": "bad",
            "argv": base.argv,
            "timeout_seconds": 1.0,
        }
        with self.assertRaisesRegex(ValueError, "workspace-relative|traversal"):
            CheckDefinition(cwd="../outside", discovery_path="tests", **common)
        with self.assertRaisesRegex(ValueError, "verifier"):
            CheckDefinition(cwd=".", discovery_path="verifier", **common)
        with self.assertRaisesRegex(ValueError, "process_only"):
            CheckDefinition(
                cwd=".",
                discovery_path="tests",
                isolation="sealed",  # type: ignore[arg-type]
                **common,
            )
        with self.assertRaisesRegex(ValueError, "network isolation"):
            CheckDefinition(
                cwd=".",
                discovery_path="tests",
                network_isolation_enforced=True,
                **common,
            )


class CheckDiscoveryTests(unittest.TestCase):
    def test_pytest_discovery_is_structural_and_runtime_attestation_is_in_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            policy = WorkspacePolicy(root)
            self.assertEqual(discover_checks(policy), ())

            (root / "tests").mkdir()
            self.assertEqual(
                discover_checks(policy),
                ("python_unittest", "python_pytest"),
            )

            (root / "anything-else").mkdir()
            self.assertEqual(
                discover_checks(policy),
                ("python_unittest", "python_pytest"),
            )

    def test_verifier_is_never_a_discovery_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            (root / "verifier" / "tests").mkdir(parents=True)
            policy = WorkspacePolicy(root)

            self.assertEqual(discover_checks(policy), ())

            link = root / "tests"
            try:
                link.symlink_to(root / "verifier", target_is_directory=True)
            except OSError:
                return
            self.assertEqual(discover_checks(policy), ())

    def test_discovery_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "workspace"
            outside = parent / "outside-tests"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "tests").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            policy = WorkspacePolicy(root)

            self.assertEqual(discover_checks(policy), ())


class CheckExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        parent = Path(self.temporary.name)
        self.root = parent / "workspace"
        self.tests = self.root / "tests"
        self.tests.mkdir(parents=True)
        self.policy = WorkspacePolicy(self.root, max_output_chars=512)
        self.artifacts = ArtifactStore(parent / "run")

    def write_test(self, source: str, name: str = "test_sample.py") -> None:
        (self.tests / name).write_text(source, encoding="utf-8")

    def runner(self, **kwargs) -> CheckRunner:
        return CheckRunner(self.policy, self.artifacts, **kwargs)

    def assert_windows_process_stopped(self, process_id: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int32

        handle = kernel32.OpenProcess(0x00100000, False, process_id)
        if not handle:
            return
        try:
            self.assertEqual(kernel32.WaitForSingleObject(handle, 5000), 0)
        finally:
            kernel32.CloseHandle(handle)

    def test_pass_has_sanitized_env_immutable_outputs_and_bounded_model_view(self) -> None:
        self.write_test(
            """\
import atexit
import os
import sys
import unittest

atexit.register(lambda: print("shutdown-diagnostic", file=sys.stderr))

class EnvironmentTest(unittest.TestCase):
    def test_environment(self):
        self.assertNotIn("FORGE8_SECRET_SENTINEL", os.environ)
        self.assertNotIn("HTTPS_PROXY", os.environ)
        self.assertNotIn("HOME", os.environ)
        self.assertEqual(os.environ["PYTHONPATH"], os.getcwd())
        self.assertEqual(os.environ["FORGE8_CHECK_ID"], "python_unittest")
        self.assertEqual(os.environ["FORGE8_ISOLATION"], "process_only")
        self.assertEqual(os.environ["FORGE8_NETWORK_ISOLATION_ENFORCED"], "0")
        print("stdout-marker")
        sys.stderr.write("stderr-marker\\n")
"""
        )
        with patch.dict(
            os.environ,
            {
                "FORGE8_SECRET_SENTINEL": "do-not-inherit",
                "HTTPS_PROXY": "http://proxy.invalid",
                "HOME": "/sensitive/home",
                "PYTHONPATH": "/sensitive/pythonpath",
            },
            clear=False,
        ):
            result = self.runner().run("python_unittest")

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.return_code, 70)
        self.assertEqual(result.cwd, ".")
        self.assertEqual(result.isolation, "process_only")
        self.assertFalse(result.network_isolation_enforced)
        self.assertFalse(result.sealed)
        self.assertIn("-B", result.command)
        self.assertIn("-S", result.command)
        self.assertIn("-I", result.command)
        self.assertIn(b"stdout-marker", self.artifacts.read(result.stdout_artifact))
        stderr = self.artifacts.read(result.stderr_artifact)
        self.assertIn(b"stderr-marker", stderr)
        self.assertIn(b"shutdown-diagnostic", stderr)
        self.assertNotIn("HOME", result.environment_keys)
        self.assertNotIn("HTTPS_PROXY", result.environment_keys)

        machine = result.as_dict()
        json.dumps(machine)
        model = json.loads(result.model_view())
        self.assertNotIn("command", model)
        self.assertNotIn("environment_keys", model)
        self.assertEqual(model["stdout"]["artifact"]["sha256"], result.stdout_artifact.sha256)
        self.assertEqual(model["isolation"], "process_only")
        self.assertFalse(model["network_isolation_enforced"])
        self.assertFalse(model["sealed"])
        with self.assertRaises(FrozenInstanceError):
            result.status = "failed"  # type: ignore[misc]
        with self.assertRaisesRegex(ValueError, "process_only"):
            replace(result, isolation="sealed")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "network isolation"):
            replace(result, network_isolation_enforced=True)
        with self.assertRaisesRegex(ValueError, "sealed"):
            replace(result, sealed=True)

    def test_model_surface_accepts_check_id_only(self) -> None:
        self.write_test("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n")
        runner = self.runner()

        with self.assertRaises(TypeError):
            runner.run("python_unittest", argv=("sh", "-c", "echo bad"))  # type: ignore[call-arg]

    def test_unknown_id_is_machine_readable_and_does_not_launch(self) -> None:
        with patch("forge8.checks.subprocess.Popen") as popen:
            result = self.runner().run("made_up")

        popen.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(self.artifacts.read(result.stdout_artifact), b"")
        self.assertEqual(self.artifacts.read(result.stderr_artifact), b"")
        json.dumps(result.as_dict())

    def test_internal_definition_uses_bounds_without_becoming_a_public_check(self) -> None:
        base = CHECK_REGISTRY["python_unittest"]
        definition = replace(
            base, id="private_fixture",
            argv=(base.argv[0], "-I", "-B", "-S", "-c", "print('X' * 4096)"),
        )
        registry_ids = tuple(CHECK_REGISTRY)
        runner = self.runner(max_output_bytes=64)
        with patch.object(runner, "_run_definition") as execute:
            for value in (definition.id, definition):
                with self.subTest(public_value=type(value).__name__):
                    result = runner.run(value)
                    self.assertEqual(result.status, "unavailable")
            execute.assert_not_called()
        result = runner._run_definition(definition)
        self.assertTrue(result.ok)
        self.assertEqual(result.command, definition.argv)
        self.assertEqual(result.return_code, 0)
        self.assertTrue(result.output_truncated)
        self.assertEqual(result.stdout_captured_bytes, 64)
        self.assertEqual(self.artifacts.read(result.stdout_artifact), b"X" * 64)
        self.assertEqual(tuple(CHECK_REGISTRY), registry_ids)
        self.assertNotIn(definition.id, discover_checks(self.policy))

    def test_missing_pytest_bootstrap_is_unavailable_before_repository_imports(self) -> None:
        self.write_test("def test_example():\n    assert True\n")
        definition = CHECK_REGISTRY["python_pytest"]
        without_site_packages = replace(
            definition,
            argv=(definition.argv[0], "-I", "-S", *definition.argv[2:]),
        )
        registry = MappingProxyType(
            {**CHECK_REGISTRY, "python_pytest": without_site_packages}
        )
        with patch.object(checks_module, "CHECK_REGISTRY", registry):
            result = self.runner().run("python_pytest")

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.return_code, 90)
        self.assertIn("pytest==9.1.1", result.error or "")
        self.assertIn("forge8[pytest]", result.error or "")
        self.assertIn(
            b"Forge8 pytest bootstrap failed",
            self.artifacts.read(result.stderr_artifact),
        )

    def test_only_attested_wrapper_statuses_are_admissible(self) -> None:
        expected = {
            0: "unavailable",
            1: "unavailable",
            2: "unavailable",
            -9: "unavailable",
            80: "passed",
            81: "failed",
            82: "unavailable",
            85: "unavailable",
            86: "unavailable",
            90: "unavailable",
        }
        for return_code, status in expected.items():
            with self.subTest(return_code=return_code):
                self.assertEqual(
                    checks_module._status_for_return_code(
                        "python_pytest", return_code
                    ),
                    status,
                )
        unittest_expected = {
            0: "unavailable",
            1: "unavailable",
            2: "unavailable",
            70: "passed",
            71: "failed",
            79: "unavailable",
        }
        for return_code, status in unittest_expected.items():
            with self.subTest(unittest_return_code=return_code):
                self.assertEqual(
                    checks_module._status_for_return_code(
                        "python_unittest", return_code
                    ),
                    status,
                )

    @unittest.skipUnless(
        _has_exact_pytest_runtime(),
        "requires the exact pytest optional runtime",
    )
    def test_pinned_pytest_outcomes_fail_closed_without_cache(self) -> None:
        test_file = self.tests / "test_pytest_runtime.py"
        test_file.write_text(
            "import pytest\n\n"
            "@pytest.mark.parametrize('value', [1, 2], ids=['one', 'two'])\n"
            "def test_value(value):\n"
            "    assert value < 2\n",
            encoding="utf-8",
        )
        failed = self.runner().run("python_pytest")

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.return_code, 81)
        self.assertIn(b"1 failed, 1 passed", self.artifacts.read(failed.stdout_artifact))

        (self.root / "conftest.py").write_text(
            "import pytest\n\n"
            "@pytest.fixture\n"
            "def expected_value():\n"
            "    yield 2\n",
            encoding="utf-8",
        )
        test_file.write_text(
            "def test_ok(expected_value):\n    assert expected_value == 2\n",
            encoding="utf-8",
        )
        passed = self.runner().run("python_pytest")
        self.assertEqual(passed.status, "passed")
        self.assertEqual(passed.return_code, 80)

        inadmissible = (
            (
                "skip",
                "import pytest\n\n"
                "def test_a_pass():\n    assert True\n\n"
                "def test_z_skip():\n    pytest.skip('not execution')\n",
                b"1 passed, 1 skipped",
            ),
            (
                "xfail",
                "import pytest\n\n"
                "def test_a_pass():\n    assert True\n\n"
                "@pytest.mark.xfail(reason='not execution')\n"
                "def test_z_xfail():\n    assert False\n",
                b"1 passed, 1 xfailed",
            ),
            (
                "early_exit",
                "import pytest\n\n"
                "def test_a_pass():\n    assert True\n\n"
                "def test_z_exit():\n    pytest.exit('early success', returncode=0)\n",
                None,
            ),
            (
                "large_exit",
                "import pytest\n\n"
                "def test_a_pass():\n    assert True\n\n"
                "def test_z_exit():\n"
                "    pytest.exit('large exit', returncode=256)\n",
                None,
            ),
        )
        for name, source, summary in inadmissible:
            with self.subTest(outcome=name):
                test_file.write_text(source, encoding="utf-8")
                result = self.runner().run("python_pytest")

                self.assertFalse(result.ok)
                self.assertEqual(result.status, "unavailable")
                self.assertEqual(result.return_code, 91)
                self.assertIn("non-repairable", result.error or "")
                if summary is not None:
                    self.assertIn(summary, self.artifacts.read(result.stdout_artifact))

        test_file.write_text("def test_a_pass():\n    assert True\n", encoding="utf-8")
        self.write_test("def test_hidden_failure():\n    assert False\n", "test_hidden.py")
        (self.root / "conftest.py").write_text(
            "import pytest\n\n"
            "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
            "def pytest_collection_modifyitems(config, items):\n"
            "    items[:] = [item for item in items "
            "if item.name != 'test_hidden_failure']\n"
            "    yield\n",
            encoding="utf-8",
        )
        filtered = self.runner().run("python_pytest")
        self.assertFalse(filtered.ok)
        self.assertEqual(filtered.status, "unavailable")
        self.assertEqual(filtered.return_code, 91)
        self.assertIn("non-repairable", filtered.error or "")
        self.assertIn(
            b"1 passed",
            self.artifacts.read(filtered.stdout_artifact),
        )

        (self.root / "conftest.py").unlink()
        (self.tests / "test_hidden.py").unlink()
        test_file.write_text("# deliberately no tests\n", encoding="utf-8")
        empty = self.runner().run("python_pytest")
        self.assertEqual(empty.status, "unavailable")
        self.assertEqual(empty.return_code, 85)
        self.assertIn("non-repairable", empty.error or "")
        self.assertFalse((self.root / ".pytest_cache").exists())
        self.assertFalse(any(self.root.rglob("__pycache__")))

    @unittest.skipUnless(
        _has_exact_pytest_runtime(),
        "requires the exact pytest optional runtime",
    )
    def test_pytest_bootstrap_cannot_be_short_circuited_by_workspace_imports(self) -> None:
        self.write_test("def test_failure():\n    assert False\n")
        for shadow_name in ("sitecustomize.py", "pluggy.py"):
            with self.subTest(shadow_name=shadow_name):
                shadow = self.root / shadow_name
                shadow.write_text("import os\nos._exit(0)\n", encoding="utf-8")
                try:
                    result = self.runner().run("python_pytest")
                finally:
                    shadow.unlink()

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.return_code, 81)
                self.assertIn(
                    b"1 failed",
                    self.artifacts.read(result.stdout_artifact),
                )

    def test_attested_failure_and_fixture_error_keep_complete_evidence(self) -> None:
        scenarios = (
            (
                "assertion",
                """\
import unittest

class FailureTest(unittest.TestCase):
    def test_failure(self):
        self.assertEqual(1, 2, "intentional failure evidence")
""",
                b"intentional failure evidence",
            ),
            (
                "module-fixture",
                """\
import unittest

def setUpModule():
    raise RuntimeError("fixture failure evidence")

class FixtureTest(unittest.TestCase):
    def test_unreached(self):
        pass
""",
                b"fixture failure evidence",
            ),
            (
                "unicode-failure",
                """\
import unittest

class UnicodeFailureTest(unittest.TestCase):
    def test_failure(self):
        self.fail("unicode failure 🚀")
""",
                "unicode failure 🚀".encode("utf-8"),
            ),
        )
        for name, source, marker in scenarios:
            with self.subTest(outcome=name):
                self.write_test(source)
                result = self.runner().run("python_unittest")

                self.assertFalse(result.ok)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.return_code, 71)
                self.assertFalse(result.timed_out)
                self.assertFalse(result.partial_output)
                stderr = self.artifacts.read(result.stderr_artifact)
                self.assertIn(marker, stderr)
                self.assertIn(b"FAILED", stderr)

    def test_timeout_keeps_partial_stdout_and_stderr(self) -> None:
        self.write_test(
            """\
import sys
import time

print("before-timeout-stdout", flush=True)
sys.stderr.write("before-timeout-stderr\\n")
sys.stderr.flush()
time.sleep(10)
"""
        )
        result = self.runner(max_timeout_seconds=0.2).run("python_unittest")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "timed_out")
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.return_code)
        self.assertTrue(result.partial_output)
        self.assertIn(b"before-timeout-stdout", self.artifacts.read(result.stdout_artifact))
        self.assertIn(b"before-timeout-stderr", self.artifacts.read(result.stderr_artifact))
        self.assertIn("timeout", result.error or "")

    def _assert_interruption_cleanup(
        self, interruption: BaseException, *, during_start: bool = False,
        cleanup_fails: bool = False,
    ) -> None:
        events: list[str] = []
        process = Mock(pid=1234)
        process.stdout = Mock()
        process.stderr = Mock()
        for name in ("stdout", "stderr"):
            def close(name=name):
                events.append("close-" + name)
                if cleanup_fails:
                    raise KeyboardInterrupt("cleanup close interrupted")
            getattr(process, name).close.side_effect = close
        waits = 0

        def wait(*, timeout):
            nonlocal waits
            waits += 1
            events.append("wait")
            if waits == 1 and not during_start:
                raise interruption
            self.assertEqual(timeout, 5.0)
            if cleanup_fails:
                raise OSError("reap failed")
            return -9

        process.wait.side_effect = wait
        readers = []
        for index in range(2):
            reader = Mock(ident=None)
            def start(reader=reader, index=index):
                events.append(f"start-{index}")
                if during_start and index == 1:
                    raise interruption
                reader.ident = index + 1
            reader.start.side_effect = start
            def join(*, timeout, index=index):
                events.append(f"join-{index}")
                if cleanup_fails:
                    raise RuntimeError("cleanup join failed")
            reader.join.side_effect = join
            readers.append(reader)
        job = SimpleNamespace(handle=10, assign_and_resume=Mock())

        def terminate():
            events.append("terminate")
            job.handle = None
            if cleanup_fails:
                raise OSError("termination failed")

        job.terminate_and_close = terminate
        runner = self.runner()
        with (
            patch("forge8.checks.subprocess.Popen", return_value=process),
            patch("forge8.checks.threading.Thread", side_effect=readers),
            patch("forge8.checks._new_windows_job", return_value=job),
            patch.object(runner, "_kill", side_effect=lambda child: terminate()) as kill,
        ):
            with self.assertRaises(type(interruption)) as raised:
                runner.run("python_unittest")
        self.assertIs(raised.exception, interruption)
        self.assertIn("terminate", events)
        self.assertEqual(events.count("wait"), 1 if during_start else 2)
        self.assertLess(events.index("terminate"), len(events) - 1 - events[::-1].index("wait"))
        self.assertLess(len(events) - 1 - events[::-1].index("wait"), events.index("close-stdout"))
        if os.name == "posix":
            kill.assert_called_once_with(process)
        else:
            kill.assert_not_called()
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()
        readers[0].join.assert_any_call(timeout=1.0)
        if during_start:
            readers[1].join.assert_not_called()
        self.assertEqual(list(self.artifacts.objects.iterdir()), [])

    def test_interrupted_wait_terminates_and_reaps_before_closing_pipes(self) -> None:
        for interruption in (KeyboardInterrupt("stop"), SystemExit(130), RuntimeError("wait failed")):
            with self.subTest(interruption=type(interruption).__name__):
                self._assert_interruption_cleanup(interruption)

    def test_interrupted_reader_start_cleans_up_only_started_readers(self) -> None:
        self._assert_interruption_cleanup(KeyboardInterrupt("start interrupted"), during_start=True)

    def test_cleanup_errors_do_not_replace_original_interruption(self) -> None:
        self._assert_interruption_cleanup(KeyboardInterrupt("original"), cleanup_fails=True)

    @unittest.skipUnless(os.name == "posix", "requires native POSIX process groups")
    def test_real_posix_child_is_reaped_on_wait_interruption(self) -> None:
        self.write_test("import time\ntime.sleep(30)\n")
        original_popen = checks_module.subprocess.Popen
        owned = []
        interruption = KeyboardInterrupt("controlled wait interruption")

        def launch(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            wait = process.wait
            owned.append((process, wait))
            first = True
            def interrupted_wait(*, timeout):
                nonlocal first
                if first:
                    first = False
                    raise interruption
                return wait(timeout=timeout)
            process.wait = interrupted_wait
            return process

        try:
            with patch("forge8.checks.subprocess.Popen", side_effect=launch):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    self.runner().run("python_unittest")
            self.assertIs(raised.exception, interruption)
            self.assertEqual(len(owned), 1)
            process, _ = owned[0]
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)
        finally:
            for process, wait in owned:
                if process.poll() is None:
                    CheckRunner._kill(process)
                wait(timeout=5.0)

    @unittest.skipUnless(os.name == "posix" and Path("/proc/self/stat").is_file(), "requires Linux process state")
    def test_posix_normal_exit_terminates_same_group_child_holding_output(self) -> None:
        child_pid_file = self.root / "same-group-child.pid"
        self.write_test(
            f"""\
import subprocess
import sys
import unittest
from pathlib import Path

class DescendantTest(unittest.TestCase):
    def test_descendant(self):
        child = subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
        )
        Path({str(child_pid_file)!r}).write_text(str(child.pid), encoding="ascii")
        print("same-group-parent-finished", flush=True)
"""
        )
        original_popen = checks_module.subprocess.Popen
        owned = []

        def launch(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            owned.append(process)
            return process

        def child_running(pid):
            try:
                # An orphaned zombie is stopped; its adoptive parent owns reaping.
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                return state not in {"Z", "X"}
            except FileNotFoundError:
                return False

        try:
            with patch("forge8.checks.subprocess.Popen", side_effect=launch):
                result = self.runner().run("python_unittest")
            self.assertTrue(result.ok)
            self.assertEqual(len(owned), 1)
            child_pid = int(child_pid_file.read_text(encoding="ascii"))
            for _ in range(100):
                if not child_running(child_pid):
                    break
                time.sleep(0.01)
            self.assertFalse(child_running(child_pid), "same-group child survived its completed check")
            self.assertIn(b"same-group-parent-finished", self.artifacts.read(result.stdout_artifact))
            self.assertIsNotNone(owned[0].poll())
            self.assertTrue(owned[0].stdout.closed)
            self.assertTrue(owned[0].stderr.closed)
        finally:
            for process in owned:
                # Exact group created by this fixture, including a reaped leader.
                CheckRunner._kill(process)
                process.wait(timeout=5.0)
            if child_pid_file.exists():
                child_pid = int(child_pid_file.read_text(encoding="ascii"))
                for _ in range(100):
                    if not child_running(child_pid):
                        break
                    time.sleep(0.01)
                self.assertFalse(child_running(child_pid), "fixture cleanup left its child running")

    @unittest.skipUnless(os.name == "posix", "requires POSIX group cleanup")
    def test_posix_normal_cleanup_error_cannot_report_pass(self) -> None:
        self.write_test("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n")
        runner = self.runner()
        with patch.object(runner, "_kill", side_effect=OSError("group cleanup denied")):
            result = runner.run("python_unittest")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "launch_error")
        self.assertIn("POSIX", result.error or "")
        self.assertIn("group cleanup denied", result.error or "")

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_posix_group_permission_error_is_not_hidden_after_leader_exit(self) -> None:
        process = Mock(pid=1234, returncode=0)
        with patch("forge8.checks.os.killpg", side_effect=PermissionError("owned group denied")):
            with self.assertRaises(PermissionError):
                CheckRunner._kill(process)
        process.kill.assert_not_called()
        process.returncode = None
        with patch("forge8.checks.os.killpg", side_effect=PermissionError("owned group denied")):
            CheckRunner._kill(process)
        process.kill.assert_called_once_with()

    @unittest.skipUnless(os.name == "nt", "requires native Windows Job Objects")
    def test_windows_timeout_terminates_grandchild(self) -> None:
        child_pid = self.root / "timeout-child.pid"
        self.write_test(
            f"""\
import subprocess
import sys
import time
import unittest
from pathlib import Path

class DescendantTest(unittest.TestCase):
    def test_descendant(self):
        child = subprocess.Popen(
            [sys.executable, "-B", "-S", "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path({str(child_pid)!r}).write_text(str(child.pid), encoding="ascii")
        time.sleep(60)
"""
        )
        result = self.runner(max_timeout_seconds=1.5).run("python_unittest")

        self.assertEqual(result.status, "timed_out")
        self.assertTrue(child_pid.is_file())
        self.assert_windows_process_stopped(int(child_pid.read_text(encoding="ascii")))
        self.assertEqual(result.isolation, "process_only")
        self.assertFalse(result.network_isolation_enforced)
        self.assertFalse(result.sealed)

    @unittest.skipUnless(os.name == "nt", "requires native Windows Job Objects")
    def test_windows_normal_root_exit_terminates_lingering_grandchild(self) -> None:
        child_pid = self.root / "normal-child.pid"
        self.write_test(
            f"""\
import subprocess
import sys
import unittest
from pathlib import Path

class DescendantTest(unittest.TestCase):
    def test_descendant(self):
        child = subprocess.Popen(
            [sys.executable, "-B", "-S", "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path({str(child_pid)!r}).write_text(str(child.pid), encoding="ascii")
"""
        )
        result = self.runner().run("python_unittest")

        self.assertEqual(result.status, "passed")
        self.assertTrue(child_pid.is_file())
        self.assert_windows_process_stopped(int(child_pid.read_text(encoding="ascii")))

    def test_launch_error_is_captured_without_raising(self) -> None:
        self.write_test("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n")
        with patch(
            "forge8.checks.subprocess.Popen",
            side_effect=FileNotFoundError("interpreter disappeared"),
        ):
            result = self.runner().run("python_unittest")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "launch_error")
        self.assertIsNone(result.return_code)
        self.assertIn("interpreter disappeared", result.error or "")
        self.assertEqual(self.artifacts.read(result.stdout_artifact), b"")
        self.assertEqual(self.artifacts.read(result.stderr_artifact), b"")

    def test_combined_output_has_hard_byte_cap_and_explicit_truncation(self) -> None:
        self.policy = WorkspacePolicy(self.root, max_output_chars=80)
        self.write_test(
            """\
import sys
import unittest

class LoudTest(unittest.TestCase):
    def test_loud(self):
        print("🙂" * 2000, flush=True)
        sys.stderr.write("E" * 5000)
        sys.stderr.flush()
"""
        )
        result = self.runner(max_output_bytes=512).run("python_unittest")

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "passed")
        self.assertTrue(result.output_truncated)
        self.assertTrue(result.partial_output)
        self.assertGreater(
            result.stdout_observed_bytes + result.stderr_observed_bytes,
            result.output_limit_bytes,
        )
        self.assertLessEqual(
            result.stdout_captured_bytes + result.stderr_captured_bytes,
            result.output_limit_bytes,
        )
        self.assertEqual(
            len(self.artifacts.read(result.stdout_artifact))
            + len(self.artifacts.read(result.stderr_artifact)),
            result.stdout_captured_bytes + result.stderr_captured_bytes,
        )
        self.assertLessEqual(len(result.stdout_preview) + len(result.stderr_preview), 80)
        json.loads(result.model_view())

    def test_cwd_escape_makes_check_unavailable_before_launch(self) -> None:
        self.tests.rmdir()
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        try:
            self.tests.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")

        with patch("forge8.checks.subprocess.Popen") as popen:
            result = self.runner().run("python_unittest")

        popen.assert_not_called()
        self.assertEqual(result.status, "unavailable")

    def test_invalid_runner_limits_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.runner(max_output_bytes=0)
        with self.assertRaises(ValueError):
            self.runner(max_timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
