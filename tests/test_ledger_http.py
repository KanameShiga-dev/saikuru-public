import http.cookiejar
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

from server import Handler


class LedgerHttpTests(unittest.TestCase):
    def test_warning_paths_are_resolved_once_and_keep_project_associations(self):
        projects = [
            {'id': 'search', 'path': r'C:\Work\Search'},
            {'id': 'other', 'path': r'C:\Work\Other'},
            {'id': 'empty', 'path': r'C:\Work\Empty'},
        ]
        jobs = [
            {'project': r'c:\work\search', 'title': 'First',
             'warning_issues': [{'title': 'First warning'}]},
            {'project': r'C:\Work\Other', 'title': 'Other',
             'warning_issues': [{'title': 'Other warning'}]},
            {'project': r'C:\Work\Search', 'title': 'Second',
             'warning_issues': [{'title': 'Second warning'}]},
        ]
        ledger = SimpleNamespace(
            snapshot=Mock(return_value={'projects': projects}),
            issues=Mock(return_value=[]),
        )
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        url = f'http://127.0.0.1:{server.server_port}'
        server.app = SimpleNamespace(
            port=server.server_port, origin=url, cookie='ledger-test-cookie',
            ledger=ledger, config={'approved_roots': []},
            store=SimpleNamespace(all=Mock(return_value=jobs)),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            with client.open(url + '/ledger') as response:
                response.read()
            with patch('server.canonical', side_effect=lambda path: path.casefold()) as resolve:
                with client.open(url + '/api/ledger', timeout=5) as response:
                    result = json.load(response)
                self.assertEqual(resolve.call_count, len(projects) + 3)
            self.assertEqual([i['title'] for i in result['projects'][0]['warning_issues']],
                             ['First warning', 'Second warning'])
            self.assertEqual([i['title'] for i in result['projects'][1]['warning_issues']],
                             ['Other warning'])
            self.assertEqual(result['projects'][2]['warning_issues'], [])
            self.assertEqual(len(result['warning_issues']), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
