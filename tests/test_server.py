from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from forge8.server import (
    LocalServerSupervisor,
    ServerPlan,
    _port_is_available,
    load_server_profile,
    prepare_server,
)


class PortAvailabilityTests(unittest.TestCase):
    def test_reuseaddr_is_set_before_bind_only_on_posix(self) -> None:
        for os_name in ("posix", "nt"):
            with self.subTest(os_name=os_name), patch(
                "forge8.server.os.name", os_name
            ), patch("forge8.server.socket.socket") as factory:
                listener = factory.return_value.__enter__.return_value
                self.assertTrue(_port_is_available("127.0.0.1", 0))
                expected = []
                if os_name == "posix":
                    expected.append(call.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1))
                expected.append(call.bind(("127.0.0.1", 0)))
                self.assertEqual(listener.method_calls, expected)
                factory.return_value.__exit__.assert_called_once()

    def test_live_loopback_listener_is_rejected_and_closed_port_is_available(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if os.name == "posix":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            address = listener.getsockname()
            self.assertFalse(_port_is_available(*address))
        self.assertTrue(_port_is_available(*address))

    @unittest.skipUnless(sys.platform == "linux", "requires Linux TCP TIME_WAIT semantics")
    def test_time_wait_is_reusable_but_replacement_listener_is_not(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.settimeout(2)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            address = listener.getsockname()
            with socket.create_connection(address, timeout=2) as client:
                accepted, _ = listener.accept()
                with accepted:
                    accepted.settimeout(2)
                    # The server actively closes first, so its local port owns
                    # TIME_WAIT after the peer acknowledges and closes too.
                    accepted.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b"")
                    client.shutdown(socket.SHUT_WR)
                    self.assertEqual(accepted.recv(1), b"")

        # Establish that the old bind-only probe would incorrectly reject it.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as plain_probe:
            with self.assertRaises(OSError) as raised:
                plain_probe.bind(address)
            self.assertEqual(raised.exception.errno, errno.EADDRINUSE)
        self.assertTrue(_port_is_available(*address))

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as replacement:
            replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            replacement.bind(address)
            replacement.listen(1)
            self.assertFalse(_port_is_available(*address))


class FakeProcess:
    def __init__(
        self,
        *,
        return_code=None,
        stubborn=False,
        terminate_error=None,
        kill_error=None,
        wait_errors=(),
        poll_errors=(),
    ) -> None:
        self.pid = 4242
        self.return_code = return_code
        self.stubborn = stubborn
        self.terminate_error = terminate_error
        self.kill_error = kill_error
        self.wait_errors = tuple(wait_errors)
        self.poll_errors = list(poll_errors)
        self.terminate_calls = 0
        self.kill_calls = 0
        self.poll_calls = 0
        self.wait_timeouts = []

    def poll(self):
        self.poll_calls += 1
        if self.poll_errors:
            raise self.poll_errors.pop(0)
        return self.return_code

    def wait(self, timeout=None):
        call_index = len(self.wait_timeouts)
        self.wait_timeouts.append(timeout)
        if call_index < len(self.wait_errors) and self.wait_errors[call_index] is not None:
            raise self.wait_errors[call_index]
        if timeout is None:
            if self.return_code is None:
                self.return_code = 0
            return self.return_code
        if self.stubborn and self.kill_calls == 0:
            raise subprocess.TimeoutExpired(cmd="llama-server", timeout=timeout)
        if self.stubborn and self.kill_calls:
            self.return_code = -9
        elif self.return_code is None:
            self.return_code = -15
        return self.return_code

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error
        if not self.stubborn:
            self.return_code = -15

    def kill(self):
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error
        self.return_code = -9


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FaultingCloseStream:
    def __init__(self, error) -> None:
        self.closed = False
        self.error = error
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        raise self.error


class ServerFixture(unittest.TestCase):
    def make_workspace(self, *, runtime_platform="windows-x86_64", executable_name=None):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        if executable_name is None:
            executable_name = "llama-server.exe" if runtime_platform.startswith("windows") else "llama-server"
        runtime_root = root / "runtime"
        install_root = runtime_root / "llama-pinned"
        model_root = root / "models"
        runtime_manifest_path = root / "config" / "runtimes" / "runtime.json"
        model_manifest_path = root / "config" / "models" / "model.json"
        profile_path = root / "config" / "profiles" / "profile.json"
        install_root.mkdir(parents=True)
        model_root.mkdir()
        runtime_manifest_path.parent.mkdir(parents=True)
        model_manifest_path.parent.mkdir(parents=True)
        profile_path.parent.mkdir(parents=True)

        archive_data = b"pinned server archive"
        archive = runtime_root / "runtime.zip"
        archive.write_bytes(archive_data)
        executable_data = b"native llama server"
        executable = install_root / executable_name
        executable.write_bytes(executable_data)
        if runtime_platform.startswith("linux"):
            executable.chmod(0o755)
        model_data = b"GGUF local model"
        model = model_root / "model.gguf"
        model.write_bytes(model_data)

        runtime_manifest = {
            "schema_version": 2,
            "name": "test runtime",
            "build": 10621,
            "commit": "abc123",
            "platform": runtime_platform,
            "assets": [
                {
                    "filename": archive.name,
                    "sha256": hashlib.sha256(archive_data).hexdigest(),
                }
            ],
            "install_dir": install_root.name,
            "required_files": [executable_name],
            "installed_files": [
                {
                    "filename": executable_name,
                    "size_bytes": len(executable_data),
                    "sha256": hashlib.sha256(executable_data).hexdigest(),
                }
            ],
        }
        model_manifest = {
            "schema_version": 1,
            "id": "test-model",
            "revision": "def456",
            "files": [
                {
                    "role": "target",
                    "filename": model.name,
                    "size_bytes": len(model_data),
                    "sha256": hashlib.sha256(model_data).hexdigest(),
                }
            ],
        }
        profile = {
            "schema_version": 1,
            "id": "test-profile",
            "runtime_manifest": "config/runtimes/runtime.json",
            "model_manifest": "config/models/model.json",
            "executable": f"runtime/{install_root.name}/{executable_name}",
            "model": "models/model.gguf",
            "host": "127.0.0.1",
            "port": 18080,
            "context_tokens": 8192,
            "parallel": 1,
            "kv_cache": "q8_0",
            "flash_attention": "on",
            "reasoning": "off",
            "reasoning_budget": 0,
            "flags": ["--no-mmproj", "--offline", "--jinja", "--metrics", "--slots"],
            "experimental": False,
        }
        runtime_manifest_path.write_text(json.dumps(runtime_manifest), encoding="utf-8")
        model_manifest_path.write_text(json.dumps(model_manifest), encoding="utf-8")
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        return {
            "temporary": temporary,
            "root": root,
            "runtime_root": runtime_root,
            "runtime_manifest": runtime_manifest_path,
            "model_manifest": model_manifest_path,
            "profile_path": profile_path,
            "profile": profile,
            "model": model,
            "executable": executable,
        }

    def prepare(self, fixture, *, system_name="Windows", is_wsl=False, cancel_requested=None):
        return prepare_server(
            fixture["root"],
            runtime_manifest_path=Path("config/runtimes/runtime.json"),
            model_manifest_path=Path("config/models/model.json"),
            profile_path=Path("config/profiles/profile.json"),
            system_name=system_name,
            is_wsl=is_wsl,
            cancel_requested=cancel_requested,
        )


class ServerPreparationTests(ServerFixture):
    def test_preparation_cancelled_before_metadata_never_verifies_or_returns_plan(self):
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        with (patch("forge8.server.load_server_profile") as profile,
              patch("forge8.server.verify_runtime") as runtime,
              patch("forge8.server.verify_model") as model,
              self.assertRaises(KeyboardInterrupt)):
            self.prepare(fixture, cancel_requested=lambda: True)
        profile.assert_not_called()
        runtime.assert_not_called()
        model.assert_not_called()

    def test_cancellation_reaches_both_native_runtime_and_model_hashing(self):
        from forge8 import runtime
        original_hash = runtime.sha256_file
        for system, platform in (("Windows", "windows-x86_64"), ("Linux", "linux-x86_64")):
            fixture = self.make_workspace(runtime_platform=platform)
            self.addCleanup(fixture["temporary"].cleanup)
            for target in ("runtime.zip", "model.gguf"):
                cancelled = threading.Event()
                visited = []

                def hash_file(path, **options):
                    visited.append(path.name)
                    if path.name == target:
                        cancelled.set()
                    return original_hash(path, **options)

                with self.subTest(system=system, target=target), \
                        patch.object(runtime, "sha256_file", side_effect=hash_file), \
                        self.assertRaises(KeyboardInterrupt):
                    self.prepare(fixture, system_name=system, is_wsl=system == "Linux",
                        cancel_requested=cancelled.is_set)
                self.assertEqual(visited[-1], target)

    def test_uncancelled_preparation_keeps_full_verification_and_exact_plan(self):
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        original = self.prepare(fixture)
        cancellable = self.prepare(fixture, cancel_requested=lambda: False)
        self.assertTrue(cancellable.ok, cancellable.errors)
        self.assertEqual(original.as_dict(), cancellable.as_dict())
        fixture["model"].write_bytes(b"x" * fixture["model"].stat().st_size)
        rejected = self.prepare(fixture, cancel_requested=lambda: False)
        self.assertEqual(rejected.status, "integrity_failed")
        self.assertIsNone(rejected.plan)

    def test_cancel_after_final_hash_still_cannot_publish_a_ready_plan(self):
        from forge8 import server
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        cancelled = threading.Event()
        verify = server.verify_model

        def verify_then_cancel(*args, **options):
            result = verify(*args, **options)
            cancelled.set()
            return result

        with patch.object(server, "verify_model", side_effect=verify_then_cancel), \
                self.assertRaises(KeyboardInterrupt):
            self.prepare(fixture, cancel_requested=cancelled.is_set)

    def test_omitted_batch_pair_preserves_legacy_profile_and_exact_command(self) -> None:
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        preparation = self.prepare(fixture)
        self.assertTrue(preparation.ok, preparation.errors)
        profile = preparation.plan.profile
        self.assertIsNone(profile.batch_size)
        self.assertIsNone(profile.ubatch_size)
        expected = {key: value for key, value in fixture["profile"].items() if key != "schema_version"}
        expected["reasoning_format"] = "none"
        self.assertEqual(profile.as_dict(), expected)
        self.assertEqual(preparation.plan.command, (
            str(fixture["executable"]), "--model", str(fixture["model"]),
            "--host", "127.0.0.1", "--port", "18080", "--ctx-size", "8192",
            "--parallel", "1", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
            "--flash-attn", "on", "--gpu-layers", "all", "--fit", "off",
            "--reasoning", "off", "--reasoning-format", "none", "--reasoning-budget", "0",
            "--cache-ram", "512", "--no-webui", "--cors-origins", "localhost",
            "--log-colors", "off", *fixture["profile"]["flags"],
        ))

    def test_explicit_batch_pair_is_reflected_in_both_native_plans(self) -> None:
        for system, runtime in (("Windows", "windows-x86_64"), ("Linux", "linux-x86_64")):
            fixture = self.make_workspace(runtime_platform=runtime)
            self.addCleanup(fixture["temporary"].cleanup)
            original = self.prepare(fixture, system_name=system, is_wsl=system == "Linux")
            self.assertTrue(original.ok, original.errors)
            for batch, ubatch in ((1, 1), (512, 256), (8192, 8192)):
                with self.subTest(system=system, batch=batch, ubatch=ubatch):
                    fixture["profile"].update(batch_size=batch, ubatch_size=ubatch)
                    fixture["profile_path"].write_text(json.dumps(fixture["profile"]), encoding="utf-8")
                    preparation = self.prepare(fixture, system_name=system, is_wsl=system == "Linux")
                    self.assertTrue(preparation.ok, preparation.errors)
                    self.assertEqual(preparation.plan.command, original.plan.command + (
                        "--batch-size", str(batch), "--ubatch-size", str(ubatch)))
                    self.assertEqual((preparation.plan.profile.batch_size, preparation.plan.profile.ubatch_size), (batch, ubatch))
                    recorded = preparation.as_dict()["plan"]["profile"]
                    self.assertEqual((recorded["batch_size"], recorded["ubatch_size"]), (batch, ubatch))

    def test_batch_pair_rejects_partial_null_noninteger_or_invalid_bounds(self) -> None:
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        cases = [{"batch_size": 512}, {"ubatch_size": 256}, {"batch_size": None},
            {"ubatch_size": None}, {"batch_size": 256, "ubatch_size": 512}]
        for field in ("batch_size", "ubatch_size"):
            for value in (None, True, False, "512", 512.0, [], {}, 0, -1, 8193):
                cases.append({"batch_size": 512, "ubatch_size": 256, field: value})
        for pair in cases:
            with self.subTest(pair=pair):
                fixture["profile_path"].write_text(json.dumps({**fixture["profile"], **pair}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "batch"):
                    load_server_profile(fixture["profile_path"])
                result = self.prepare(fixture)
                self.assertEqual(result.status, "configuration_error")
                self.assertIsNone(result.plan)

    def test_optional_reasoning_format_preserves_existing_command(self) -> None:
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        original = self.prepare(fixture)
        self.assertTrue(original.ok, original.errors)
        self.assertEqual(original.plan.profile.reasoning_format, "none")
        fixture["profile"]["reasoning_format"] = "none"
        fixture["profile_path"].write_text(json.dumps(fixture["profile"]), encoding="utf-8")
        explicit = self.prepare(fixture)
        self.assertTrue(explicit.ok, explicit.errors)
        self.assertEqual(explicit.plan.command, original.plan.command)

    def test_bounded_reasoning_plan_is_supported_for_both_native_runtimes(self) -> None:
        for system, runtime_platform in (("Windows", "windows-x86_64"), ("Linux", "linux-x86_64")):
            with self.subTest(system=system):
                fixture = self.make_workspace(runtime_platform=runtime_platform)
                self.addCleanup(fixture["temporary"].cleanup)
                fixture["profile"].update(
                    reasoning="on", reasoning_budget=2048, reasoning_format="auto"
                )
                fixture["profile_path"].write_text(json.dumps(fixture["profile"]), encoding="utf-8")
                preparation = self.prepare(fixture, system_name=system, is_wsl=system == "Linux")
                self.assertTrue(preparation.ok, preparation.errors)
                command = preparation.plan.command
                for flag, value in (
                    ("--reasoning", "on"), ("--reasoning-budget", "2048"),
                    ("--reasoning-format", "auto"), ("--ctx-size", "8192"),
                    ("--parallel", "1"), ("--cache-type-k", "q8_0"),
                ):
                    self.assertEqual(command[command.index(flag) + 1], value)
                self.assertEqual(preparation.plan.profile.as_dict()["reasoning_format"], "auto")

    def test_reasoning_profile_rejects_unbounded_or_mixed_combinations(self) -> None:
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        cases = (
            ("off", 2048, "none"), ("off", 0, "auto"), ("off", 0, "deepseek"),
            ("on", 0, "auto"), ("on", 2048, "none"), ("on", 2047, "auto"),
            ("on", 4096, "auto"), ("auto", 2048, "auto"),
            ("on", 2048, True), ("on", 2048, []),
            ("off", False, "none"), ("on", True, "auto"),
            ("off", 0.0, "none"), ("on", "2048", "auto"), ("on", None, "auto"),
        )
        for reasoning, budget, output_format in cases:
            with self.subTest(reasoning=reasoning, budget=budget, output_format=output_format):
                fixture["profile"].update(
                    reasoning=reasoning, reasoning_budget=budget, reasoning_format=output_format
                )
                fixture["profile_path"].write_text(json.dumps(fixture["profile"]), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "reasoning"):
                    load_server_profile(fixture["profile_path"])
        fixture["profile"].update(reasoning="on", reasoning_budget=2048)
        del fixture["profile"]["reasoning_format"]
        fixture["profile_path"].write_text(json.dumps(fixture["profile"]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "off/0/none or on/2048/auto"):
            load_server_profile(fixture["profile_path"])

    def test_shipped_reader_profiles_pin_each_native_runtime_and_same_quantized_model(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        model_relative = "config/models/qwen35_9b_q4_k_m.json"
        model_manifest = json.loads((repository / model_relative).read_text(encoding="utf-8"))
        self.assertEqual(model_manifest["id"], "qwen35-9b-q4-k-m")
        self.assertEqual(model_manifest["repository"], "unsloth/Qwen3.5-9B-GGUF")
        self.assertEqual(model_manifest["revision"], "3885219b6810b007914f3a7950a8d1b469d598a5")
        self.assertIn("does not pin its base revision", model_manifest["provenance_limit"])
        target, = model_manifest["files"]
        self.assertEqual(target["role"], "target")
        self.assertEqual(target["size_bytes"], 5680522464)
        self.assertEqual(target["sha256"], "03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8")
        self.assertIn(f'/resolve/{model_manifest["revision"]}/', target["download_url"])
        for suffix, runtime, executable in (
            ("", "llama_cpp_b10621.json", "runtime/llama-b10621-win-cuda12.4/llama-server.exe"),
            ("_linux", "llama_cpp_b10621_linux_cuda.json", "runtime/llama-b10621-linux-cuda12.8/llama-server"),
        ):
            with self.subTest(suffix=suffix):
                profile = load_server_profile(repository / f"config/profiles/qwen35_9b_read_8k{suffix}.json")
                self.assertEqual(profile.model_manifest, model_relative)
                self.assertEqual(profile.model, f'models/{target["filename"]}')
                self.assertEqual(profile.runtime_manifest, f"config/runtimes/{runtime}")
                self.assertEqual(profile.executable, executable)
                self.assertTrue((repository / profile.runtime_manifest).is_file())
                self.assertEqual((profile.reasoning, profile.reasoning_budget, profile.reasoning_format), ("on", 2048, "auto"))

    def test_verified_windows_plan_has_fixed_safe_production_shape(self) -> None:
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)

        preparation = self.prepare(fixture)

        self.assertTrue(preparation.ok, preparation.errors)
        assert preparation.plan is not None
        plan = preparation.plan
        self.assertEqual(plan.platform_shape, "windows-native")
        command = plan.command
        required_flags = {
            "--offline",
            "--no-mmproj",
            "--no-webui",
            "--ctx-size",
            "--cache-type-k",
            "--cache-type-v",
            "--flash-attn",
            "--gpu-layers",
            "--fit",
            "--reasoning",
            "--reasoning-format",
            "--reasoning-budget",
            "--cache-ram",
        }
        self.assertTrue(required_flags.issubset(command))
        expected_values = {
            "--ctx-size": "8192",
            "--cache-type-k": "q8_0",
            "--cache-type-v": "q8_0",
            "--flash-attn": "on",
            "--gpu-layers": "all",
            "--fit": "off",
            "--reasoning": "off",
            "--reasoning-format": "none",
            "--reasoning-budget": "0",
            "--cache-ram": "512",
        }
        for flag, expected in expected_values.items():
            self.assertEqual(command[command.index(flag) + 1], expected)
        self.assertNotIn("--api-key", command)
        self.assertNotIn("--api-key", json.dumps(preparation.as_dict()))

    def test_wsl_windows_pe_is_explicitly_refused(self) -> None:
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)

        preparation = self.prepare(fixture, system_name="Linux", is_wsl=True)

        self.assertEqual(preparation.status, "unsupported_platform")
        self.assertFalse(preparation.ok)
        message = " ".join(preparation.errors)
        self.assertIn("WSL NAT", message)
        self.assertIn("Windows Python", message)
        self.assertIn("PowerShell", message)
        self.assertIn("forge8 fix", message)

    def test_linux_native_runtime_interface_is_supported(self) -> None:
        fixture = self.make_workspace(runtime_platform="linux-x86_64")
        self.addCleanup(fixture["temporary"].cleanup)

        for is_wsl in (False, True):
            with self.subTest(is_wsl=is_wsl):
                preparation = self.prepare(fixture, system_name="Linux", is_wsl=is_wsl)

                self.assertTrue(preparation.ok, preparation.errors)
                assert preparation.plan is not None
                self.assertEqual(preparation.plan.platform_shape, "linux-native")
                self.assertEqual(preparation.plan.command[0], str(fixture["executable"].resolve()))

    def test_linux_execute_permission_error_is_actionable_after_integrity(self) -> None:
        fixture = self.make_workspace(runtime_platform="linux-x86_64")
        self.addCleanup(fixture["temporary"].cleanup)
        mode_before = fixture["executable"].stat().st_mode
        for is_wsl in (False, True):
            with self.subTest(is_wsl=is_wsl), patch(
                "forge8.server.os.access", return_value=False
            ) as access:
                preparation = self.prepare(fixture, system_name="Linux", is_wsl=is_wsl)
            access.assert_called_once_with(fixture["executable"].resolve(), os.X_OK)
            self.assertEqual(preparation.status, "configuration_error")
            self.assertIsNone(preparation.plan)
            self.assertTrue(preparation.runtime_integrity.ok)
            self.assertTrue(preparation.model_integrity.ok)
            message = " ".join(preparation.errors)
            self.assertIn(str(fixture["executable"].resolve()), message)
            self.assertIn("execute permission", message)
            self.assertIn("noexec", message)
            self.assertIn("did not change permissions", message)
            self.assertEqual(fixture["executable"].stat().st_mode, mode_before)

    def test_tampered_linux_runtime_precedes_execute_permission_failure(self) -> None:
        fixture = self.make_workspace(runtime_platform="linux-x86_64")
        self.addCleanup(fixture["temporary"].cleanup)
        fixture["executable"].write_bytes(b"tampered executable")
        with patch("forge8.server.os.access", return_value=False) as access:
            preparation = self.prepare(fixture, system_name="Linux", is_wsl=True)
        self.assertEqual(preparation.status, "integrity_failed")
        self.assertIsNone(preparation.plan)
        self.assertFalse(preparation.runtime_integrity.ok)
        access.assert_not_called()

    def test_windows_plan_does_not_apply_posix_execute_checks(self) -> None:
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        with patch("forge8.server.os.access", return_value=False) as access:
            preparation = self.prepare(fixture, system_name="Windows", is_wsl=False)
        self.assertTrue(preparation.ok, preparation.errors)
        access.assert_not_called()

    @unittest.skipUnless(sys.platform == "linux", "requires native Linux permissions")
    def test_native_linux_detection_rejects_an_actual_nonexecutable_file(self) -> None:
        fixture = self.make_workspace(runtime_platform="linux-x86_64")
        self.addCleanup(fixture["temporary"].cleanup)
        ready = self.prepare(fixture, system_name=None, is_wsl=None)
        self.assertTrue(ready.ok, ready.errors)
        self.assertEqual(ready.plan.platform_shape, "linux-native")
        fixture["executable"].chmod(0o644)
        self.assertFalse(os.access(fixture["executable"], os.X_OK))
        preparation = self.prepare(fixture, system_name=None, is_wsl=None)
        self.assertEqual(preparation.status, "configuration_error")
        self.assertIn("execute permission", " ".join(preparation.errors))
        self.assertTrue(preparation.runtime_integrity.ok)
        self.assertIsNone(preparation.plan)
        self.assertFalse(os.access(fixture["executable"], os.X_OK))

    def test_tampered_model_fails_integrity_before_plan(self) -> None:
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        fixture["model"].write_bytes(b"tampered")

        preparation = self.prepare(fixture)

        self.assertEqual(preparation.status, "integrity_failed")
        self.assertIsNone(preparation.plan)
        self.assertIsNotNone(preparation.model_integrity)
        assert preparation.model_integrity is not None
        self.assertFalse(preparation.model_integrity.ok)

    def test_tampered_server_executable_fails_integrity_with_archive_unchanged(self) -> None:
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        archive_before = (fixture["runtime_root"] / "runtime.zip").read_bytes()
        original = fixture["executable"].read_bytes()
        fixture["executable"].write_bytes(b"X" * len(original))

        preparation = self.prepare(fixture)

        self.assertEqual(preparation.status, "integrity_failed")
        self.assertIsNone(preparation.plan)
        self.assertEqual(
            (fixture["runtime_root"] / "runtime.zip").read_bytes(), archive_before
        )
        self.assertIsNotNone(preparation.runtime_integrity)
        assert preparation.runtime_integrity is not None
        self.assertFalse(preparation.runtime_integrity.ok)
        self.assertTrue(
            any("llama-server.exe" in item for item in preparation.runtime_integrity.mismatched)
        )

    def test_unpinned_dll_in_install_directory_fails_before_server_plan(self) -> None:
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        extra = fixture["executable"].parent / "llama-server-impl.dll"
        extra.write_bytes(b"untrusted side-loaded implementation")

        preparation = self.prepare(fixture)

        self.assertEqual(preparation.status, "integrity_failed")
        self.assertIsNone(preparation.plan)
        self.assertIsNotNone(preparation.runtime_integrity)
        assert preparation.runtime_integrity is not None
        self.assertIn(
            "unexpected installed file: llama-pinned/llama-server-impl.dll",
            preparation.runtime_integrity.mismatched,
        )

    def test_profile_rejects_unknown_fields_unsafe_flags_and_escape(self) -> None:
        cases = (
            ("unknown", "value", "unknown fields"),
            ("flags", ["--offline", "--no-mmproj", "--api-key", "leak"], "unsupported flags"),
            ("flags", ["--offline", "--no-mmproj", "--batch-size", "512"], "unsupported flags"),
            ("flags", ["--offline", "--no-mmproj", "--ubatch-size", "256"], "unsupported flags"),
            ("model", "../outside.gguf", "inside the workspace"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                fixture = self.make_workspace()
                self.addCleanup(fixture["temporary"].cleanup)
                profile = dict(fixture["profile"])
                profile[field] = value
                fixture["profile_path"].write_text(json.dumps(profile), encoding="utf-8")
                preparation = self.prepare(fixture)
                self.assertEqual(preparation.status, "configuration_error")
                self.assertIn(expected, " ".join(preparation.errors))


class ServerSupervisorTests(ServerFixture):
    def setUp(self) -> None:
        fixture = self.make_workspace()
        self.addCleanup(fixture["temporary"].cleanup)
        preparation = self.prepare(fixture)
        self.assertTrue(preparation.ok, preparation.errors)
        assert preparation.plan is not None
        self.fixture = fixture
        self.plan: ServerPlan = preparation.plan

    def supervisor(self, **kwargs):
        defaults = {
            "log_directory": self.fixture["root"] / "run-logs",
            "port_probe": lambda _host, _port: True,
            "api_key_factory": lambda: "per-run-super-secret",
            "health_probe": lambda _endpoint, _key, _timeout: (True, None),
        }
        defaults.update(kwargs)
        return LocalServerSupervisor(self.plan, **defaults)

    def test_explicit_external_log_root_keeps_process_plan_and_owns_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_root = Path(temporary).resolve()
            for log_directory in (Path("server"), log_root / "server"):
                with self.subTest(log_directory=log_directory):
                    process = FakeProcess()
                    observed = {}

                    def factory(command, **kwargs):
                        observed["command"] = command
                        observed["cwd"] = kwargs["cwd"]
                        kwargs["stdout"].write(b"server ready\n")
                        return process

                    supervisor = self.supervisor(
                        log_root=log_root,
                        log_directory=log_directory,
                        process_factory=factory,
                    )
                    self.addCleanup(supervisor.stop)
                    result = supervisor.start()
                    self.assertTrue(result.ok, result.as_dict())
                    self.assertEqual(supervisor.log_directory, log_root / "server")
                    self.assertEqual(Path(result.stdout_log).parent, log_root / "server")
                    self.assertEqual(Path(result.stdout_log).read_bytes(), b"server ready\n")
                    self.assertEqual(observed["command"], list(self.plan.command))
                    self.assertEqual(observed["cwd"], str(self.plan.working_directory))
                    self.assertEqual(supervisor.stop().status, "terminated")
                    self.assertIsNone(supervisor.api_key)

    def test_external_log_directory_requires_explicit_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "inside the workspace"):
                self.supervisor(log_directory=Path(temporary) / "server")

    def test_log_directory_cannot_escape_explicit_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_root = Path(temporary).resolve()
            for log_directory in (Path("../escape"), self.fixture["root"] / "logs"):
                with self.subTest(log_directory=log_directory):
                    with self.assertRaisesRegex(ValueError, "inside the workspace"):
                        self.supervisor(log_root=log_root, log_directory=log_directory)

    def test_explicit_log_root_must_be_existing_absolute_nonroot_directory(self) -> None:
        root = self.fixture["root"].resolve()
        for log_root in (Path("relative"), root / "missing", self.fixture["executable"], Path(root.anchor)):
            with self.subTest(log_root=log_root):
                with self.assertRaisesRegex(ValueError, "server log root"):
                    self.supervisor(log_root=log_root, log_directory=Path("server"))

    def test_explicit_log_root_rejects_symlink(self) -> None:
        target = self.fixture["root"] / "log-target"
        target.mkdir()
        linked_root = self.fixture["root"] / "log-link"
        try:
            linked_root.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "regular directory"):
            self.supervisor(log_root=linked_root, log_directory=Path("server"))

    def test_explicit_log_root_rejects_reparse_metadata(self) -> None:
        root = self.fixture["root"].resolve()
        mode = root.stat().st_mode
        with patch("forge8.server.Path.lstat") as lstat:
            lstat.return_value.st_mode = mode
            lstat.return_value.st_file_attributes = 0x400
            with self.assertRaisesRegex(ValueError, "regular directory"):
                self.supervisor(log_root=root, log_directory=Path("server"))

    def test_ready_launch_is_shell_free_secret_env_only(self) -> None:
        process = FakeProcess()
        observed = {}

        def factory(command, **kwargs):
            observed["command"] = command
            observed.update(kwargs)
            # Prove even an accidentally echoed secret is redacted from the
            # machine result while remaining in the protected raw log file.
            kwargs["stderr"].write(b"key=per-run-super-secret\n")
            return process

        supervisor = self.supervisor(process_factory=factory)
        result = supervisor.start()
        self.addCleanup(supervisor.stop)

        self.assertTrue(result.ok, result.as_dict())
        self.assertFalse(observed["shell"])
        self.assertEqual(observed["stdin"], subprocess.DEVNULL)
        self.assertEqual(observed["env"]["LLAMA_API_KEY"], "per-run-super-secret")
        self.assertNotIn("per-run-super-secret", " ".join(observed["command"]))
        self.assertNotIn("per-run-super-secret", json.dumps(result.as_dict()))
        self.assertIn("[REDACTED]", result.stderr_tail)

        shutdown = supervisor.stop()
        self.assertEqual(shutdown.status, "terminated")
        self.assertEqual(process.terminate_calls, 1)
        self.assertIsNone(supervisor.api_key)

    def test_health_retries_until_ready(self) -> None:
        process = FakeProcess()
        clock = FakeClock()
        responses = iter(((False, "loading"), (False, "still loading"), (True, None)))
        supervisor = self.supervisor(
            process_factory=lambda _command, **_kwargs: process,
            health_probe=lambda _endpoint, _key, _timeout: next(responses),
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            health_interval_seconds=0.1,
            startup_timeout_seconds=2.0,
        )

        result = supervisor.start()

        self.assertTrue(result.ok)
        self.assertEqual(result.health_attempts, 3)
        supervisor.stop()

    def test_cancel_before_start_does_not_probe_issue_secret_or_launch(self) -> None:
        factory, port_probe, key_factory = Mock(), Mock(), Mock()
        supervisor = self.supervisor(
            process_factory=factory, port_probe=port_probe,
            api_key_factory=key_factory, cancel_requested=lambda: True,
        )

        result = supervisor.start()

        self.assertEqual(result.status, "interrupted")
        self.assertFalse(result.ok)
        self.assertEqual(result.health_attempts, 0)
        self.assertIsNone(result.pid)
        self.assertIsNone(result.stdout_log)
        self.assertIsNone(supervisor.api_key)
        self.assertEqual(supervisor.shutdown_result.status, "not_started")
        factory.assert_not_called()
        port_probe.assert_not_called()
        key_factory.assert_not_called()
        self.assertFalse(supervisor.log_directory.exists())
        self.assertIs(supervisor.start(), result)

    def test_cancel_after_secret_creation_still_prevents_launch(self) -> None:
        cancelled = []
        factory = Mock()

        def key_factory():
            cancelled.append(True)
            return "per-run-super-secret"

        supervisor = self.supervisor(
            process_factory=factory, api_key_factory=key_factory,
            cancel_requested=lambda: bool(cancelled),
        )
        result = supervisor.start()

        self.assertEqual(result.status, "interrupted")
        factory.assert_not_called()
        self.assertIsNone(supervisor.api_key)
        self.assertEqual(supervisor.shutdown_result.status, "not_started")
        self.assertTrue(supervisor._stdout_stream.closed)
        self.assertTrue(supervisor._stderr_stream.closed)

    def test_cancel_during_health_wait_reaps_child_and_retains_redacted_logs(self) -> None:
        for stubborn in (False, True):
            with self.subTest(stubborn=stubborn):
                process = FakeProcess(stubborn=stubborn)
                clock = FakeClock()

                def factory(_command, **kwargs):
                    kwargs["stderr"].write(b"loading per-run-super-secret\n")
                    return process

                supervisor = self.supervisor(
                    process_factory=factory,
                    health_probe=lambda *_args: (False, "loading per-run-super-secret"),
                    monotonic=clock.monotonic, sleeper=clock.sleep,
                    health_interval_seconds=0.1,
                    cancel_requested=lambda: clock.now >= 0.1,
                )
                self.addCleanup(supervisor.stop)
                with supervisor:
                    result = supervisor.start_result
                    self.assertEqual(result.status, "interrupted")
                    self.assertEqual(result.health_attempts, 1)
                    self.assertEqual(result.pid, process.pid)
                    self.assertIsNone(supervisor.api_key)
                    self.assertIn("loading [REDACTED]", result.stderr_tail)
                    self.assertEqual(result.health_last_error, "loading [REDACTED]")
                    self.assertNotIn("per-run-super-secret", json.dumps(result.as_dict()))
                    self.assertEqual(Path(result.stderr_log).read_bytes(), b"loading per-run-super-secret\n")
                self.assertEqual(clock.now, 0.1)
                self.assertTrue(supervisor.shutdown_result.ok)
                self.assertEqual(supervisor.shutdown_result.status, "killed" if stubborn else "terminated")
                self.assertEqual(process.terminate_calls, 1)
                self.assertEqual(process.kill_calls, int(stubborn))
                self.assertEqual(process.wait_timeouts, [5.0, 2.0] if stubborn else [5.0])
                self.assertTrue(supervisor._stdout_stream.closed)
                self.assertTrue(supervisor._stderr_stream.closed)

    def test_cancel_during_successful_health_probe_does_not_report_ready(self) -> None:
        process = FakeProcess()
        cancelled = []

        def health_probe(*_args):
            cancelled.append(True)
            return True, None

        supervisor = self.supervisor(
            process_factory=lambda *_args, **_kwargs: process,
            health_probe=health_probe, cancel_requested=lambda: bool(cancelled),
        )
        self.addCleanup(supervisor.stop)
        result = supervisor.start()

        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.health_attempts, 1)
        self.assertEqual(supervisor.shutdown_result.status, "terminated")
        self.assertEqual(process.terminate_calls, 1)
        self.assertIsNone(supervisor.api_key)

    def test_port_collision_never_launches_process(self) -> None:
        called = []
        supervisor = self.supervisor(
            port_probe=lambda _host, _port: False,
            process_factory=lambda *_args, **_kwargs: called.append(True),
        )

        result = supervisor.start()

        self.assertEqual(result.status, "port_collision")
        self.assertEqual(called, [])
        self.assertIsNone(supervisor.api_key)
        json.dumps(result.as_dict())

    def test_early_bind_exit_is_classified_as_port_collision(self) -> None:
        process = FakeProcess(return_code=1)

        def factory(_command, **kwargs):
            kwargs["stderr"].write(b"error: bind failed: WSAEADDRINUSE\n")
            return process

        supervisor = self.supervisor(process_factory=factory)
        result = supervisor.start()

        self.assertEqual(result.status, "port_collision")
        self.assertEqual(result.return_code, 1)
        self.assertIn("WSAEADDRINUSE", result.stderr_tail)

    def test_non_bind_early_exit_is_machine_readable(self) -> None:
        process = FakeProcess(return_code=7)
        supervisor = self.supervisor(process_factory=lambda _command, **_kwargs: process)

        result = supervisor.start()

        self.assertEqual(result.status, "early_exit")
        self.assertEqual(result.return_code, 7)
        self.assertFalse(result.ok)

    def test_startup_timeout_terminates_process(self) -> None:
        process = FakeProcess()
        clock = FakeClock()
        supervisor = self.supervisor(
            process_factory=lambda _command, **_kwargs: process,
            health_probe=lambda _endpoint, _key, _timeout: (False, "loading"),
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            startup_timeout_seconds=0.2,
            health_interval_seconds=0.05,
        )

        result = supervisor.start()

        self.assertEqual(result.status, "startup_timeout")
        self.assertGreater(result.health_attempts, 0)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(supervisor.shutdown_result.status if supervisor.shutdown_result else None, "terminated")
        self.assertIsNone(supervisor.api_key)

    def test_launch_error_is_machine_readable(self) -> None:
        def factory(_command, **_kwargs):
            raise FileNotFoundError("runtime vanished")

        supervisor = self.supervisor(process_factory=factory)
        result = supervisor.start()

        self.assertEqual(result.status, "launch_error")
        self.assertIn("runtime vanished", result.error or "")
        self.assertIsNone(supervisor.api_key)

    def test_context_manager_shutdown_escalates_terminate_to_bounded_kill(self) -> None:
        process = FakeProcess(stubborn=True)
        supervisor = self.supervisor(process_factory=lambda _command, **_kwargs: process)

        with supervisor as running:
            self.assertTrue(running.start_result.ok if running.start_result else False)

        self.assertIsNotNone(supervisor.shutdown_result)
        assert supervisor.shutdown_result is not None
        self.assertEqual(supervisor.shutdown_result.status, "killed")
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_timeouts, [5.0, 2.0])

    def test_terminate_error_still_kills_waits_redacts_and_is_idempotent(self) -> None:
        process = FakeProcess(
            terminate_error=OSError("terminate leaked per-run-super-secret")
        )
        supervisor = self.supervisor(process_factory=lambda _command, **_kwargs: process)
        self.assertTrue(supervisor.start().ok)

        shutdown = supervisor.stop()

        self.assertEqual(shutdown.status, "killed")
        self.assertTrue(shutdown.ok)
        self.assertEqual(shutdown.return_code, -9)
        self.assertFalse(shutdown.terminate_sent)
        self.assertTrue(shutdown.kill_sent)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_timeouts, [2.0])
        encoded = json.dumps(shutdown.as_dict())
        self.assertIn("terminate failed", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertNotIn("per-run-super-secret", encoded)

        repeated = supervisor.stop()
        self.assertIs(repeated, shutdown)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_timeouts, [2.0])

    def test_first_wait_error_still_kills_and_bounded_waits(self) -> None:
        process = FakeProcess(
            wait_errors=(OSError("wait leaked per-run-super-secret"), None)
        )
        supervisor = self.supervisor(process_factory=lambda _command, **_kwargs: process)
        self.assertTrue(supervisor.start().ok)

        shutdown = supervisor.stop()

        self.assertEqual(shutdown.status, "killed")
        self.assertTrue(shutdown.ok)
        self.assertEqual(shutdown.return_code, -9)
        self.assertTrue(shutdown.terminate_sent)
        self.assertTrue(shutdown.kill_sent)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_timeouts, [5.0, 2.0])
        encoded = json.dumps(shutdown.as_dict())
        self.assertIn("wait after terminate failed", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertNotIn("per-run-super-secret", encoded)

    def test_unproven_exit_after_api_errors_is_never_acceptable(self) -> None:
        process = FakeProcess(
            terminate_error=OSError("terminate leaked per-run-super-secret"),
            kill_error=OSError("kill leaked per-run-super-secret"),
            wait_errors=(OSError("reap leaked per-run-super-secret"),),
        )
        supervisor = self.supervisor(process_factory=lambda _command, **_kwargs: process)
        self.assertTrue(supervisor.start().ok)
        process.poll_errors.extend(
            (
                OSError("initial poll leaked per-run-super-secret"),
                OSError("final poll leaked per-run-super-secret"),
            )
        )

        shutdown = supervisor.stop()

        self.assertEqual(shutdown.status, "shutdown_error")
        self.assertFalse(shutdown.ok)
        self.assertIsNone(shutdown.return_code)
        self.assertFalse(shutdown.terminate_sent)
        self.assertFalse(shutdown.kill_sent)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_timeouts, [2.0])
        encoded = json.dumps(shutdown.as_dict())
        self.assertIn("terminate failed", encoded)
        self.assertIn("kill failed", encoded)
        self.assertIn("wait after kill failed", encoded)
        self.assertIn("initial poll failed", encoded)
        self.assertIn("poll after kill wait failure failed", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertNotIn("per-run-super-secret", encoded)

        repeated = supervisor.stop()
        self.assertIs(repeated, shutdown)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_timeouts, [2.0])

    def test_log_close_errors_are_redacted_and_cannot_reenter_shutdown(self) -> None:
        process = FakeProcess()
        supervisor = self.supervisor(process_factory=lambda _command, **_kwargs: process)
        self.assertTrue(supervisor.start().ok)
        supervisor._close_logs()
        stdout = FaultingCloseStream(OSError("stdout leaked per-run-super-secret"))
        stderr = FaultingCloseStream(OSError("stderr leaked per-run-super-secret"))
        supervisor._stdout_stream = stdout
        supervisor._stderr_stream = stderr

        shutdown = supervisor.stop()

        self.assertEqual(shutdown.status, "terminated")
        self.assertTrue(shutdown.ok)
        self.assertEqual(shutdown.return_code, -15)
        self.assertEqual(stdout.close_calls, 1)
        self.assertEqual(stderr.close_calls, 1)
        encoded = json.dumps(shutdown.as_dict())
        self.assertIn("stdout log close failed", encoded)
        self.assertIn("stderr log close failed", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertNotIn("per-run-super-secret", encoded)

        repeated = supervisor.stop()
        self.assertIs(repeated, shutdown)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)
        self.assertEqual(stdout.close_calls, 1)
        self.assertEqual(stderr.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
