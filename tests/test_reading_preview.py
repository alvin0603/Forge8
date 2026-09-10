"""Pure text-adapter tests: no source execution, inference, or host processes."""

from __future__ import annotations

import json
import unittest
import unicodedata

from forge8.reading_preview import StructuredAnswerPreview


PREFIX = '{"kind":"answer","text":"'
SUFFIX = '","citations":[{"evidence_id":"WRAPPER_SENTINEL","start_line":1,"end_line":2}]}'


class ReadingPreviewTests(unittest.TestCase):
    def collect(self, chunks):
        output = []
        preview = StructuredAnswerPreview(output.append)
        for chunk in chunks:
            preview(chunk)
            for part in output:
                part.encode("utf-8")
                self.assertFalse(any(unicodedata.category(char) in {"Cs", "Cf", "Zl", "Zp"}
                    or (unicodedata.category(char) == "Cc" and char not in "\r\n\t") for char in part))
        return "".join(output)

    def test_every_split_and_character_chunks_preserve_exact_ordinary_text(self):
        texts = ["", "Markdown **bold** `code` [E1:L1-L2]", "繁體中文🙂𝄞é e\u0301",
            'A "quoted" name and C:\\path\\file / slash', "line1\nline2\r\n\ttab", "trailing backslash\\"]
        for text in texts:
            for ascii_only in (False, True):
                raw = PREFIX + json.dumps(text, ensure_ascii=ascii_only)[1:-1] + SUFFIX
                for split in range(len(raw) + 1):
                    with self.subTest(text=text, ascii_only=ascii_only, split=split):
                        self.assertEqual(self.collect([raw[:split], raw[split:]]), text)
                self.assertEqual(self.collect(raw), text)

    def test_only_json_whitespace_and_literal_canonical_prefix_are_recognized(self):
        raw = ' \r\n\t{ "kind" \t: "answer" \n,\r "text" : "visible"' + SUFFIX[1:]
        self.assertEqual(self.collect(raw), "visible")
        invalid = [
            '{"text":"LEAK","kind":"answer"}', '{"kind":"abstain","text":"LEAK"}',
            '{"kind":"Answer","text":"LEAK"}', '{"kind":"answer","extra":1,"text":"LEAK"}',
            '{"kind":"answer","kind":"answer","text":"LEAK"}',
            '{"kind":"answer","text":null}', '{"kind":"answer","text":{"text":"LEAK"}}',
            '{"kind":"answer","text":["LEAK"]}', '{"kind":"answer","text":true}',
            '{"kind":"answer","text":123}', '[{"kind":"answer","text":"LEAK"}]',
            '```json\n' + PREFIX + "LEAK" + SUFFIX, 'ordinary preamble ' + PREFIX + "LEAK" + SUFFIX,
            '{"k\\u0069nd":"answer","text":"LEAK"}', '{"kind":"\\u0061nswer","text":"LEAK"}',
            '{"kind":"answer","te\\u0078t":"LEAK"}', '\ufeff' + PREFIX + "LEAK" + SUFFIX,
            '{\u00a0"kind":"answer","text":"LEAK"}', '{"kind":/* comment */"answer","text":"LEAK"}',
        ]
        for raw in invalid:
            for chunks in ([raw], list(raw)):
                with self.subTest(raw=raw, chunks=len(chunks)):
                    self.assertEqual(self.collect(chunks), "")

    def test_incomplete_prefix_never_emits_and_is_not_searched_for_later(self):
        for end in range(len(PREFIX) + 1):
            self.assertEqual(self.collect([PREFIX[:end]]), "")
        self.assertEqual(self.collect(["INVALID", PREFIX + "LEAK" + SUFFIX]), "")
        self.assertEqual(self.collect(['{"kind":"abstain",', PREFIX + "LEAK" + SUFFIX]), "")

    def test_pending_escape_and_surrogate_are_atomic_and_never_duplicated(self):
        output = []
        preview = StructuredAnswerPreview(output.append)
        preview(PREFIX + "first\\")
        self.assertEqual(output, ["first"])
        preview("uD83D"); preview("\\"); preview("uDE"); preview("")
        self.assertEqual(output, ["first"])
        preview("42")
        self.assertEqual(output, ["first", "🙂"])
        preview(r'\n\t\r\/\\\"last' + SUFFIX)
        self.assertEqual("".join(output), 'first🙂\n\t\r/\\"last')
        before = output[:]
        preview(PREFIX + "ANOTHER_ANSWER" + SUFFIX)
        self.assertEqual(output, before)
        for escaped, expected in ((r"\uD800\uDC00", "\U00010000"), (r"\uDBFF\uDFFF", "\U0010ffff")):
            self.assertEqual(self.collect(PREFIX + escaped + SUFFIX), expected)

    def test_malformed_escapes_stop_permanently_without_exposing_suffix(self):
        invalid = [r"\q", r"\x41", r"\u12g4", r"\uZZZZ", r"\udc00", r"\udfff",
            r"\ud800x", r"\ud800\n", r"\ud800\u0041", r"\ud800\ud800", r"\udbff\uZZZZ"]
        for value in invalid:
            for chunks in ([PREFIX + "safe" + value + "DO_NOT_SHOW" + SUFFIX], list(PREFIX + "safe" + value + "DO_NOT_SHOW" + SUFFIX)):
                with self.subTest(value=value, chunks=len(chunks)):
                    self.assertEqual(self.collect([*chunks, PREFIX + "DO_NOT_RESTART" + SUFFIX]), "safe")
        for pending in ("\\", "\\u", "\\u123", "\\ud800", "\\ud800\\", "\\ud800\\uDC"):
            self.assertEqual(self.collect([PREFIX + "safe" + pending]), "safe")

    def test_unsafe_raw_and_escaped_unicode_follow_multiline_text_policy(self):
        unsafe = ["\x00", "\x08", "\x0b", "\x0c", "\x1b", "\x7f", "\x85",
            "\u200b", "\u200d", "\u202a", "\u202e", "\u2066", "\u2069", "\ufeff", "\u2028", "\u2029", "\ud800", "\udfff"]
        for value in unsafe:
            for encoded in (value, json.dumps(value, ensure_ascii=True)[1:-1]):
                with self.subTest(value=repr(value), encoded=repr(encoded)):
                    self.assertEqual(self.collect([PREFIX + "safe", encoded + "DO_NOT_SHOW" + SUFFIX]), "safe")
        for raw_control in "\r\n\t":
            self.assertEqual(self.collect([PREFIX + "safe" + raw_control + "DO_NOT_SHOW" + SUFFIX]), "safe")
        self.assertEqual(self.collect([PREFIX + r"\r\n\t" + SUFFIX]), "\r\n\t")

    def test_real_quote_closes_text_and_wrapper_never_becomes_preview(self):
        for suffix in (SUFFIX, '","text":"DUPLICATE_LEAK"}', '" BROKEN_JSON_LEAK', '"' + PREFIX + "NESTED_LEAK" + SUFFIX):
            self.assertEqual(self.collect([PREFIX + "safe", suffix]), "safe")
        self.assertEqual(self.collect([PREFIX + r'quoted\"still text' + SUFFIX]), 'quoted"still text')
        self.assertEqual(self.collect([PREFIX + r'backslash\\' + SUFFIX]), "backslash\\")

    def test_raw_bound_is_inclusive_and_oversized_delta_never_accumulates(self):
        text = "a" * (32768 - len(PREFIX) - 1)
        self.assertEqual(self.collect([PREFIX + text + '"']), text)
        output = []
        preview = StructuredAnswerPreview(output.append)
        preview(PREFIX + "safe")
        preview("x" * 32768)
        preview("SHOULD_NOT_RESTART" + SUFFIX)
        self.assertEqual(output, ["safe"])
        self.assertLessEqual(len(preview._buffer), 32768)
        self.assertEqual(self.collect([" " * 32768, PREFIX + "LEAK" + SUFFIX]), "")
        self.assertEqual(self.collect([PREFIX + "x" * 32768 + SUFFIX]), "")

    def test_invalid_delta_stops_and_callback_failures_are_not_hidden(self):
        for invalid in (None, 1, True, b"bytes", ["text"], {"text": "text"}):
            self.assertEqual(self.collect([PREFIX + "safe", invalid, "DO_NOT_SHOW" + SUFFIX]), "safe")
        with self.assertRaises(TypeError):
            StructuredAnswerPreview(None)

        def failed(_text):
            raise RuntimeError("sink failed")

        with self.assertRaisesRegex(RuntimeError, "sink failed"):
            StructuredAnswerPreview(failed)(PREFIX + "safe")


if __name__ == "__main__":
    unittest.main()
