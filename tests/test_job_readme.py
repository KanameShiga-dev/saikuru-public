"""The receipt screen shows how to try the result by reading the job's README.md (2026-11-08)."""
import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from server import Handler


class JobReadmeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        job = {'id': 'fictional', 'project': str(self.root)}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.app = SimpleNamespace(
            store=SimpleNamespace(get=lambda _id, _kind: job),
            config={}, port=self.server.server_port, cookie='fictional-cookie',
            origin=f'http://127.0.0.1:{self.server.server_port}')
        self.url = self.server.app.origin + '/api/job/readme?job_id=fictional'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def signed_in(self):
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with client.open(self.server.app.origin + '/') as response:
            response.read()
        return client

    def test_a_person_without_the_screen_session_is_refused(self):
        (self.root / 'README.md').write_text('架空の使い方', encoding='utf-8')
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.url)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_the_readme_is_returned_and_kept_to_a_screenful(self):
        (self.root / 'README.md').write_text('使' * 30000, encoding='utf-8')
        with self.signed_in().open(self.url) as response:
            self.assertEqual(json.load(response)['content'], '使' * 20000)


if __name__ == '__main__':
    unittest.main()
