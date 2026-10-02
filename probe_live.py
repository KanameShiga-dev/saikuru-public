"""Optional live smoke probe: sends synthetic text, denies all tool requests.

Uses the existing CLI subscription/quota. No project files are supplied in the prompt.
"""
import argparse
import json
import time
import sys
from pathlib import Path
from team_adapters import ADAPTERS, ProviderError
from team_config import ROOT, discover, default_config
from team_engine import RESULT_SCHEMA


class ProbeContext:
    writable = False
    token = 'probe-has-no-permission'
    endpoint = 'http://127.0.0.1:1'
    approval_timeout = 10

    def __init__(self, adapter):
        self.project = str(ROOT / 'sample-project')
        self.task = {'id': 'connectivity-probe', 'profile': default_config()['profiles'][adapter + '-standard']}
        self.command = discover()[adapter]
        if not self.command:
            raise ProviderError('CLI not found')
        self.deadline = time.time() + 120

    def check(self):
        if time.time() > self.deadline:
            raise ProviderError('Probe timeout')

    def event(self, text):
        print(text, flush=True)

    def approve(self, _):
        return {'allow': False, 'note': 'Probe: all tool calls are denied'}


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('provider', choices=list(ADAPTERS))
    args = parser.parse_args()
    prompt = ('Connectivity check only. Do not use tools. Do not read or write files. '
              'Return exactly this object using the output schema: '
              '{"status":"done","summary":"connection confirmed","checks":[],"question":""}')
    try:
        value = ADAPTERS[args.provider]().run(ProbeContext(args.provider), prompt, RESULT_SCHEMA)
        print(json.dumps({'provider': args.provider, 'probe': 'passed', 'result': value}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'provider': args.provider, 'probe': 'failed', 'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
