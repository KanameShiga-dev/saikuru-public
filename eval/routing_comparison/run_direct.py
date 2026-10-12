"""One direct Claude CLI canary using the selected synthetic task."""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from team_config import claude_config_directory, discover
from team_claude_auth import ClaudeAuth
from run_pair import BASE, FILE, GOAL, INITIAL, quality


def main():
    project = BASE / os.environ.get('ROUTING_DIRECT_NAME', 'direct')
    project.mkdir(exist_ok=False)
    (project / FILE).write_text(INITIAL, encoding='utf-8')
    scope = ('Synthetic comparison. Read only inside this folder and edit ' + FILE + ' only. '
             'No network, installation, deletion, shell commands, or external files. '
             'The independent evaluator will check the result.\n')
    (project / 'CLAUDE.md').write_text(scope, encoding='utf-8')
    command = discover()['claude'] + [
        '-p', '--restricted', '--output-format', 'stream-json', '--verbose',
        '--permission-mode', 'default', '--tools', 'Read,Edit,Glob,Grep',
        '--allowedTools', 'Edit', '--disallowedTools', 'Bash,Write,WebSearch,Agent,Task',
        '--model', 'sonnet', '--effort', 'medium', '--no-session-persistence']
    prompt = GOAL.replace('1人の制作担当で修正し、通常レビューとセキュリティレビューへ進めてください。',
                          '1人で修正してください。レビューはこのCLIの外で別途評価します。')
    prompt = prompt.replace('制作後、評価側が独立採点を実施し、EVALUATION_CHECKS.md に結果を記録します。',
                            '作業後、CLIの外で独立採点します。')
    prompt = prompt.replace('レビュー担当はこの証拠を読み、採点の未実施を推測せずに判断してください。', '')
    prompt = prompt.replace('予定済みの後続レビュー工程そのものは未確認の必須検証へ列挙しません。', '')
    # Match the production Claude worker's credential injection without recording the token.
    env = ClaudeAuth(claude_config_directory()).env(os.environ.copy())
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    started = time.time()
    try:
        run = subprocess.run(command, input=prompt, capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=180, cwd=project, env=env)
        outcome = None
        resolved_model = None
        for line in run.stdout.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get('type') == 'result':
                outcome = item
            if item.get('type') == 'system' and item.get('subtype') == 'init':
                resolved_model = item.get('model')
        usage = (outcome or {}).get('usage') or {}
        keys = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
        reported = sum(usage[key] for key in keys) if all(type(usage.get(key)) is int for key in keys) else None
        tokens = reported if run.returncode == 0 and not (outcome or {}).get('is_error') else None
        diagnostic = (run.stderr + ' ' + str((outcome or {}).get('result', ''))).casefold()
        categories = [name for name, markers in {
            'authentication': ('auth', 'login', 'sign in'),
            'permission': ('permission', 'denied', 'restricted'),
            'model': ('model', 'sonnet'),
            'network': ('network', 'connect', 'timeout'),
            'tool': ('tool', 'read', 'edit'),
        }.items() if any(marker in diagnostic for marker in markers)]
        result = {'route': 'direct', 'status': (outcome or {}).get('subtype') if run.returncode == 0 else 'cli_failed',
                  'returncode': run.returncode, 'independent_pass': quality(project),
                  'tokens_measured': tokens, 'usage_unknown': tokens is None,
                  'reported_tokens_on_failed_run': reported if run.returncode != 0 else None,
                  'diagnostic_categories': categories if run.returncode != 0 else [],
                  'resolved_model': resolved_model,
                  'seconds': round(time.time() - started, 1),
                  'source_sha256': hashlib.sha256((project / FILE).read_bytes()).hexdigest(),
                  'extra_files': sorted(p.name for p in project.iterdir()
                                        if p.name not in (FILE, 'CLAUDE.md'))}
    except subprocess.TimeoutExpired:
        result = {'route': 'direct', 'status': 'timeout', 'returncode': None,
                  'independent_pass': quality(project), 'tokens_measured': None,
                  'usage_unknown': True, 'seconds': round(time.time() - started, 1)}
    (BASE / 'direct-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
