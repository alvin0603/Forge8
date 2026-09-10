from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from forge8.trace import GENESIS_HASH, TraceError, TraceLedger, verify_trace


class TraceLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="forge8-trace-test-")
        self.root = Path(self.temporary.name)
        self.trace = self.root / "trace.jsonl"
        self.seal = self.root / "trace.seal.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_append_seal_and_verify(self) -> None:
        ledger = TraceLedger(self.trace, self.seal)
        first = ledger.append("state.entered", {"state": "SCAN"})
        second = ledger.append(
            "tool.completed", {"tool": "read_text", "ok": True, "bytes": 42}
        )
        sealed = ledger.seal()

        self.assertEqual(first.sequence, 1)
        self.assertEqual(first.previous_hash, GENESIS_HASH)
        self.assertEqual(second.previous_hash, first.event_hash)
        self.assertEqual(sealed.event_count, 2)
        self.assertEqual(sealed.head_hash, second.event_hash)
        verified = verify_trace(self.trace, self.seal)
        self.assertTrue(verified.ok, verified.errors)
        self.assertEqual(verified.event_count, 2)

    def test_resume_unsealed_trace_continues_chain(self) -> None:
        first_writer = TraceLedger(self.trace, self.seal)
        first = first_writer.append("one", {"value": 1})

        resumed = TraceLedger(self.trace, self.seal)
        second = resumed.append("two", {"value": 2})

        self.assertEqual(resumed.event_count, 2)
        self.assertEqual(second.previous_hash, first.event_hash)
        resumed.seal()
        self.assertTrue(verify_trace(self.trace, self.seal).ok)

    def test_seal_detects_tampering_and_truncation(self) -> None:
        ledger = TraceLedger(self.trace, self.seal)
        ledger.append("one", {"value": "original"})
        ledger.append("two", {"value": "second"})
        ledger.seal()
        original = self.trace.read_text(encoding="utf-8")

        self.trace.write_text(
            original.replace("original", "tampered"), encoding="utf-8"
        )
        tampered = verify_trace(self.trace, self.seal)
        self.assertFalse(tampered.ok)
        self.assertTrue(
            any("hash mismatch" in error for error in tampered.errors), tampered.errors
        )

        self.trace.write_text(original.splitlines(keepends=True)[0], encoding="utf-8")
        truncated = verify_trace(self.trace, self.seal)
        self.assertFalse(truncated.ok)
        self.assertIn("seal event count mismatch", truncated.errors)

    def test_sealed_trace_refuses_resume_or_append(self) -> None:
        ledger = TraceLedger(self.trace, self.seal)
        ledger.append("done", {})
        ledger.seal()

        with self.assertRaisesRegex(TraceError, "already sealed"):
            TraceLedger(self.trace, self.seal)
        with self.assertRaisesRegex(TraceError, "sealed"):
            ledger.append("late", {})

    def test_non_json_and_non_finite_payloads_fail_before_write(self) -> None:
        ledger = TraceLedger(self.trace, self.seal)
        with self.assertRaisesRegex(TraceError, "canonical JSON"):
            ledger.append("bad", {"path": Path("not-json")})
        with self.assertRaisesRegex(TraceError, "canonical JSON"):
            ledger.append("bad", {"number": float("nan")})
        self.assertFalse(self.trace.exists())

    def test_event_encoding_is_canonical_and_single_line(self) -> None:
        ledger = TraceLedger(self.trace, self.seal)
        event = ledger.append("unicode", {"z": 1, "a": "臺灣\nline"})
        line = self.trace.read_text(encoding="utf-8")
        payload = json.loads(line)

        self.assertEqual(line.count("\n"), 1)
        self.assertIn("臺灣", line)
        self.assertEqual(payload["event_hash"], event.event_hash)
        self.assertLess(line.index('"a"'), line.index('"z"'))

    def test_unicode_line_separators_are_payload_not_jsonl_delimiters(self) -> None:
        for index, separator in enumerate(("\u0085", "\u2028", "\u2029")):
            with self.subTest(separator=ascii(separator)):
                trace = self.root / f"unicode-{index}.jsonl"
                seal_path = self.root / f"unicode-{index}.seal.json"
                payload = {"text": "before" + separator + "after"}
                first = TraceLedger(trace, seal_path).append("one", payload)
                prefix = trace.read_bytes()
                self.assertIn(separator.encode("utf-8"), prefix)
                self.assertEqual(prefix.count(b"\n"), 1)

                resumed = TraceLedger(trace, seal_path)
                second = resumed.append("two", payload)
                self.assertEqual(second.previous_hash, first.event_hash)
                original = trace.read_bytes()
                self.assertTrue(original.startswith(prefix))
                self.assertEqual(original.count(b"\n"), 2)
                self.assertEqual([json.loads(line)["payload"] for line in original.split(b"\n")[:-1]],
                    [payload, payload])
                sealed = resumed.seal()
                self.assertEqual(sealed.trace_sha256, hashlib.sha256(original).hexdigest())
                verified = verify_trace(trace, seal_path)
                self.assertTrue(verified.ok, verified.errors)
                self.assertEqual(verified.event_count, 2)
                self.assertEqual(trace.read_bytes(), original)

    def test_empty_and_physical_lf_boundaries_keep_existing_semantics(self) -> None:
        seed = TraceLedger(self.trace, self.seal).append("one", {})
        record = self.trace.read_bytes().removesuffix(b"\n")
        for index, (content, count) in enumerate(((b"", 0), (record, 1), (record + b"\n", 1))):
            with self.subTest(valid=index):
                trace = self.root / f"valid-{index}.jsonl"
                seal_path = self.root / f"valid-{index}.seal.json"
                trace.write_bytes(content)
                resumed = TraceLedger(trace, seal_path)
                self.assertEqual(resumed.event_count, count)
                self.assertEqual(resumed.head_hash, seed.event_hash if count else GENESIS_HASH)
                resumed.seal()
                self.assertTrue(verify_trace(trace, seal_path).ok)
                self.assertEqual(trace.read_bytes(), content)
        for index, content in enumerate((b"\n", b"\n" + record + b"\n",
                record + b"\n\n", record + b"\n\n" + record + b"\n")):
            with self.subTest(invalid=index):
                trace = self.root / f"invalid-{index}.jsonl"
                trace.write_bytes(content)
                with self.assertRaisesRegex(TraceError, "empty trace line"):
                    TraceLedger(trace)


if __name__ == "__main__":
    unittest.main()
