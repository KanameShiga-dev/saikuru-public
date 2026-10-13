"""Evaluator-written long command for the cancel evaluation. The AI must not edit it.

Appends one line per second to heartbeat.txt next to this file and stops by itself after
240 seconds, so a process left behind cannot run for long. No network or other file access.
"""
import sys
import time
from pathlib import Path

LIMIT_SECONDS = 240


def run(seconds=LIMIT_SECONDS, target=None):
    target = Path(target) if target else Path(__file__).resolve().with_name('heartbeat.txt')
    for n in range(1, seconds + 1):
        with target.open('a', encoding='utf-8') as handle:
            handle.write(f'{n} {time.time():.1f}\n')
        time.sleep(1)
    return seconds


if __name__ == '__main__':
    print('slow_task finished', run(), flush=True)
    sys.exit(0)
