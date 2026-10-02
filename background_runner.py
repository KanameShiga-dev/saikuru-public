"""Task Scheduler entry point. Keep the local server alive outside desktop sessions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from manage import health
from team_config import ROOT


def main():
    import msvcrt
    data = ROOT / 'data'
    data.mkdir(exist_ok=True)
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
    try:
        while True:
            # Explicit maintenance stop persists until manage.py start.
            if (data / 'background.disabled').exists() or health('http://127.0.0.1:8790'):
                time.sleep(3)
                continue
            if child is not None and child.poll() is None:
                time.sleep(3)
                continue
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
