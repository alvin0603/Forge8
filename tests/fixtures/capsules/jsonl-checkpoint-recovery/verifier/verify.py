#!/usr/bin/env python3
"""Black-box verifier for the JSONL checkpoint recovery capsule."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


CAPSULE_ROOT = Path(__file__).resolve().parents[1]
SUBMITTED_WORKSPACE = CAPSULE_ROOT / "workspace"


class FollowerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = tempfile.TemporaryDirectory(prefix="forge8-jsonl-")
        cls.workspace = Path(cls.sandbox.name) / "workspace"
        shutil.copytree(SUBMITTED_WORKSPACE, cls.workspace)
        sys.path.insert(0, str(cls.workspace))
        from eventspool import JsonlFollower, MalformedEvent

        cls.JsonlFollower = JsonlFollower
        cls.MalformedEvent = MalformedEvent

    @classmethod
    def tearDownClass(cls) -> None:
        sys.path.remove(str(cls.workspace))
        cls.sandbox.cleanup()

    def setUp(self) -> None:
        self.case = Path(tempfile.mkdtemp(dir=self.sandbox.name))
        self.source = self.case / "events.jsonl"
        self.checkpoint = self.case / "state.json"

    def checkpoint_offset(self) -> int:
        payload = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        offset = payload["offset"]
        self.assertIs(type(offset), int)
        self.assertGreaterEqual(offset, 0)
        return offset

    def test_complete_prefix_is_committed_but_partial_tail_is_deferred(self) -> None:
        complete = b'{"id":1,"kind":"ready"}\n'
        self.source.write_bytes(complete + b'{"id":2,"kind":"pend')
        follower = self.JsonlFollower(self.source, self.checkpoint)

        self.assertEqual(follower.poll(), [{"id": 1, "kind": "ready"}])
        self.assertEqual(self.checkpoint_offset(), len(complete))

        with self.source.open("ab") as handle:
            handle.write(b'ing"}\n')
        restarted = self.JsonlFollower(self.source, self.checkpoint)
        self.assertEqual(restarted.poll(), [{"id": 2, "kind": "pending"}])
        self.assertEqual(restarted.poll(), [])

    def test_split_utf8_codepoint_is_deferred_across_restart(self) -> None:
        encoded = '{"id":7,"label":"café"}\n'.encode("utf-8")
        split_at = encoded.index("é".encode("utf-8")) + 1
        self.source.write_bytes(encoded[:split_at])

        first = self.JsonlFollower(self.source, self.checkpoint)
        self.assertEqual(first.poll(), [])
        if self.checkpoint.exists():
            self.assertEqual(self.checkpoint_offset(), 0)

        with self.source.open("ab") as handle:
            handle.write(encoded[split_at:])
        restarted = self.JsonlFollower(self.source, self.checkpoint)
        self.assertEqual(restarted.poll(), [{"id": 7, "label": "café"}])

    def test_blank_complete_lines_advance_the_checkpoint(self) -> None:
        prefix = b"\n  \n\t\n"
        self.source.write_bytes(prefix + b'{"id":9')
        follower = self.JsonlFollower(self.source, self.checkpoint)

        self.assertEqual(follower.poll(), [])
        self.assertEqual(self.checkpoint_offset(), len(prefix))
        with self.source.open("ab") as handle:
            handle.write(b"}\n")
        self.assertEqual(follower.poll(), [{"id": 9}])

    def test_complete_malformed_record_is_not_consumed(self) -> None:
        self.source.write_bytes(b'{"id": broken}\n')
        follower = self.JsonlFollower(self.source, self.checkpoint)

        with self.assertRaises(self.MalformedEvent) as caught:
            follower.poll()
        self.assertEqual(caught.exception.line_number, 1)
        self.assertIsInstance(caught.exception.detail, str)
        if self.checkpoint.exists():
            self.assertEqual(self.checkpoint_offset(), 0)

        self.source.write_bytes(b'{"id":10,"fixed":true}\n')
        self.assertEqual(follower.poll(), [{"id": 10, "fixed": True}])

    def test_valid_record_is_not_lost_when_later_record_is_malformed(self) -> None:
        self.source.write_bytes(b'{"id":1}\n{"id": nope}\n')
        follower = self.JsonlFollower(self.source, self.checkpoint)
        with self.assertRaises(self.MalformedEvent):
            follower.poll()

        self.source.write_bytes(b'{"id":1}\n{"id":2}\n')
        self.assertEqual(follower.poll(), [{"id": 1}, {"id": 2}])

    def test_non_object_json_is_malformed_and_retryable(self) -> None:
        self.source.write_bytes(b"[1,2,3]\n")
        follower = self.JsonlFollower(self.source, self.checkpoint)
        with self.assertRaises(self.MalformedEvent):
            follower.poll()
        self.source.write_bytes(b'{"items":[1,2,3]}\n')
        self.assertEqual(follower.poll(), [{"items": [1, 2, 3]}])

    def test_truncation_restarts_from_beginning(self) -> None:
        self.source.write_text(
            '{"id":1,"padding":"xxxxxxxxxxxxxxxxxxxxxxxx"}\n',
            encoding="utf-8",
        )
        follower = self.JsonlFollower(self.source, self.checkpoint)
        self.assertEqual([item["id"] for item in follower.poll()], [1])

        self.source.write_text('{"id":2}\n', encoding="utf-8")
        self.assertEqual(follower.poll(), [{"id": 2}])
        self.assertEqual(follower.poll(), [])


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FollowerContractTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {
        "capsule": "repository.jsonl-checkpoint-recovery",
        "passed": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
