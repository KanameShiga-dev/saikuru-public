"""Optional live smoke probe: sends synthetic text, denies all tool requests.

Uses the existing CLI subscription/quota. No project files are supplied in the prompt.
"""
import argparse
import json
import time
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
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
        profile = ({'adapter': 'copilot', 'model': 'gpt-5-mini', 'effort': 'low'} if adapter == 'copilot'
                   else default_config()['profiles'][adapter + '-standard'])
        self.task = {'id': 'connectivity-probe', 'profile': profile}
        self.task['role'] = 'planner' if adapter == 'copilot' else 'builder'
        self.text_only = True
        self.command = discover()[adapter]
        if not self.command:
            raise ProviderError('CLI not found')
        self.engine = SimpleNamespace(config={'computer_use_allowed': False})
        self.agent_definition = SimpleNamespace(name='connectivity_probe', description='Connectivity probe',
                                                sandbox='read-only', tools=())
        self.agent_system_instructions = 'Connectivity probe only. Do not use tools or access files.'
        self.agent_run = {'id': str(uuid.uuid4()), 'session_id': None}
        self.deadline = time.time() + 120

    def check(self):
        if time.time() > self.deadline:
            raise ProviderError('Probe timeout')

    def event(self, text):
        print(text, flush=True)

    def approve(self, _):
        return {'allow': False, 'note': 'Probe: all tool calls are denied'}

    def agent_started(self, session_id, *_args):
        self.agent_run['session_id'] = session_id

    def record_usage(self, *_args):
        return None

    def observe_message(self, *_args):
        return None


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('provider', choices=list(ADAPTERS))
    args = parser.parse_args()
    prompt = ('Connectivity check only. Do not use tools. Do not read or write files. '
              'Return exactly this object using the output schema: '
              '{"status":"done","summary":"connection confirmed","checks":[],"pending_checks":[],"question":""}')
    try:
        value = ADAPTERS[args.provider]().run(ProbeContext(args.provider), prompt, RESULT_SCHEMA)
        print(json.dumps({'provider': args.provider, 'probe': 'passed', 'result': value}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'provider': args.provider, 'probe': 'failed', 'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
