"""Independent fixed acceptance tests. Kept outside the editable sample projects."""
import csv
import importlib
import io
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(PROJECT))
Store = importlib.import_module('app.store').TaskStore
make_server = importlib.import_module('app.server').make_server


class StoreAcceptance(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.ids = [self.store.create('alpha', 'first', 'todo'),
                    self.store.create('beta', 'alpha note', 'doing'),
                    self.store.create('100%_done', 'last', 'done')]
    def tearDown(self):
        self.store.db.close()
    def test_existing_create_and_all(self):
        self.assertEqual(len(self.store.all()), 3)
    def test_list(self):
        self.assertEqual(self.store.list()['total'], 3)
    def test_search_title_and_description(self):
        self.assertEqual(self.store.list(query='alpha')['total'], 2)
    def test_status_filter(self):
        self.assertEqual(self.store.list(status='doing')['items'][0]['title'], 'beta')
    def test_combined_filter(self):
        self.assertEqual(self.store.list(query='alpha', status='doing')['total'], 1)
    def test_pagination(self):
        row = self.store.list(page=2, page_size=1)
        self.assertEqual((row['total'], row['page'], row['page_size']), (3, 2, 1))
        self.assertEqual(row['items'][0]['id'], self.ids[1])
    def test_page_after_end(self):
        self.assertEqual(self.store.list(page=20)['items'], [])
    def test_literal_wildcards(self):
        self.assertEqual(self.store.list(query='%_')['total'], 1)
    def test_sql_input_is_literal(self):
        self.assertEqual(self.store.list(query="' OR 1=1 --")['total'], 0)
    def test_invalid_pagination(self):
        for field, value in [('page', 0), ('page', True), ('page', '1'),
                             ('page_size', 0), ('page_size', 101), ('page_size', 1.5)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.store.list(**{field: value})
    def test_invalid_status(self):
        with self.assertRaises(ValueError):
            self.store.list(status='other')
    def test_update(self):
        self.store.update(self.ids[0], title=' renamed ', status='done')
        row = self.store.list(query='renamed')['items'][0]
        self.assertEqual((row['title'], row['description'], row['status']), ('renamed', 'first', 'done'))
    def test_update_invalid_preserves_state(self):
        before = self.store.all()
        with self.assertRaises(ValueError):
            self.store.update(self.ids[0], title='changed', status='other')
        self.assertEqual(self.store.all(), before)
    def test_invalid_title(self):
        for title in ['', ' ', 'x'*101]:
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.store.update(self.ids[0], title=title)
    def test_unknown_id(self):
        with self.assertRaises(KeyError):
            self.store.update(9999, status='done')
    def test_csv_header_and_filtered_rows(self):
        rows = list(csv.reader(io.StringIO(self.store.export_csv(status='done'))))
        self.assertEqual(rows[0], ['id', 'title', 'description', 'status'])
        self.assertEqual(len(rows), 2)
    def test_csv_quotes_and_newlines(self):
        self.store.create('a,b', 'line1\nline2')
        rows = list(csv.DictReader(io.StringIO(self.store.export_csv(query='a,b'))))
        self.assertEqual(rows[0]['description'], 'line1\nline2')
    def test_csv_formula_neutralization(self):
        for initial in '=+-@':
            self.store.create(initial+'title', initial+'description')
        rows = list(csv.DictReader(io.StringIO(self.store.export_csv())))
        self.assertTrue(all(r['title'].startswith("'") and r['description'].startswith("'") for r in rows[3:]))
    def test_csv_empty(self):
        rows = list(csv.reader(io.StringIO(self.store.export_csv(query='nonexistent'))))
        self.assertEqual(len(rows), 1)
    def test_csv_not_limited_to_one_page(self):
        for i in range(25):
            self.store.create('extra '+str(i))
        self.assertEqual(len(list(csv.DictReader(io.StringIO(self.store.export_csv())))), 28)


class HttpAcceptance(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.id = self.store.create('日本語', '説明')
        self.server = make_server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.db.close()
    def request(self, path, body=None):
        req = urllib.request.Request(self.url+path, data=None if body is None else json.dumps(body).encode(),
            method='GET' if body is None else 'PUT', headers={'Content-Type':'application/json'})
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5)
    def test_list_endpoint(self):
        with self.request('/api/tasks?page=1&page_size=1') as response:
            self.assertEqual(json.load(response)['total'], 1)
    def test_bad_pagination(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/tasks?page=0')
        self.assertEqual(error.exception.code, 400)
    def test_update_endpoint(self):
        with self.request('/api/tasks/'+str(self.id), {'status':'done'}) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.store.all()[0]['status'], 'done')
    def test_unknown_update(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/tasks/9999', {'status':'done'})
        self.assertEqual(error.exception.code, 404)
    def test_export_endpoint(self):
        with self.request('/api/export?status=todo') as response:
            self.assertIn('text/csv', response.headers.get('Content-Type',''))
            self.assertIn('日本語', response.read().decode('utf-8-sig'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
