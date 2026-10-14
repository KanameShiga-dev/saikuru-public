"""SQAH Phase 3 over real HTTP routes with a disposable synthetic store: no AI, provider or normal DB."""
import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from eval.development_readiness.serve_synthetic import make_fixture


class QualityHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_fixture(self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = self.server.app.origin
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.store = self.server.app.store

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.db.close()
        self.temp.cleanup()

    def get(self, path):
        return self.client.open(urllib.request.Request(self.origin + path, headers={'X-Agent-Team-UI': '1'}), timeout=5)

    def counts(self):
        with self.store.lock:
            return (self.store.db.execute('SELECT COUNT(*) FROM objects').fetchone()[0],
                    self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0])

    def test_requires_the_session_cookie(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get('/api/quality/job?id=fixture-0')
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_returns_the_view_and_writes_nothing(self):
        with self.get('/') as response:
            self.assertEqual(response.status, 200)
        before = self.counts()
        with self.get('/api/quality/job?id=fixture-0') as response:
            view = json.load(response)
        self.assertEqual(self.counts(), before)
        self.assertEqual(view['job_id'], 'fixture-0')
        self.assertTrue(view['decides_nothing'])
        # The fixture job has no validation command: unknown, never a pass.
        self.assertEqual(view['overall'], 'inconclusive')
        self.assertEqual(view['facts'][0]['state'], 'inconclusive')
        self.assertFalse(view['findings']['counted_as_evidence'])
        self.assertEqual([r['status'] for r in view['findings']['reviews']], ['done'])

    def test_review_text_never_reaches_the_response(self):
        with self.get('/') as response:
            response.read()
        with self.get('/api/quality/job?id=fixture-0') as response:
            body = response.read().decode('utf-8')
        self.assertNotIn('架空の結果', body)

    def test_unknown_job_is_an_error_not_an_empty_pass(self):
        with self.get('/') as response:
            response.read()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get('/api/quality/job?id=does-not-exist')
        self.assertGreaterEqual(error.exception.code, 400)
        error.exception.close()

    def test_the_route_accepts_no_writes(self):
        with self.get('/') as response:
            response.read()
        before = self.counts()
        request = urllib.request.Request(self.origin + '/api/quality/job', data=b'{"id":"fixture-0"}',
                                         headers={'Content-Type': 'application/json', 'X-Agent-Team-UI': '1'})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.client.open(request, timeout=5)
        self.assertGreaterEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(self.counts(), before)


if __name__ == '__main__':
    unittest.main()
