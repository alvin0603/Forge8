"""Call navigation uses retained bytes, not host execution or model context."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from forge8 import desk as app
import test_desk as fixtures


TEXT = "raise RuntimeError('never execute source')\ndef chosen(value): return value\n"
CALLS = "from main import chosen\nx = chosen(1); y = chosen(2)\n"


class CallDeskTests(fixtures.DeskFixture):
    def setUp(self):
        super().setUp()
        (self.source / 'main.py').write_text(TEXT, encoding='utf-8')
        (self.source / 'calls.py').write_text(CALLS, encoding='utf-8')
        self.version = self.desk.refresh()['version']

    def search(self, query='chosen', version=None):
        return self.desk.definitions(query, version or self.version, mode='calls')

    def test_cross_file_calls_are_byte_bound_without_model_worker_or_state_changes(self):
        self.desk._trial_baseline = b'owned historical state'
        before = deepcopy((self.desk.project, self.desk.job, self.desk.history(),
            self.desk.contents, self.desk.outlines, self.desk.experiment,
            self.desk._trial_baseline, self.desk._trial_revision))
        with patch('subprocess.Popen', side_effect=AssertionError('no child')), \
                patch.object(app, '_run_locate_cli') as locate, \
                patch.object(app, '_run_explain_cli') as explain:
            result = self.search()
        locate.assert_not_called(); explain.assert_not_called()
        self.assertEqual((result['version'], result['mode'], result['query']),
                         (self.version, 'calls', 'chosen'))
        rows = result['matches']
        self.assertEqual([(r['path'], r['start_line'], r['start_column']) for r in rows],
                         [('calls.py', 2, 4), ('calls.py', 2, 19)])
        self.assertEqual({r['source_sha256'] for r in rows},
                         {hashlib.sha256((self.source / 'calls.py').read_bytes()).hexdigest()})
        self.assertFalse(result['semantics_verified'])
        self.assertEqual(before, (self.desk.project, self.desk.job, self.desk.history(),
            self.desk.contents, self.desk.outlines, self.desk.experiment,
            self.desk._trial_baseline, self.desk._trial_revision))
        self.assertIsNone(self.desk.worker)
        self.assertIsNone(self.desk.experiment_worker)

    def test_changed_live_source_is_not_read_until_explicit_refresh(self):
        expected = self.search()
        (self.source / 'calls.py').write_text('other()\n', encoding='utf-8')
        self.assertEqual(self.search(), expected)
        new = self.desk.refresh()['version']
        self.assertEqual(self.search(version=new)['matches'], [])
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.search()

    def test_changed_retained_bytes_fail_whole_and_release_scan_slot(self):
        path = Path(self.desk.browse_snapshot.snapshot_root) / 'calls.py'
        path.write_text(CALLS.replace('1', '9'), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            self.search()
        path.write_text(CALLS, encoding='utf-8')
        self.assertEqual(self.search()['total_matches'], 2)

    def test_after_scan_drift_is_detected_before_publication(self):
        original = app.call_occurrences
        def change(*args):
            result = original(*args)
            (Path(self.desk.browse_snapshot.snapshot_root) / 'calls.py').write_text(
                CALLS.replace('1', '9'), encoding='utf-8')
            return result
        with patch.object(app, 'call_occurrences', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'snapshot changed'):
                self.search()

    def test_closed_stale_and_bad_queries_read_no_target(self):
        with patch.object(app, '_retained_bytes', side_effect=AssertionError('must not read')):
            for query in ('', 'x y', 'obj.chosen', 'x()', 'class', 'x' * 129, 'x\n'):
                with self.subTest(query=query), self.assertRaises(ValueError):
                    self.search(query)
            with self.assertRaisesRegex(ValueError, 'stale'):
                self.search(version='wrong')
            self.desk.close()
            with self.assertRaisesRegex(ValueError, 'closed'):
                self.search()

    def test_concurrent_scan_refused_without_global_busy_or_queued_work(self):
        with self.desk.call_scan_lock:
            self.assertFalse(self.desk._busy())
            with self.assertRaisesRegex(ValueError, 'another call search'):
                self.search()
            self.assertEqual(self.desk.source_view('0', self.version)['version'], self.version)
        self.assertEqual(self.search()['total_matches'], 2)

    def test_parse_runs_without_desk_lock_and_same_hash_refresh_invalidates_it(self):
        entered, proceed = threading.Event(), threading.Event()
        original = app.call_occurrences
        errors = []
        def paused(*args):
            entered.set()
            if not proceed.wait(3):
                raise AssertionError('scan held up refresh')
            return original(*args)
        def run():
            try:
                self.search()
            except Exception as exc:
                errors.append(exc)
        with patch.object(app, 'call_occurrences', side_effect=paused):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.desk.lock.acquire(timeout=1))
                try:
                    self.assertEqual(self.desk.refresh()['version'], self.version)
                finally:
                    self.desk.lock.release()
            finally:
                proceed.set()
                worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), 'snapshot refreshed')
        self.assertEqual(self.search()['total_matches'], 2)

    def test_closure_after_scan_invalidates_publication(self):
        original = app.call_occurrences
        def close(*args):
            result = original(*args)
            self.desk.close()
            return result
        with patch.object(app, 'call_occurrences', side_effect=close):
            with self.assertRaisesRegex(ValueError, 'closed'):
                self.search()

    def test_oversized_whole_response_is_not_published(self):
        partial = {'padding': 'é' * 32750}
        self.assertLess(len(json.dumps(partial, ensure_ascii=False).encode()), 65536)
        with patch.object(app, 'call_occurrences', return_value=partial):
            with self.assertRaisesRegex(ValueError, '64 KiB'):
                self.search()

    def test_time_budget_is_explicit_incomplete_coverage_not_zero_calls(self):
        with patch.object(app.time, 'monotonic', side_effect=[10., 14.]):
            result = self.search()
        self.assertEqual(result['stop_reason'], 'time_limit')
        self.assertEqual(result['uninspected_files'], len(self.desk.project['files']))
        self.assertEqual(result['inspected_files'], 0)
        self.assertEqual(result['matches'], [])


class CallHTTPTests(fixtures.DeskFixture):
    request = fixtures.ReadingHTTPTests.request
    close_server = fixtures.ReadingHTTPTests.close_server

    def setUp(self):
        super().setUp()
        (self.source / 'main.py').write_text(TEXT + CALLS, encoding='utf-8')
        self.version = self.desk.refresh()['version']
        self.server, url = app.make_server(self.desk)
        self.origin = url.split('/#', 1)[0]
        self.token = parse_qs(urlsplit(url).fragment)['token'][0]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close_server)

    def route(self):
        return '/api/definitions?' + urlencode({'q': 'chosen', 'version': self.version, 'mode': 'calls'})

    def test_authenticated_get_has_no_store_and_no_execution(self):
        route = self.route()
        for fields, auth, expected in (({}, False, 401), ({'Host': 'evil.invalid'}, True, 403),
                                      ({'Origin': 'https://evil.invalid'}, True, 403)):
            with self.subTest(fields=fields):
                self.assertEqual(self.request(route, headers=fields, auth=auth)[0], expected)
        status, headers, body = self.request(route)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(json.loads(body), self.desk.definitions('chosen', self.version, mode='calls'))
        self.assertEqual(self.desk.job['status'], 'idle')
        self.assertIsNone(self.desk.worker)

    def test_duplicate_mode_unknown_query_and_scan_contention_refused(self):
        self.assertEqual(self.request(self.route() + '&mode=exact')[0], 400)
        self.assertEqual(self.request(self.route() + '&q=other')[0], 400)
        with self.desk.call_scan_lock:
            self.assertEqual(self.request(self.route())[0], 400)
