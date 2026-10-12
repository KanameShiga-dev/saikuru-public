import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from server import Handler
from team_ui_automation import UiAutomation

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        port=self.server.server_port
        self.url=f'http://127.0.0.1:{port}'
        self.server.app=SimpleNamespace(port=port,origin=self.url,cookie='synthetic-test-cookie',engine=None,
            ui_automation=UiAutomation(self.temp.name,lambda *_:{'action':'SEARCH','arm':'ollama','usage':{}}))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.temp.cleanup()
    def post(self,headers=None,body=None):
        h={'Content-Type':'application/json','X-Agent-Team-UI':'1'};h.update(headers or {})
        r=urllib.request.Request(self.url+'/api/ui-automation/start',data=json.dumps(body or
            {'mode':'hybrid','goal':'search','consent':True}).encode(),headers=h)
        return json.load(self.client.open(r))
    def test_auth_required(self):
        with self.assertRaises(urllib.error.HTTPError) as e:self.post()
        self.assertEqual(e.exception.code,403)
        e.exception.close()
    def test_page_cookie_then_start(self):
        html=self.client.open(self.url+'/operation-tests').read()
        self.assertIn(b'operation-tests.js',html)
        self.assertEqual(self.post()['status'],'running')
        history=json.load(self.client.open(self.url+'/api/ui-automation/history'))
        self.assertEqual(len(history['records']),1)
    def test_cross_origin_rejected(self):
        self.client.open(self.url+'/operation-tests').close()
        with self.assertRaises(urllib.error.HTTPError) as e:self.post({'Origin':'http://foreign.invalid'})
        self.assertEqual(e.exception.code,403)
        e.exception.close()
    def test_ui_header_required(self):
        self.client.open(self.url+'/operation-tests').close()
        with self.assertRaises(urllib.error.HTTPError) as e:self.post({'X-Agent-Team-UI':'0'})
        self.assertEqual(e.exception.code,403)
        e.exception.close()
    def test_arbitrary_goal_rejected(self):
        self.client.open(self.url+'/operation-tests').close()
        with self.assertRaises(urllib.error.HTTPError) as e:self.post(body={'goal':'delete','mode':'hybrid','consent':True})
        self.assertEqual(e.exception.code,400)
        e.exception.close()

if __name__=='__main__':unittest.main()
