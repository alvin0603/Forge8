"""Cached keyword navigation integration; target source is never executed."""
from copy import deepcopy
import json
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from forge8 import desk as app
import test_desk as fixtures


TEXT = ("raise RuntimeError('source must never execute')\n"
        "def mergeHeaders(headers):\n    return headers\n"
        "def prepare(request):\n    merge = request\n    return merge.headers\n")


class KeywordDeskTests(fixtures.DeskFixture):
    def setUp(self):
        super().setUp()
        (self.source / 'main.py').write_text(TEXT, encoding='utf-8')
        self.version = self.desk.refresh()['version']

    def search(self, query='merge headers', version=None):
        return self.desk.definitions(query, version or self.version, mode='keywords')

    def test_name_and_body_reasons_without_model_source_execution_or_state_changes(self):
        before = deepcopy((self.desk.project, self.desk.job, self.desk.history(), self.desk.contents, self.desk.outlines))
        with patch('subprocess.Popen', side_effect=AssertionError('no child')), \
                patch.object(app, '_run_locate_cli') as locate, \
                patch.object(app, '_run_explain_cli') as explain:
            result = self.search()
        locate.assert_not_called(); explain.assert_not_called()
        self.assertEqual((result['version'], result['mode'], result['terms']),
                         (self.version, 'keywords', ['merge', 'headers']))
        self.assertEqual([row['name'] for row in result['matches']], ['mergeHeaders', 'prepare'])
        self.assertEqual(result['matches'][0]['matched_terms'], [
            {'term': 'merge', 'field': 'name', 'line': None},
            {'term': 'headers', 'field': 'name', 'line': None}])
        self.assertEqual(result['matches'][1]['matched_terms'], [
            {'term': 'merge', 'field': 'source', 'line': 5},
            {'term': 'headers', 'field': 'source', 'line': 6}])
        self.assertEqual((result['total_matches'], result['truncated'], result['unavailable_files']), (2, False, 0))
        self.assertEqual(before, (self.desk.project, self.desk.job, self.desk.history(), self.desk.contents, self.desk.outlines))
        self.assertIsNone(self.desk.worker)
        self.assertEqual(self.desk.model_status(), {'enabled': False})

    def test_legacy_literal_and_exact_shapes_remain_unchanged(self):
        self.assertEqual(self.desk.search('merge headers'), {'matches': [], 'truncated': False})
        exact = self.desk.definitions('mergeHeaders', self.version)
        self.assertEqual(set(exact), {'version', 'matches', 'truncated', 'unavailable_files'})
        self.assertEqual([row['name'] for row in exact['matches']], ['mergeHeaders'])
        self.assertNotIn('matched_terms', exact['matches'][0])
        self.assertEqual(exact, self.desk.definitions('mergeHeaders', self.version, mode='exact'))
        with self.assertRaises(ValueError):
            self.desk.definitions('merge headers', self.version)

    def test_snapshot_cache_not_live_file_and_old_version_refused_after_refresh(self):
        expected = self.search()
        (self.source / 'main.py').write_text('def changed(): pass\n', encoding='utf-8')
        self.assertEqual(self.search(), expected)
        new = self.desk.refresh()['version']
        self.assertEqual(self.search(version=new)['matches'], [])
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.search()

    def test_closed_stale_or_invalid_mode_refused_before_keyword_matching(self):
        with patch.object(app, 'keyword_definitions') as matching:
            for mode in ('unknown', True, None, [], {}):
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    self.desk.definitions('merge headers', self.version, mode=mode)
            with self.assertRaises(ValueError):
                self.search(version='stale')
            self.desk.close()
            with self.assertRaisesRegex(ValueError, 'closing'):
                self.search()
            matching.assert_not_called()

    def test_final_response_size_includes_native_utf8_version_and_mode(self):
        partial = {'padding': 'é' * 32750}
        self.assertLess(len(json.dumps(partial, ensure_ascii=False).encode('utf-8')), 65536)
        with patch.object(app, 'keyword_definitions', return_value=partial):
            with self.assertRaisesRegex(ValueError, '64 KiB'):
                self.search()


class KeywordHTTPTests(fixtures.DeskFixture):
    request = fixtures.ReadingHTTPTests.request
    close_server = fixtures.ReadingHTTPTests.close_server

    def setUp(self):
        super().setUp()
        (self.source / 'main.py').write_text(TEXT, encoding='utf-8')
        self.version = self.desk.refresh()['version']
        self.server, url = app.make_server(self.desk)
        self.origin = url.split('/#', 1)[0]
        self.token = parse_qs(urlsplit(url).fragment)['token'][0]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close_server)

    def test_keyword_get_auth_host_no_store_and_no_model(self):
        route = '/api/definitions?' + urlencode({'q': 'merge headers', 'version': self.version, 'mode': 'keywords'})
        for fields, auth, expected in (({}, False, 401), ({'Host': 'evil.invalid'}, True, 403),
                                      ({'Origin': 'https://evil.invalid'}, True, 403)):
            with self.subTest(fields=fields):
                self.assertEqual(self.request(route, headers=fields, auth=auth)[0], expected)
        status, headers, body = self.request(route)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(json.loads(body), self.desk.definitions('merge headers', self.version, mode='keywords'))
        self.assertEqual(self.desk.job['status'], 'idle')
        self.assertIsNone(self.desk.worker)

    def test_unknown_duplicate_modes_and_stale_keyword_get_refused(self):
        route = '/api/definitions?' + urlencode({'q': 'merge headers', 'version': self.version})
        self.assertEqual(self.request(route)[0], 400)  # Existing exact mode still rejects spaces.
        for suffix in ('&mode=unknown', '&mode=keywords&mode=exact'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.request(route + suffix)[0], 400)
        stale = '/api/definitions?' + urlencode({'q': 'merge headers', 'version': 'stale', 'mode': 'keywords'})
        self.assertEqual(self.request(stale)[0], 400)
