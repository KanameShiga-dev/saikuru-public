"""Isolated UI acceptance fixture. No provider or scheduler is started."""
from pathlib import Path
from server import make_server
from team_engine import PLAN_SCHEMA


if __name__ == '__main__':
    server = make_server(Path('.test-output/ui-fixture'), 8788)
    engine = server.app.engine
    job = engine.create_job('画面検証（模擬・AI呼び出しなし）', '承認カードの操作を確認する', server.app.config['approved_roots'][0], False)
    task = [t for t in engine.store.all('task') if t['job_id'] == job['id']][0]
    plan = {'summary': 'これは画面検証専用の模擬計画です。実際のAIには接続していません。',
            'tasks': [{'title': '模擬の作業', 'instruction': '画面テスト。実行されません。', 'role': 'builder'}]}
    engine._finish(task, plan)
    print('UI fixture: http://127.0.0.1:8788', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close(); server.app.store.db.close(); server.app.data_lock.close()
