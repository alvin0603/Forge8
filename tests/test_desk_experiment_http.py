"""Local HTTP boundary checks only; experiment execution is always mocked."""
import http.client
import json
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from forge8 import desk as desk_module
import test_desk as fixtures


class ExperimentHTTPTests(fixtures.DeskFixture):
    request = fixtures.ReadingHTTPTests.request
    close_server = fixtures.ReadingHTTPTests.close_server

    def setUp(self):
        super().setUp()
        self.server, url = desk_module.make_server(self.desk)
        self.origin = url.split("/#", 1)[0]
        self.token = parse_qs(urlsplit(url).fragment)["token"][0]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.addCleanup(self.close_server)

    def test_run_route_requires_existing_auth_host_origin_and_default_desk_stays_disabled(self):
        with patch.object(self.desk, "start_experiment") as run:
            for fields, auth, expected in (({}, False, 401), ({"Host": "evil.invalid"}, True, 403),
                    ({"Origin": "https://evil.invalid"}, True, 403)):
                with self.subTest(fields=fields, auth=auth):
                    self.assertEqual(self.request("/api/experiment/run", method="POST", payload={},
                        headers=fields, auth=auth, headers_only=True)[0], expected)
            run.assert_not_called()
        payload = {"file": "0", "version": self.desk.project["version"], "entry": "entry"}
        status, _, body = self.request("/api/experiment/prepare", method="POST", payload=payload)
        self.assertEqual(status, 400)
        self.assertIn("disabled", json.loads(body)["error"])
        self.assertIsNone(self.desk.experiment_worker)

    def test_raw_input_and_result_remain_strings_through_json_transport(self):
        raw = ' {"args":[9007199254740993,"你好"],"kwargs":{}}\n'
        with patch.object(self.desk, "start_experiment", return_value={"id": "owned"}) as run:
            self.assertEqual(self.request("/api/experiment/run", method="POST", payload={"input_text": raw})[0], 202)
            self.assertEqual(run.call_args.args[0]["input_text"], raw)
        result = '{"return":9007199254740993}'
        with patch.object(self.desk, "experiment_status", return_value={"result_text": result}):
            status, headers, body = self.request("/api/experiment/current")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result_text"], result)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_larger_escaped_envelope_is_experiment_run_only_and_still_bounded(self):
        raw = json.dumps({"args": ["\\" * 7000], "kwargs": {}})
        payload = {"input_text": raw}
        self.assertLess(len(raw.encode()), 16_384)
        self.assertGreater(len(json.dumps(payload).encode()), 16_384)
        with patch.object(self.desk, "start_experiment", return_value={"id": "owned"}) as run, \
                patch.object(self.desk, "start") as model:
            self.assertEqual(self.request("/api/experiment/run", method="POST", payload=payload)[0], 202)
            self.assertEqual(self.request("/api/jobs", method="POST", payload=payload, headers_only=True)[0], 400)
            self.assertEqual(self.request("/api/experiment/run", method="POST",
                payload={"input_text": "x" * (36 * 1024)}, headers_only=True)[0], 400)
            run.assert_called_once()
            model.assert_not_called()

    def test_duplicate_consent_and_nonfinite_envelope_are_rejected_before_dispatch(self):
        with patch.object(self.desk, "start_experiment") as run:
            for raw in (b'{"allow_execution":false,"allow_execution":true}', b'{"file":NaN}'):
                with self.subTest(raw=raw):
                    connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
                    try:
                        connection.request("POST", "/api/experiment/run", raw,
                            {"Authorization": "Bearer " + self.token, "Origin": self.origin,
                             "Content-Type": "application/json"})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 400)
                        response.read()
                    finally:
                        connection.close()
            run.assert_not_called()
