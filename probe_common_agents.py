"""Explicit live CLI smoke run in an isolated fixture; consumes CLI usage quota."""
import argparse
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from team_config import default_config, discover
from team_engine import Engine
from team_store import Store
from team_adapters import Process


def run(output, provider='mixed'):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    project = output / 'fixture'
    project.mkdir()
    marker = project / 'marker.txt'
    marker.write_text('before\n', encoding='utf-8')
    policy = ('This is an isolated 采来 — サイクル — integration fixture. Only marker.txt may be changed. '
              'Do not create other files, contact external services, or spawn agents. '
              'File tools and local CLI reads of marker.txt are permitted. Reviewers are read-only.\n')
    (project / 'AGENTS.md').write_text(policy, encoding='utf-8')
    (project / 'CLAUDE.md').write_text(policy, encoding='utf-8')
    store = Store(output / 'store')
    config = default_config()
    config.update(approved_roots=[str(project)], automatic_operations=True, max_parallel_projects=1,
                  task_timeout_seconds=240, approval_timeout_seconds=30)
    if provider in ('codex', 'claude'):
        config['roles'] = {role: provider + '-standard' for role in config['roles']}
    engine = None
    original_receive = Process.receive
    def capture_receive(process, ctx):
        message = original_receive(process, ctx)
        if message.get('type') in ('result', 'assistant') or ('id' in message and 'method' not in message):
            with (output / 'fixture-messages.jsonl').open('a', encoding='utf-8') as evidence:
                evidence.write(json.dumps(message, ensure_ascii=False) + '\n')
        return message
    Process.receive = capture_receive

    class Bridge(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                if self.path != '/worker/tool':
                    raise ValueError('fixture worker only')
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                answer = engine.tool_request(token, body)
                raw = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception:
                self.send_response(403)
                self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Bridge)
    engine = Engine(store, config, discover(), f'http://127.0.0.1:{server.server_port}')
    threading.Thread(target=server.serve_forever, daemon=True).start()
    start = time.time()
    try:
        goal = ('共通Agentの起動を確認するための隔離された検証依頼です。'
                '実装タスクは一件だけ。marker.txtを読み取り、ファイル編集ツールで'
                'beforeからafterへ変更してください。CLIでは対象ファイルの読み取りだけを許可し、'
                'その他ファイルは変更せず、テスト・依存導入・外部通信は行わない。'
                '調査タスクは不要です。通常レビューで内容がafterになっている証拠を確認する。'
                'この検証では共通セキュリティAgentの起動確認も必要なので、security_review_required=trueとする。'
                'questions=[]、context_updates=[]として単純な検証結果のみ報告する。')
        job = engine.create_job('isolated common agent smoke', goal, str(project), True)
        while time.time() - start < 360:
            for task in store.all('task'):
                if provider == 'mixed' and task.get('agent_name') == 'common-security-reviewer' and task['status'] == 'queued' and task['profile']['adapter'] != 'claude':
                    store.update(task['id'], profile=dict(adapter='claude', model='sonnet', effort='medium'))
            current = store.get(job['id'])
            if current['status'] in ('failed', 'blocked', 'interrupted', 'awaiting_acceptance') and not engine.active:
                break
            engine.tick()
            time.sleep(.2)
        tasks = store.all('task')
        agents = [t.get('agent_run') for t in tasks if t.get('agent_run')]
        expected = {'common-explorer', 'common-implementer', 'common-reviewer', 'common-security-reviewer'}
        success = (store.get(job['id'])['status'] == 'awaiting_acceptance' and
                   marker.read_text(encoding='utf-8').strip() == 'after' and
                   {r['name'] for r in agents} == expected and len(agents) == 4 and
                   all(r.get('session_id') and r['status'] == 'completed' for r in agents) and
                   len({r['session_id'] for r in agents}) == 4)
        if success:
            approval = next(a for a in store.all('approval') if a['kind'] == 'completion' and a['status'] == 'pending')
            engine.decide(approval['id'], True, 'isolated fixture: file and native session IDs verified')
        report = dict(success=success, job_status=store.get(job['id'])['status'], elapsed_seconds=round(time.time()-start, 2),
            marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest(),
            agents=agents, tasks=[dict(title=t['title'], status=t['status'], agent=t.get('agent_name'), summary=t.get('summary')) for t in tasks])
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(dict(success=success, job_status=report['job_status'],
                             agents=[dict(name=r['name'], provider=r['provider'], status=r['status'], session_id=r.get('session_id')) for r in agents]), ensure_ascii=False), flush=True)
        return success
    finally:
        Process.receive = original_receive
        engine.shutdown.set()
        for ctx in list(engine.active.values()):
            ctx.cancel.set()
        deadline = time.time() + 20
        while engine.active and time.time() < deadline:
            time.sleep(.1)
        server.shutdown()
        server.server_close()
        engine.handoff.close()
        store.db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--provider', choices=('mixed', 'codex', 'claude'), default='mixed')
    args = parser.parse_args()
    raise SystemExit(0 if run(args.output, args.provider) else 1)
