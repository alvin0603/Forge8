from __future__ import annotations

import copy
import http.client
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from forge8 import desk as desk_module
from test_desk import DeskFixture


HEADER = "Traceback (most recent call last):\n"


class TracebackTests(DeskFixture):
    def payload(self, frames, suffix="ValueError: example\n"):
        return {"text": HEADER + frames + suffix, "version": self.desk.project["version"]}

    def test_relative_and_native_absolute_frames_preserve_order_and_duplicates(self):
        absolute = str(self.source / "main.py")
        payload = self.payload('  File "main.py", line 2, in work\n'
            f'  File "{absolute}", line 1, in <module>\n'
            '  File "main.py", line 2\n')
        before = copy.deepcopy(self.desk.job)
        result = self.desk.tracebacks(payload)
        self.assertEqual(result, {"version": payload["version"], "scope": "unverified_user_traceback",
            "matched": 3, "frames": [
                {"reported_path": path, "line": line, "function": name,
                 "file": "0", "path": "main.py", "reason": None}
                for path, line, name in (("main.py", 2, "work"), (absolute, 1, "<module>"), ("main.py", 2, ""))]})
        self.assertEqual(self.desk.job, before)
        self.assertIsNone(self.desk.worker)

    def test_native_paths_use_exact_admitted_suffix_not_basename_or_other_os_mapping(self):
        directory = self.source / "pkg"
        directory.mkdir()
        (directory / "main.py").write_text("nested = 1\n", encoding="utf-8")
        self.desk.refresh()
        nested = str(Path("pkg") / "main.py")
        paths = [(nested, None), (str(directory / "main.py"), None),
            (str(self.root / "other" / "main.py"), "outside_project"),
            ("absent/main.py", "unknown_source"), ("Main.py", "unknown_source"),
            ("../main.py", "unsupported_path"), ("pkg/../main.py", "unsupported_path"),
            (str(self.source / ".." / "project" / "main.py"), "unsupported_path"),
            ("pkg//main.py", "unsupported_path"),
            ("main.py/", "unsupported_path"), ("file:///main.py", "unsupported_path"),
            ("C:main.py", "unsupported_path"), (r"\\server\share\main.py", "unsupported_path"),
            (r"\\?\C:\main.py", "unsupported_path"), ("NUL", "unsupported_path"),
            ("<stdin>", "unsupported_path"), ("<frozen runpy>", "unsupported_path")]
        paths += [("/usr/lib/main.py", "unsupported_path")] if os.name == "nt" else [
            (r"C:\work\main.py", "unsupported_path"), (r"pkg\main.py", "unsupported_path")]
        for path, expected in paths:
            with self.subTest(path=path):
                result = self.desk.tracebacks(self.payload(f'  File "{path}", line 1\n'))
                frame = result["frames"][0]
                self.assertEqual(frame["reason"], expected)
                self.assertEqual(frame["reported_path"], path)
                self.assertEqual(result["matched"], int(expected is None))
                if expected is None:
                    self.assertEqual(frame["path"], "pkg/main.py")
                else:
                    self.assertIsNone(frame["file"])
                    self.assertIsNone(frame["path"])
        if os.name == "nt":
            absolute = str(self.source).swapcase() + "\\main.py"
            self.assertEqual(self.desk.tracebacks(self.payload(f'  File "{absolute}", line 1\n'))["matched"], 1)

    def test_dot_components_are_lexical_and_compressed_repetitions_are_not_dropped(self):
        for path in ("./main.py", f".{os.sep}main.py", f"{self.source}{os.sep}.{os.sep}main.py"):
            with self.subTest(path=path):
                frame = self.desk.tracebacks(self.payload(f'  File "{path}", line 1\n'))["frames"][0]
                self.assertEqual((frame["path"], frame["reason"]), ("main.py", None))
                self.assertEqual(frame["reported_path"], path)
        frame = '  File "main.py", line 1\n'
        for repeat in ("1 more time", "998 more times"):
            with self.subTest(repeat=repeat), self.assertRaisesRegex(ValueError, "compressed"):
                self.desk.tracebacks(self.payload(frame + f"  [Previous line repeated {repeat}]\n"))
        self.assertEqual(self.desk.tracebacks(self.payload(
            frame + "    [Previous line repeated 998 more times]\n"))["matched"], 1)

    def test_cached_lookup_never_touches_reported_or_live_paths_or_starts_a_worker(self):
        payload = self.payload('  File "main.py", line 2\n')
        expected = self.desk.tracebacks(payload)
        (self.source / "main.py").write_text("live = 9\n", encoding="utf-8")
        self.desk.job.update(question="unchanged\nquestion", focus=[{"file": "0", "start": 1, "end": 2}])
        before = copy.deepcopy(self.desk.job)
        with patch.object(Path, "resolve", side_effect=AssertionError("resolve")), \
                patch.object(Path, "stat", side_effect=AssertionError("stat")), \
                patch.object(Path, "lstat", side_effect=AssertionError("lstat")), \
                patch.object(Path, "open", side_effect=AssertionError("open")), \
                patch.object(threading.Thread, "start", side_effect=AssertionError("worker")), \
                patch.object(desk_module, "_run_explain_cli") as model:
            self.assertEqual(self.desk.tracebacks(payload), expected)
        model.assert_not_called()
        self.assertEqual(self.desk.job, before)
        self.assertIsNone(self.desk.worker)

    def test_line_range_and_nonphysical_lines_are_not_navigable(self):
        for number in (0, 3, 999999):
            with self.subTest(number=number):
                result = self.desk.tracebacks(self.payload(f'  File "main.py", line {number}\n'))
                self.assertEqual(result["frames"][0]["reason"], "line_out_of_range")
                self.assertEqual(result["matched"], 0)
        for separator in ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                (self.source / "main.py").write_text("# first" + separator + "# second\n", encoding="utf-8")
                self.desk.refresh()
                result = self.desk.tracebacks(self.payload('  File "main.py", line 1\n'))
                excluded = separator in ("\v", "\x1c", "\x1d", "\x1e")
                self.assertEqual("main.py" in self.desk.project["excluded"], excluded)
                self.assertEqual(result["frames"][0]["reason"], "unknown_source" if excluded else "line_separators")
                self.assertIsNone(result["frames"][0]["file"])
        (self.source / "main.py").write_bytes(b"first = 1\r\nsecond = 2\r\n")
        self.desk.refresh()
        self.assertEqual(self.desk.tracebacks(self.payload('  File "main.py", line 2\n'))["matched"], 1)

    def test_classic_chains_ignore_source_lines_and_preserve_repeated_frames(self):
        frames = ('  File "main.py", line 1, in <module>\n'
            '    File "not-a-frame.py", line 999, in source_literal\n'
            'ValueError: first\n\nDuring handling of the above exception, another exception occurred:\n\n'
            + HEADER + '  File "main.py", line 1, in <module>\n')
        payload = self.payload(frames)
        payload["text"] = payload["text"].replace("\n", "\r\n")
        result = self.desk.tracebacks(payload)
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["frames"][0], result["frames"][1])

    def test_exact_payload_header_frames_and_limits_fail_without_partial_result(self):
        good = self.payload('  File "main.py", line 1\n')
        invalid = [None, [], {}, {**good, "extra": True}, {"text": good["text"]},
            {**good, "version": "old"}, {**good, "version": 1}]
        bad_text = ["", " \n\t", HEADER, good["text"].removeprefix(HEADER),
            HEADER + '    File "main.py", line 1\n',
            good["text"] + '  File "main.py", line x\n',
            good["text"] + '  File "main.py", line 1, in \n',
            good["text"] + "  File 'main.py', line 1\n",
            good["text"] + ' | File "main.py", line 1\n',
            good["text"] + '    |   File "main.py", line 1\n',
            good["text"] + '\tFile "main.py", line 1\n',
            good["text"] + f'  File "main.py", line {2**53}\n',
            HEADER + '  File "main.py", line 1\n' * 33]
        bad_text += [good["text"] + char for char in ("\0", "\x1b", "\x85", "\u2028", "\u2029", "\u202e", "\ud800")]
        invalid += [{**good, "text": text} for text in bad_text]
        maximum = good["text"] + "x" * (2000 - len(good["text"]))
        self.assertEqual(self.desk.tracebacks({**good, "text": maximum})["matched"], 1)
        invalid.append({**good, "text": maximum + "x"})
        for payload in invalid:
            with self.subTest(payload=repr(payload)[:120]), self.assertRaises(ValueError):
                self.desk.tracebacks(payload)
        self.assertEqual(self.desk.tracebacks(self.payload('  File "main.py", line 1\n' * 32))["matched"], 32)
        self.desk.close()
        with self.assertRaisesRegex(ValueError, "closing"):
            self.desk.tracebacks(good)


class TracebackHTTPTests(DeskFixture):
    def test_traceback_post_keeps_auth_and_version_boundaries_without_a_job(self):
        server, url = desk_module.make_server(self.desk)
        token = parse_qs(urlsplit(url).fragment)["token"][0]
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        payload = {"text": HEADER + '  File "main.py", line 2\nValueError: example',
            "version": self.desk.project["version"]}

        def request(*, method="POST", auth=True, origin=True, body=payload, headers_only=False):
            headers = {"Content-Type": "application/json"}
            if auth: headers["Authorization"] = "Bearer " + token
            if origin: headers["Origin"] = f"http://127.0.0.1:{server.server_port}"
            raw = json.dumps(body).encode() if method == "POST" else None
            if headers_only:
                headers["Content-Length"] = str(len(raw))
                raw = None
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request(method, "/api/traceback", raw, headers)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), json.loads(response.read())
            finally:
                connection.close()

        try:
            with patch.object(desk_module, "_run_explain_cli") as model:
                self.assertEqual(request(auth=False, headers_only=True)[0], 401)
                self.assertEqual(request(origin=False, headers_only=True)[0], 403)
                self.assertEqual(request(method="GET")[0], 404)
                self.assertEqual(request(body={**payload, "version": "stale"})[0], 400)
                self.assertEqual(request(body={**payload, "focus": []})[0], 400)
                status, headers, result = request()
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(result, self.desk.tracebacks(payload))
                self.assertEqual(self.desk.status()["status"], "idle")
                self.assertIsNone(self.desk.worker)
                model.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.assertFalse(thread.is_alive())
