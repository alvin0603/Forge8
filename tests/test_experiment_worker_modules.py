from __future__ import annotations

from copy import deepcopy
import ast
import hashlib
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch

from forge8 import _experiment_worker as worker


class ExperimentWorkerModulesTests(unittest.TestCase):
    """Only owned bytes and mocked engines: never execute any guest bootstrap/source."""

    def test_guest_probes_top_level_conflicts_before_exposing_selected_zip(self) -> None:
        # Static contract, not a claim of executing the target interpreter.
        tree = ast.parse(worker.GUEST_BOOTSTRAP)
        branch = next(node for node in tree.body if isinstance(node, ast.Try)).body[0]
        self.assertIsInstance(branch, ast.If)
        guard = next(node for node in branch.body if isinstance(node, ast.If))
        probe = next(node for node in ast.walk(guard.test) if isinstance(node, ast.Call)
                     and ast.unparse(node.func) == "importlib.util.find_spec")
        self.assertEqual([ast.unparse(arg) for arg in probe.args], ["name"])
        generator = next(node for node in ast.walk(guard.test) if isinstance(node, ast.comprehension))
        self.assertEqual(ast.unparse(generator.iter), "bundle['top_levels']")
        self.assertIsInstance(guard.body[0], ast.Raise)
        expose = next(node for node in branch.body if isinstance(node, ast.Expr)
                      and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "sys.path.append")
        self.assertLess(guard.lineno, expose.lineno)
        self.assertEqual(ast.literal_eval(expose.value.args[0]), "/modules/selected.zip")

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.request_path = self.root / "worker-input.json"
        self.request_path.write_bytes(b"{}")
        self.modules = self.root / "modules"
        self.modules.mkdir()
        self.archive = self.modules / "selected.zip"
        # Archive construction/content validation belongs to the controller;
        # these inert bytes test the worker's exact file/pin boundary only.
        self.archive_bytes = b"owned inert archive bytes; never interpreted"
        self.archive.write_bytes(self.archive_bytes)
        self.bundle = {"entry_module": "owned.logic", "top_levels": ["owned"],
                       "sha256": hashlib.sha256(self.archive_bytes).hexdigest(),
                       "size_bytes": len(self.archive_bytes)}
        self.request = {"source": "raise AssertionError('must not execute on host')\n",
                        "entry": "entry", "input": {"args": [9007199254740993], "kwargs": {}}}
        installed = self.root / "installed"
        installed.mkdir()
        self.cache_bytes = b"owned inert cache bytes; deserialize is always mocked"
        (installed / "python.cwasm").write_bytes(self.cache_bytes)
        self.cache_pin = {"size_bytes": len(self.cache_bytes),
                          "sha256": hashlib.sha256(self.cache_bytes).hexdigest()}

    def test_exact_adjacent_directory_and_pin_are_admitted_without_mutating_metadata(self) -> None:
        original = deepcopy(self.bundle)
        self.assertEqual(worker._module_bundle_directory(self.request_path, self.bundle), self.modules)
        self.assertEqual(self.bundle, original)

    def test_malformed_bundle_rejected_before_any_runtime_loading(self) -> None:
        invalid = [[], {}, {**self.bundle, "host_path": str(self.root)}]
        invalid += [{**self.bundle, "entry_module": name} for name in
                    ("", ".owned", "owned..logic", "../owned", "other.logic", "套件.logic", 1)]
        invalid += [{**self.bundle, "top_levels": roots} for roots in
                    ([], ["owned", "owned"], ["z", "owned"], ["owned.logic"], [1], "owned")]
        invalid += [{**self.bundle, "size_bytes": size} for size in (True, 0, -1, 131073, "12")]
        invalid += [{**self.bundle, "sha256": digest} for digest in (None, "a" * 63, "A" * 64, "z" * 64)]
        for bundle in invalid:
            with self.subTest(bundle=bundle), patch.object(worker, "_runtime") as runtime:
                with self.assertRaises(ValueError):
                    worker.execute(self.root, self.request, self.cache_pin,
                                   module_bundle=bundle, request_path=self.request_path)
                runtime.assert_not_called()

    def test_extra_directory_member_wrong_size_and_wrong_digest_are_rejected(self) -> None:
        extra = self.modules / "private.json"
        extra.write_bytes(b"not exposed")
        with self.assertRaisesRegex(ValueError, "only selected.zip"):
            worker._module_bundle_directory(self.request_path, self.bundle)
        extra.unlink()
        for changed in ({"size_bytes": len(self.archive_bytes) + 1}, {"sha256": "0" * 64}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                worker._module_bundle_directory(self.request_path, {**self.bundle, **changed})

    def test_nonregular_hardlinked_and_reparse_inputs_are_rejected(self) -> None:
        real_lstat = Path.lstat
        for target in (self.archive, self.request_path, self.modules):
            changes = ([{"st_mode": stat.S_IFIFO | 0o600}, {"st_nlink": 2},
                        {"st_file_attributes": 0x400}]
                       if target != self.modules else [{"st_file_attributes": 0x400}])
            for changed in changes:
                actual = real_lstat(target)
                metadata = SimpleNamespace(**{name: getattr(actual, name) for name in dir(actual)
                                              if name.startswith("st_")})
                for name, value in changed.items():
                    setattr(metadata, name, value)

                def lstat(path, *args, **kwargs):
                    return metadata if path == target else real_lstat(path, *args, **kwargs)

                with self.subTest(target=target.name, changed=changed), \
                        patch.object(Path, "resolve", lambda path, **kwargs: path), \
                        patch.object(Path, "lstat", lstat):
                    with self.assertRaises(ValueError):
                        worker._module_bundle_directory(self.request_path, self.bundle)

    def test_missing_relative_and_redirected_request_paths_are_rejected(self) -> None:
        for path in (None, Path("worker-input.json")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                worker._module_bundle_directory(path, self.bundle)
        real_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            return self.root / "elsewhere" if path == self.archive else real_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve), self.assertRaisesRegex(ValueError, "redirected"):
            worker._module_bundle_directory(self.request_path, self.bundle)

    def test_changed_open_handle_stat_is_rejected(self) -> None:
        actual = self.archive.stat()
        changed = SimpleNamespace(**{name: getattr(actual, name) for name in dir(actual)
                                     if name.startswith("st_")})
        changed.st_ino += 1
        with patch.object(worker.os, "fstat", return_value=changed), \
                self.assertRaisesRegex(ValueError, "changed before reading"):
            worker._module_bundle_directory(self.request_path, self.bundle)

    def test_windows_archive_birth_identity_and_independent_ctimes(self) -> None:
        real_lstat = Path.lstat
        actual = real_lstat(self.archive)
        before = SimpleNamespace(**{name: getattr(actual, name) for name in dir(actual)
                                    if name.startswith("st_")})
        before.st_birthtime_ns, before.st_ctime_ns = 50, 100
        for change in (None, "birth", "descriptor", "path"):
            with self.subTest(change=change):
                opened = deepcopy(before)
                opened.st_ctime_ns = 200  # A different API meaning, not drift.
                final, current = deepcopy(opened), deepcopy(before)
                if change == "birth":
                    final.st_birthtime_ns += 1
                elif change == "descriptor":
                    final.st_ctime_ns += 1
                elif change == "path":
                    current.st_ctime_ns += 1
                archive_stats = iter((before, before, current))

                def lstat(path, *args, **kwargs):
                    return next(archive_stats) if path == self.archive else real_lstat(path, *args, **kwargs)

                native = SimpleNamespace(name="nt", fstat=MagicMock(side_effect=[opened, final]))
                with patch.object(worker, "os", native), patch.object(Path, "lstat", lstat):
                    if change:
                        with self.assertRaisesRegex(ValueError, "archive integrity failed"):
                            worker._module_bundle_directory(self.request_path, self.bundle)
                    else:
                        self.assertEqual(worker._module_bundle_directory(self.request_path, self.bundle), self.modules)

    def test_execute_keeps_legacy_mount_and_adds_only_readonly_bundle_mount_when_selected(self) -> None:
        for bundled in (False, True):
            runtime = MagicMock()
            runtime.ExitTrap = type("OwnedExitTrap", (Exception,), {})
            runtime.Trap = type("OwnedTrap", (Exception,), {})
            module = runtime.Module.deserialize.return_value.__enter__.return_value
            module.imports = []
            linker = runtime.Linker.return_value.__enter__.return_value
            start = MagicMock()  # No guest code or bootstrap evaluation.
            linker.instantiate.return_value.exports.return_value = {"_start": start}
            original = deepcopy(self.request)
            options = {"module_bundle": self.bundle, "request_path": self.request_path} if bundled else {}
            with self.subTest(bundled=bundled), \
                    patch.object(worker, "_runtime", return_value=(runtime, object())), \
                    patch.object(worker.threading, "Timer") as timer:
                report = worker.execute(self.root, self.request, self.cache_pin, **options)
            wasi = runtime.WasiConfig.return_value
            mounts = [call(str(self.root / "installed/guest/lib"), "/lib", fs_mutable=False)]
            if bundled:
                mounts.append(call(str(self.modules), "/modules", fs_mutable=False))
            self.assertEqual(wasi.preopen_dir.call_args_list, mounts)
            self.assertEqual(wasi.env, [])
            self.assertEqual(wasi.argv[:5], ["python", "-I", "-S", "-B", "-c"])
            self.assertEqual(wasi.argv[5], worker.GUEST_BOOTSTRAP)
            expected = deepcopy(self.request)
            if bundled:
                expected["module_bundle"] = {key: self.bundle[key] for key in ("entry_module", "top_levels")}
            self.assertEqual(json.loads(wasi.argv[6]), expected)
            self.assertEqual(self.request, original)
            store = runtime.Store.return_value.__enter__.return_value
            store.set_limits.assert_called_once_with(memory_size=128 * 1024**2, table_elements=100_000,
                                                     instances=1, memories=1, tables=1)
            timer.assert_called_once_with(5.0, runtime.Engine.return_value.__enter__.return_value.increment_epoch)
            timer.return_value.cancel.assert_called_once_with()
            timer.return_value.join.assert_called_once_with()
            start.assert_called_once_with(store)
            self.assertEqual(report["guest_output"], {"stdout": "", "stderr": ""})

    def test_main_forwards_actual_request_path_only_in_explicit_bundle_mode(self) -> None:
        for bundled in (False, True):
            payload = {"request": self.request, "cache_pin": self.cache_pin}
            if bundled:
                payload["module_bundle"] = self.bundle
            self.request_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.subTest(bundled=bundled), \
                    patch.object(worker.sys, "argv", ["worker", "run", str(self.root), str(self.request_path)]), \
                    patch.object(worker, "execute", return_value={}) as execute, patch("builtins.print"):
                worker.main()
            options = {"module_bundle": self.bundle, "request_path": self.request_path} if bundled else {}
            execute.assert_called_once_with(self.root, self.request, self.cache_pin, **options)
        self.request_path.write_text(json.dumps({**payload, "module_bundle": None}), encoding="utf-8")
        with patch.object(worker.sys, "argv", ["worker", "run", str(self.root), str(self.request_path)]), \
                patch.object(worker, "execute") as execute, self.assertRaises(ValueError):
            worker.main()
        execute.assert_not_called()

    def test_bundle_changed_during_engine_loading_is_rejected_before_its_mount(self) -> None:
        runtime = MagicMock()
        module = runtime.Module.deserialize.return_value.__enter__.return_value
        module.imports = []

        def runtime_loaded(*args, **kwargs):
            self.archive.write_bytes(b"changed after initial admission")
            return runtime, object()

        with patch.object(worker, "_runtime", side_effect=runtime_loaded), \
                patch.object(worker.threading, "Timer") as timer, self.assertRaises(ValueError):
            worker.execute(self.root, self.request, self.cache_pin,
                           module_bundle=self.bundle, request_path=self.request_path)
        runtime.WasiConfig.return_value.preopen_dir.assert_called_once_with(
            str(self.root / "installed/guest/lib"), "/lib", fs_mutable=False)
        runtime.Linker.return_value.__enter__.return_value.instantiate.assert_not_called()
        runtime.Module.deserialize.return_value.__exit__.assert_called_once()
        runtime.Engine.return_value.__exit__.assert_called_once()
        timer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
