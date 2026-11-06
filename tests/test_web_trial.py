import copy
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

from team_p0_contract import artifact_records
from team_web_trial import WebTrials


class WebTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'output.txt').write_text('fictional output', encoding='utf-8')
        (self.root / 'start.ps1').write_text('param([int]$Port)', encoding='utf-8')
        (self.root / 'stop.ps1').write_text('param([int]$Port)', encoding='utf-8')
        self.job = {'id': 'fictional', 'project': str(self.root), 'p0_contract_version': 1,
                    'status': 'awaiting_acceptance',
                    'p0_artifacts': artifact_records(str(self.root), ['output.txt'])}
        store = SimpleNamespace(get=lambda *args: copy.deepcopy(self.job),
                                all=lambda *args: [copy.deepcopy(self.job)],
                                directory=self.root, event=Mock())
        self.app = SimpleNamespace(store=store, config={'approved_roots': [str(self.root)]},
                                   port=8790, engine=SimpleNamespace(project_blocked=lambda p: False))
        self.trials = WebTrials(self.app)
        self.addCleanup(self.trials.close)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.body = {'job_id': self.job['id'], 'start_script': 'start.ps1',
                     'stop_script': 'stop.ps1', 'port': self.port}

    def consent(self):
        prepared = self.trials.prepare(self.body)
        return {'token': prepared['token'], 'approve_start': True, 'approve_stop': True}

    def test_prepare_does_not_execute_and_each_command_needs_consent(self):
        with patch('team_web_trial.subprocess.Popen') as launch:
            prepared = self.trials.prepare(self.body)
            self.assertIn('-ExecutionPolicy Bypass', prepared['start_command'])
            self.assertIn('stop.ps1', prepared['stop_command'])
            for body in ({'token': prepared['token']},
                         {'token': prepared['token'], 'approve_start': True}):
                with self.assertRaises(ValueError):
                    self.trials.start(body)
            launch.assert_not_called()
        self.assertEqual(self.job['status'], 'awaiting_acceptance')

    def test_settings_selects_existing_scripts_and_unused_port_without_execution(self):
        (self.root / 'start.ps1').rename(self.root / 'start-server.ps1')
        (self.root / 'stop.ps1').rename(self.root / 'stop-server.ps1')
        with patch('team_web_trial.subprocess.Popen') as launch:
            settings = self.trials.settings(self.job['id'])
            self.assertTrue(settings['configured'])
            self.assertEqual(settings['start_script'], 'start-server.ps1')
            self.assertEqual(settings['stop_script'], 'stop-server.ps1')
            self.assertFalse(self.trials.occupied(settings['port']))
            self.assertIn('token', self.trials.prepare(dict(settings, job_id=self.job['id'])))
            launch.assert_not_called()

    def test_settings_does_not_guess_missing_or_ambiguous_scripts(self):
        (self.root / 'stop.ps1').unlink()
        self.assertFalse(self.trials.settings(self.job['id'])['configured'])
        (self.root / 'stop.ps1').write_text('param([int]$Port)', encoding='utf-8')
        for name in ('start-server.ps1', 'stop-server.ps1'):
            (self.root / name).write_text('param([int]$Port)', encoding='utf-8')
        self.assertFalse(self.trials.settings(self.job['id'])['configured'])

    def test_settings_preserves_artifact_version_and_scope_guards(self):
        (self.root / 'output.txt').write_text('unvalidated change', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.trials.settings(self.job['id'])
        self.job['status'] = 'running'
        with self.assertRaises(ValueError):
            self.trials.settings(self.job['id'])

    def test_script_change_or_output_change_invalidates_prepared_consent(self):
        for name in ('start.ps1', 'output.txt'):
            consent = self.consent()
            path = self.root / name
            original = path.read_bytes()
            path.write_bytes(original + b'\n# changed')
            with self.subTest(name=name), patch('team_web_trial.subprocess.Popen') as launch:
                with self.assertRaises(ValueError):
                    self.trials.start(consent)
                launch.assert_not_called()
            path.write_bytes(original)

    def test_expired_consent_unknown_token_and_busy_port_are_rejected(self):
        consent = self.consent()
        self.trials.pending[consent['token']]['expires'] = 0
        with self.assertRaises(ValueError):
            self.trials.start(consent)
        consent = self.consent()
        with patch.object(self.trials, 'occupied', return_value=True), patch('team_web_trial.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):
                self.trials.start(consent)
            launch.assert_not_called()

    def test_scope_and_script_restrictions(self):
        for change in ({'port': 8790}, {'port': 80}, {'port': True},
                       {'start_script': '../outside.ps1'}, {'start_script': str(self.root / 'start.ps1')},
                       {'start_script': 'output.txt'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.trials.prepare(dict(self.body, **change))
        self.app.config['approved_roots'] = []
        with self.assertRaises(ValueError):
            self.trials.prepare(self.body)

    def test_running_work_is_not_restarted(self):
        self.job['status'] = 'running'
        with self.assertRaises(ValueError):
            self.trials.prepare(self.body)

    def test_listener_must_belong_to_managed_process_and_use_loopback(self):
        entry = {'port': self.port, 'process': SimpleNamespace(pid=123)}
        for rows in ([{'LocalAddress': '0.0.0.0', 'OwningProcess': 123}],
                     [{'LocalAddress': '127.0.0.1', 'OwningProcess': 999}], []):
            with self.subTest(rows=rows), patch('team_web_trial.os.name', 'nt'), \
                    patch('team_web_trial.subprocess.run',
                          return_value=SimpleNamespace(returncode=0, stdout=json.dumps(rows))):
                with self.assertRaises(ValueError):
                    self.trials.check_listener(entry)

    def test_http_routes_require_browser_auth_and_preserve_acceptance_state(self):
        from server import Handler
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.app.port = server.server_port
        if self.body['port'] == self.app.port:
            self.body['port'] = 8766 if self.app.port != 8766 else 8767
        self.app.origin = f'http://127.0.0.1:{server.server_port}'
        self.app.cookie = 'fictional-cookie'
        self.app.web_trials = self.trials
        server.app = self.app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            path = self.app.origin + '/api/job/web-trial/prepare'
            payload = json.dumps(self.body).encode()
            request = urllib.request.Request(path, data=payload, headers={
                'Content-Type': 'application/json', 'X-Agent-Team-UI': '1'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
            client = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            with client.open(self.app.origin + '/') as response:
                response.read()
            with client.open(request) as response:
                prepared = json.load(response)
            self.assertIn('token', prepared)
            with client.open(self.app.origin + '/api/job/web-trial?job_id=' + self.job['id']) as response:
                self.assertEqual(json.load(response)['status'], 'stopped')
            with client.open(self.app.origin + '/api/job/web-trial/settings?job_id=' + self.job['id']) as response:
                settings = json.load(response)
                self.assertTrue(settings['configured'])
                self.assertEqual(settings['start_script'], 'start.ps1')
            self.assertEqual(self.job['status'], 'awaiting_acceptance')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_changed_stop_script_is_not_executed(self):
        consent = self.consent()
        process = Mock()
        process.poll.return_value = None
        with patch('team_web_trial.subprocess.Popen', return_value=process), \
                patch.object(self.trials, 'occupied', return_value=False), \
                patch('team_web_trial.threading.Thread'):
            self.trials.start(consent)
        (self.root / 'stop.ps1').write_text('changed stop', encoding='utf-8')
        with patch('team_web_trial.subprocess.run') as execute:
            with self.assertRaises(ValueError):
                self.trials.stop({'job_id': self.job['id'], 'confirmed': True})
            execute.assert_not_called()
        with patch('copilot_work_tools.stop_tree') as stop:
            self.trials.stop({'job_id': self.job['id'], 'confirmed': True, 'force_owned_process': True})
            stop.assert_called_once_with(process)

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell')
    def test_real_lifetime_readiness_staleness_and_stop(self):
        (self.root / 'start.ps1').write_text(r'''
param([int]$Port)
$ErrorActionPreference='Stop'
$flag=Join-Path $PSScriptRoot 'stop.flag'
if(Test-Path $flag){Remove-Item -LiteralPath $flag}
$listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$Port)
$listener.Start()
try {
 while(-not (Test-Path $flag)) {
  if(-not $listener.Pending()){Start-Sleep -Milliseconds 50;continue}
  $client=$listener.AcceptTcpClient()
  try {
   $stream=$client.GetStream()
   $stream.ReadTimeout=2000
   $reader=[IO.StreamReader]::new($stream,[Text.Encoding]::ASCII,$false,1024,$true)
   do {$line=$reader.ReadLine()} while ($null -ne $line -and $line -ne '')
   $reader.Dispose()
   $bytes=[Text.Encoding]::ASCII.GetBytes("HTTP/1.1 200 OK`r`nContent-Length: 2`r`nConnection: close`r`n`r`nOK")
   $stream.Write($bytes,0,$bytes.Length)
  } finally {$client.Close()}
 }
} finally {$listener.Stop()}
''', encoding='utf-8')
        (self.root / 'stop.ps1').write_text(
            "param([int]$Port)\nNew-Item -ItemType File -Path (Join-Path $PSScriptRoot 'stop.flag') -Force | Out-Null",
            encoding='utf-8')
        self.trials.start(self.consent())
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = self.trials.status(self.job['id'])
            if status['status'] != 'starting':
                break
            time.sleep(.2)
        self.assertEqual(status['status'], 'ready', status)
        (self.root / 'output.txt').write_text('new output', encoding='utf-8')
        self.assertEqual(self.trials.status(self.job['id'])['status'], 'stale')
        self.trials.stop({'job_id': self.job['id'], 'confirmed': True})
        self.assertEqual(self.trials.status(self.job['id'])['status'], 'stopped')
        self.assertEqual(self.job['status'], 'awaiting_acceptance')


if __name__ == '__main__':
    unittest.main()
