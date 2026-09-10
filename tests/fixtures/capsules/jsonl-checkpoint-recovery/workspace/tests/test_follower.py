from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from eventspool import JsonlFollower


class JsonlFollowerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "events.jsonl"
        self.checkpoint = root / "state" / "checkpoint.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_complete_records_are_not_replayed(self) -> None:
        self.source.write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")
        follower = JsonlFollower(self.source, self.checkpoint)

        self.assertEqual(follower.poll(), [{"id": 1}, {"id": 2}])
        self.assertEqual(follower.poll(), [])

        restarted = JsonlFollower(self.source, self.checkpoint)
        self.assertEqual(restarted.poll(), [])

    def test_checkpoint_is_json(self) -> None:
        self.source.write_text('{"ok": true}\n', encoding="utf-8")
        JsonlFollower(self.source, self.checkpoint).poll()

        payload = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        self.assertGreater(payload["offset"], 0)


if __name__ == "__main__":
    unittest.main()
