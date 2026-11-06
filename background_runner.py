"""Task Scheduler entry point. Keep the local server alive outside desktop sessions."""
import json
import http.cookiejar
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from manage import health
from team_config import ROOT
from team_log_retention import has_pending_work, rotation_due, rotation_state, rotate_logs


def _request_shutdown():
    client = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    client.open('http://127.0.0.1:8790/', timeout=5).close()
    request = urllib.request.Request('http://127.0.0.1:8790/api/shutdown', data=b'{}',
        headers={'Content-Type': 'application/json', 'X-Agent-Team-UI': '1'})
    client.open(request, timeout=5).close()


def _can_rotate(data):
    try:
        return not has_pending_work(data / 'team.sqlite3')
    except (OSError, sqlite3.Error, ValueError):
        sys.stderr.write('Weekly log rotation deferred: task database could not be checked.\n')
        return False


def _rotate_if_idle(data, child, last_rotated):
    if child is None or not rotation_due(last_rotated):
        return child, last_rotated, False
    if not _can_rotate(data):
        return child, last_rotated, False
    try:
        _request_shutdown()
        child.wait(timeout=20)
    except (OSError, subprocess.TimeoutExpired, urllib.error.URLError):
        sys.stderr.write('Weekly log rotation deferred: graceful server shutdown was not confirmed.\n')
        return child, last_rotated, False
    if _can_rotate(data):
        try:
            last_rotated = rotate_logs(data)
        except OSError:
            sys.stderr.write('Weekly log rotation failed; existing task and audit records were preserved.\n')
            return None, last_rotated, False
    return None, last_rotated, True


def main():
    import msvcrt
    data = ROOT / 'data'
    data.mkdir(exist_ok=True)
    try:
        last_rotated = rotation_state(data)
    except (OSError, ValueError, json.JSONDecodeError):
        sys.stderr.write('Weekly log rotation disabled: its state file could not be read.\n')
        last_rotated = time.time()
    lock = (data / 'background.lock').open('a+b')
    if lock.tell() == 0:
        lock.write(b'0'); lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return
    child = None
    delay = 2
    last_rotation_check = 0.0
    try:
        while True:
            # Explicit maintenance stop persists until manage.py start.
            if (data / 'background.disabled').exists():
                time.sleep(3)
                continue
            server_running = health('http://127.0.0.1:8790')
            if child is not None and child.poll() is None:
                if server_running and time.monotonic() >= last_rotation_check:
                    if rotation_due(last_rotated):
                        child, last_rotated, rotated = _rotate_if_idle(data, child, last_rotated)
                        if rotated:
                            delay = 2
                    last_rotation_check = time.monotonic() + 60
                time.sleep(3)
                continue
            if child is not None:
                child = None
            if server_running:
                time.sleep(3)
                continue
            if rotation_due(last_rotated) and _can_rotate(data):
                last_rotated = rotate_logs(data)
            with (data / 'server.stdout.log').open('ab') as out, (data / 'server.stderr.log').open('ab') as err:
                child = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'server.py')],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(delay)
            delay = 2 if health('http://127.0.0.1:8790') else min(60, delay * 2)
    finally:
        lock.close()


if __name__ == '__main__':
    main()
