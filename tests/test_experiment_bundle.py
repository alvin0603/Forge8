"""Owned ZIP fixtures only; no interpreter, guest or source module executes."""
from __future__ import annotations

from pathlib import Path
import stat
import tempfile
import unittest
import warnings
import zipfile

from forge8.experiments import _unpack


PREFIX = "lib/python3.14/"
ENCODINGS = PREFIX + "encodings/__init__.py"


class ExperimentBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="forge8-bundle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.members = [
            ("python.wasm", b"owned wasm placeholder; never executed"),
            ("LICENSE", b"owned license\r\n"),
            ("lib/", b""),
            (PREFIX, b""),
            (PREFIX + "package/data/message.txt", "\u4f60\u597d\r\nsecond line\n".encode("utf-8")),
            (PREFIX + "package/data/", b""),
            (PREFIX + "package/__init__.py", b""),
            (PREFIX + "package/", b""),
            (PREFIX + "module_\u8cc7\u6599.py", "VALUE = '\u4f60'\r\n".encode("utf-8")),
            (PREFIX + "encodings/", b""),
            (ENCODINGS, b"# owned encoding fixture\r\n"),
            (PREFIX + "empty-directory/", b""),
            (PREFIX + "lib-dynload/", b""),
        ]

    def archive(self, members, name="owned.zip", *, system=3,
                date=(2024, 2, 4, 6, 8, 10), permissions=0o600):
        path = self.root / name
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # Intentional duplicate-path fixtures.
            with zipfile.ZipFile(path, "w") as package:
                for name, data in members:
                    if isinstance(name, zipfile.ZipInfo):
                        member = name
                    else:
                        # Preserve raw unsafe paths even on native Windows.
                        member = zipfile.ZipInfo()
                        member.filename = member.orig_filename = name
                        member.date_time = date
                        member.create_system = system
                        kind = stat.S_IFDIR if name.endswith("/") else stat.S_IFREG
                        member.external_attr = (kind | permissions) << 16
                    package.writestr(member, data)
        return path

    def test_every_stdlib_member_preserves_exact_bytes_and_original_archive(self) -> None:
        archive = self.archive(self.members)
        original = archive.read_bytes()
        destination = self.root / "guest"
        _unpack(archive, destination, bundle_stdlib=True)
        expected = {name[len(PREFIX):]: data for name, data in self.members
                    if name.startswith(PREFIX) and name != PREFIX}
        with zipfile.ZipFile(destination / "lib/python314.zip") as bundled:
            self.assertEqual({item.filename: bundled.read(item) for item in bundled.infolist()}, expected)
        self.assertEqual(archive.read_bytes(), original)

    def test_only_bundle_and_empty_physical_dynload_remain_with_outside_files(self) -> None:
        destination = self.root / "guest"
        _unpack(self.archive(self.members), destination, bundle_stdlib=True)
        self.assertEqual(
            sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()),
            ["LICENSE", "lib/python314.zip", "python.wasm"],
        )
        self.assertEqual(
            sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_dir()),
            ["lib", "lib/python3.14", "lib/python3.14/lib-dynload"],
        )
        self.assertEqual(list((destination / "lib/python3.14/lib-dynload").iterdir()), [])
        for name in ("LICENSE", "python.wasm"):
            self.assertEqual((destination / name).read_bytes(), dict(self.members)[name])

    def test_bundle_bytes_and_fixed_metadata_ignore_source_order_os_and_timestamps(self) -> None:
        first = self.archive(self.members, "first.zip")
        second = self.archive(list(reversed(self.members)), "second.zip", system=0,
                              date=(2025, 8, 10, 12, 14, 16), permissions=0o777)
        first_destination, second_destination = self.root / "first", self.root / "second"
        _unpack(first, first_destination, bundle_stdlib=True)
        _unpack(second, second_destination, bundle_stdlib=True)
        first_bundle = first_destination / "lib/python314.zip"
        second_bundle = second_destination / "lib/python314.zip"
        self.assertNotEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first_bundle.read_bytes(), second_bundle.read_bytes())
        with zipfile.ZipFile(first_bundle) as bundled:
            self.assertEqual(bundled.namelist(), sorted(bundled.namelist()))
            for item in bundled.infolist():
                with self.subTest(name=item.filename):
                    self.assertEqual(item.compress_type, zipfile.ZIP_STORED)
                    self.assertEqual(item.date_time, (1980, 1, 1, 0, 0, 0))
                    self.assertEqual(item.create_system, 3)
                    mode = (stat.S_IFDIR | 0o755) if item.is_dir() else (stat.S_IFREG | 0o644)
                    self.assertEqual(item.external_attr, mode << 16)
                    self.assertEqual(item.extra, b"")
                    self.assertEqual(item.comment, b"")

    def test_incompatible_layout_is_rejected_before_destination_creation(self) -> None:
        without_encodings = [(name, data) for name, data in self.members if name != ENCODINGS]
        cases = [
            ("existing-bundle", self.members + [("lib/python314.zip", b"unexpected")]),
            ("native-extension", self.members + [(PREFIX + "lib-dynload/native.so", b"owned")]),
            ("missing-encodings", without_encodings),
            ("wrong-case-encodings", without_encodings + [(PREFIX + "Encodings/__init__.py", b"")]),
            ("directory-not-encodings-module", without_encodings + [(ENCODINGS + "/", b"")]),
        ]
        for name, members in cases:
            with self.subTest(name=name):
                destination = self.root / name
                with self.assertRaisesRegex(ValueError, "pinned stdlib ZIP layout"):
                    _unpack(self.archive(members), destination, bundle_stdlib=True)
                self.assertFalse(destination.exists())

    def test_existing_raw_path_and_link_gates_apply_before_bundling(self) -> None:
        link = zipfile.ZipInfo(PREFIX + "link.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for index, name in enumerate((
            "../escape.py", PREFIX + "../escape.py", "lib\\python3.14\\escape.py",
            PREFIX + "package/./escape.py", ENCODINGS, link,
        )):
            with self.subTest(name=name):
                destination = self.root / f"unsafe-{index}"
                archive = self.archive(self.members + [(name, b"owned")])
                with self.assertRaises(ValueError):
                    _unpack(archive, destination, bundle_stdlib=True)
                self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
