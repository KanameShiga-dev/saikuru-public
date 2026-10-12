"""One isolated full-route synthetic Markdown job; no production DB or project."""
import hashlib
import http.cookiejar
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server import make_server
from team_config import default_config
from team_optimization_metrics import attempts
from eval.routing_comparison.case_document import BRIEF, INITIAL, REQUEST, quality
from eval.routing_comparison.review_sample_metrics import reviewer_prompts

BASE = ROOT / 'data' / ('routing-comparison-20261012-' + os.environ.get('ROUTING_DOC_RUN', 'document2'))
DATA = BASE / 'runtime'
PROJECT = BASE / 'project'
LIMIT = int(os.environ.get('ROUTING_EVAL_LIMIT', '1000000'))
PREVIOUS = int(os.environ.get('ROUTING_EVAL_PREVIOUS', '574859'))
STOP_AT = int(os.environ.get('ROUTING_EVAL_STOP_NEW_AT', '800000'))
REVIEW_CONTEXT_MODE = os.environ.get('ROUTING_REVIEW_CONTEXT_MODE', 'off')
if REVIEW_CONTEXT_MODE not in ('off', 'shadow', 'on'):
    raise ValueError('ROUTING_REVIEW_CONTEXT_MODE must be off, shadow or on')
if REVIEW_CONTEXT_MODE != 'off' and any(key not in os.environ for key in
                                        ('ROUTING_DOC_RUN', 'ROUTING_EVAL_LIMIT', 'ROUTING_EVAL_PREVIOUS',
                                         'ROUTING_EVAL_STOP_NEW_AT')):
    raise ValueError('Review-context comparison requires an explicit run and fresh approved budget values')
if not 0 <= PREVIOUS < STOP_AT < LIMIT:
    raise ValueError('No budget margin for this isolated run')


def measured():
    rows, _, _ = attempts(DATA / 'team.sqlite3')
    values = [(r.get('input_total'), r.get('output')) for r in rows]
    return PREVIOUS + sum(i + o for i, o in values if i is not None and o is not None)


def main():
    BASE.mkdir(exist_ok=False)
    DATA.mkdir()
    PROJECT.mkdir()
    (PROJECT / 'BRIEF.md').write_text(BRIEF, encoding='utf-8')
    (PROJECT / 'guide-old.md').write_text(INITIAL, encoding='utf-8')
    rule = ('Synthetic document evaluation. Read BRIEF.md and guide-old.md; create guide.md only. '
            'No network, shell, installs, deletion, external files, or publication.\n')
    for name in ('AGENTS.md', 'CLAUDE.md', 'SCOPE.md'):
        (PROJECT / name).write_text(rule, encoding='utf-8')
    config = default_config()
    config.update(approved_roots=[str(PROJECT)], routing_evaluation={'enabled': True, 'root': str(BASE)},
                  review_context_mode=REVIEW_CONTEXT_MODE, max_parallel_projects=1,
                  automatic_operations=False, task_timeout_seconds=240,
                  approval_timeout_seconds=240, max_repairs=1,
                  context_budget={'mode': 'off', 'reuse_chars': 8000})
    config['local_summary']['mode'] = 'off'
    config['context_optimization'] = {'mode': 'off'}
    for role in config['roles']:
        config['roles'][role] = 'claude-standard'
    (DATA / 'config.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    manifest = {'purpose': 'synthetic Markdown full route', 'previous_measured': PREVIOUS,
                'limit': LIMIT, 'stop_at': STOP_AT, 'goal_sha256': hashlib.sha256(REQUEST.encode()).hexdigest(),
                'review_context_mode': REVIEW_CONTEXT_MODE,
                'brief_sha256': hashlib.sha256(BRIEF.encode()).hexdigest(), 'result': None}
    (BASE / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    server = make_server(DATA, int(os.environ.get('ROUTING_EVAL_PORT', '8808')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    jar = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    client.open(server.app.origin + '/', timeout=30).close()

    def api(path, body=None):
        request = urllib.request.Request(server.app.origin + path,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={'Content-Type': 'application/json', 'X-Agent-Team-UI': '1'})
        with client.open(request, timeout=40) as response:
            return json.load(response)

    job_id = None
    try:
        profile = {'adapter': 'claude', 'model': 'sonnet', 'effort': 'medium'}
        job = api('/api/jobs', {'title': '架空の図書室案内修正', 'goal': REQUEST, 'project': str(PROJECT),
                                'kind': 'documentation', 'document_format': 'md', 'consent': True,
                                'auto_execute': True, 'evaluation_mode': 'A', 'evaluation_confirmed': True,
                                'role_profiles': {role: profile for role in ('planner', 'builder', 'researcher', 'reviewer')}})
        job_id = job['id']
        baseline_files = {p.name for p in PROJECT.iterdir()}
        print('SUBMITTED document full', flush=True)
        started, last, scored_builders = time.time(), None, set()
        while True:
            state = api('/api/state')
            status = next(item['status'] for item in state['jobs'] if item['id'] == job_id)
            if status != last:
                print('STATUS', status, flush=True)
                last = status
            completed_builders = {t['id'] for t in state['tasks']
                                  if t['job_id'] == job_id and t['role'] == 'builder' and t['status'] == 'succeeded'}
            if completed_builders - scored_builders:
                passed = quality(PROJECT)
                (PROJECT / 'EVALUATION_CHECKS.md').write_text(
                    '# 独立採点結果（評価ランナー）\n\n' + ('判定: 合格\n' if passed else '判定: 不合格\n')
                    + '対象: guide.md。BRIEF.mdの時刻・冊数・期限・返却・予約を確認。\n', encoding='utf-8')
                server.app.store.event('evaluation_check', '分離評価側の資料採点を記録しました。', job_id=job_id)
                scored_builders.update(completed_builders)
                print('INDEPENDENT_CHECK', passed, flush=True)
            server.app.engine.tick()
            for approval in state['approvals']:
                if approval['job_id'] != job_id or approval['status'] != 'pending':
                    continue
                if approval['kind'] == 'completion' and quality(PROJECT):
                    api('/api/decide', {'id': approval['id'], 'allow': True,
                                        'note': 'Synthetic checker passed; proxy acceptance only.'})
                elif approval['kind'] in ('tool', 'question', 'plan'):
                    print('NEEDS_DECISION', approval['kind'], flush=True)
                    break
            if measured() >= STOP_AT or time.time() - started > 600:
                for context in list(server.app.engine.active.values()):
                    if context.task['job_id'] == job_id:
                        context.cancel.set()
                print('STOP_LIMIT', measured(), flush=True)
                break
            if status in ('accepted', 'accepted_with_pending_checks', 'blocked', 'failed', 'cancelled', 'interrupted'):
                break
            time.sleep(3)
        while any(ctx.task['job_id'] == job_id for ctx in server.app.engine.active.values()):
            time.sleep(1)
        rows, _, _ = attempts(DATA / 'team.sqlite3')
        runs = [r for r in rows if r['job_id'] == job_id]
        final = {'status': server.app.store.get(job_id)['status'], 'independent_pass': quality(PROJECT),
                 'tokens_measured': sum((r.get('input_total') or 0) + (r.get('output') or 0) for r in runs),
                 'unknown_usage': sum(r['measurement'] == 'unknown' for r in runs),
                 'roles': [r['role'] for r in runs], 'seconds': round(time.time() - started, 1),
                 'reviewer_prompts': reviewer_prompts(server.app.store, job_id),
                 'unexpected_new_files': sorted(p.name for p in PROJECT.iterdir()
                                                if p.name not in baseline_files | {'guide.md', 'EVALUATION_CHECKS.md'}),
                 'old_guide_unchanged': (PROJECT / 'guide-old.md').read_text(encoding='utf-8') == INITIAL}
        manifest['result'] = final
        (BASE / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print('RESULT', json.dumps(final, ensure_ascii=False), flush=True)
    finally:
        server.app.engine.shutdown.set()
        for context in list(server.app.engine.active.values()):
            context.cancel.set()
        server.shutdown()
        server.server_close()
        server.app.engine.handoff.close()
        server.app.store.db.close()
        server.app.data_lock.close()
        print('EVALUATION_SERVER_STOPPED', flush=True)


if __name__ == '__main__':
    main()
