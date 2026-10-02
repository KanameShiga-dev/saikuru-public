"""Start/stop without changing Windows PowerShell execution policy."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from team_config import ROOT


def health(url):
    try:
        with urllib.request.urlopen(url + '/health', timeout=1) as response:
            return json.load(response).get('app') == 'agent-team'
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status', 'login-claude', 'install-background', 'background-status'])
    parser.add_argument('--port', type=int, default=8790)
    args = parser.parse_args()
    if args.action in ('install-background', 'background-status'):
        powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        if args.action == 'background-status':
            command = "Get-ScheduledTask -TaskName AgentTeam.Background -ErrorAction SilentlyContinue | Select-Object TaskName,State,@{Name='LogonType';Expression={$_.Principal.LogonType}},@{Name='RunLevel';Expression={$_.Principal.RunLevel}} | Format-List"
            raise SystemExit(subprocess.call([powershell, '-NoProfile', '-Command', command]))
        env = dict(os.environ, AGENT_TEAM_INSTALL_ROOT=str(ROOT), AGENT_TEAM_INSTALL_PYTHON=sys.executable)
        command = (ROOT / 'install_background.ps1').read_text(encoding='utf-8-sig')
        raise SystemExit(subprocess.call([powershell, '-NoProfile', '-Command', command], env=env))
    if args.action == 'login-claude':
        from team_config import discover
        command = discover()['claude']
        if not command:
            raise SystemExit('Claude Code not found.')
        raise SystemExit(subprocess.call(command + ['auth', 'login', '--claudeai']))
    url = f'http://127.0.0.1:{args.port}'
    if args.action == 'status':
        print('Running: ' + url if health(url) else 'Not running')
        return
    if args.action == 'stop':
        (ROOT / 'data' / 'background.disabled').touch()
        if not health(url):
            raise SystemExit('采来 — サイクル — is not running on this port.')
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        client.open(url + '/').close()
        request = urllib.request.Request(url + '/api/shutdown', data=b'{}',
                    headers={'Content-Type': 'application/json', 'X-Agent-Team-UI': '1'})
        client.open(request).close()
        for _ in range(100):
            if not health(url):
                print('Stopped.')
                return
            time.sleep(.2)
        raise SystemExit('Shutdown still in progress; do not start another instance yet.')
    (ROOT / 'data' / 'background.disabled').unlink(missing_ok=True)
    if health(url):
        print('Already running: ' + url)
        return
    if os.name == 'nt':
        powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        find_task = subprocess.run([powershell, '-NoProfile', '-Command',
            "if (Get-ScheduledTask -TaskName AgentTeam.Background -ErrorAction SilentlyContinue) { exit 0 } else { exit 3 }"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        if find_task.returncode == 0:
            subprocess.run([powershell, '-NoProfile', '-Command',
                "Start-ScheduledTask -TaskName AgentTeam.Background -ErrorAction Stop"],
                check=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
            for _ in range(90):
                if health(url):
                    print('Started by Windows background task: ' + url)
                    return
                time.sleep(.5)
            raise SystemExit('Background task started, but server startup is not confirmed. Inspect background-status and server logs.')
    data = ROOT / 'data'
    data.mkdir(exist_ok=True)
    with (data / 'server.stdout.log').open('ab') as out, (data / 'server.stderr.log').open('ab') as err:
        proc = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'server.py'), '--port', str(args.port)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for _ in range(60):
        if proc.poll() is not None:
            raise SystemExit('Startup failed. See data/server.stderr.log. No policy was changed.')
        if health(url):
            print(f'Started: {url} (PID {proc.pid})')
            return
        time.sleep(.25)
    raise SystemExit('Startup unconfirmed. Inspect server log before trying again.')


if __name__ == '__main__':
    main()
