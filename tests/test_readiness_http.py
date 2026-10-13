import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from eval.development_readiness.serve_synthetic import make_fixture


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_fixture(self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = self.server.app.origin
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server.app.store.db.close()
        self.temp.cleanup()

    def request(self, path, body=None, ui=True):
        headers = {'Content-Type': 'application/json'}
        if ui: headers['X-Agent-Team-UI'] = '1'
        req = urllib.request.Request(self.origin+path, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        return self.client.open(req, timeout=5)

    def test_auth_paging_details_effort_and_csrf_boundary(self):
        for path in ('/api/jobs/archive', '/api/jobs/evaluation?id=fixture-0', '/api/tasks/detail?id=t-0'):
            with self.assertRaises(urllib.error.HTTPError) as error: self.request(path)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        with self.request('/') as response: self.assertEqual(response.status, 200)
        with self.request('/api/jobs/archive') as response: first = json.load(response)
        self.assertEqual((len(first['jobs']), first['next_offset'], first['total']), (50, 50, 53))
        with self.request('/api/jobs/archive?offset=50') as response: last = json.load(response)
        self.assertEqual((len(last['jobs']), last['next_offset']), (3, None))
        with self.request('/api/jobs/archive?id=fixture-0') as response:
            self.assertEqual(json.load(response)['tasks'][0]['result']['status'], 'done')
        body = {'job_id': 'fixture-0', 'revision': 0, 'minutes': 9, 'confirmed': True, 'observer': 'evaluation_runner'}
        with self.assertRaises(urllib.error.HTTPError) as error: self.request('/api/jobs/evaluation', body, ui=False)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with self.request('/api/jobs/evaluation', body) as response: self.assertEqual(json.load(response)['revision'], 1)
        with self.request('/api/jobs/evaluation?id=fixture-0') as response:
            value = json.load(response)
            self.assertEqual(value['record']['minutes'], 9)
            self.assertEqual(value['status'], 'accepted')
        with self.request('/api/state') as response: self.assertEqual(json.load(response)['history']['hidden_jobs'], 53)
