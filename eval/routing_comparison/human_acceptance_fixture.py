"""Serve one local, synthetic acceptance card without starting any AI worker."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import make_server
from team_config import ROOT, default_config


def main():
    base = Path(tempfile.mkdtemp(prefix='routing-human-', dir=ROOT / 'data'))
    project = base / 'project'
    runtime = base / 'runtime'
    project.mkdir()
    runtime.mkdir()
    (project / 'labels.py').write_text('def normalize_labels(values):\n'
                                             '    result = []\n'
                                             '    for value in values:\n'
                                             '        normalized = value.strip().lower()\n'
                                             '        if normalized:\n'
                                             '            result.append(normalized)\n'
                                             '    return result\n', encoding='utf-8')
    config = default_config()
    config['approved_roots'] = [str(project)]
    (runtime / 'config.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    server = make_server(runtime, 0)
    app = server.app
    try:
        job = app.engine.create_job('架空の文字列処理・画面確認',
                                    '架空のlabels.pyの結果と未確認項目を画面で確認する。実案件ではありません。',
                                    str(project), True)
        for task in app.store.all('task'):
            if task['job_id'] == job['id']:
                app.store.update(task['id'], status='cancelled')
        review = app.engine.new_task(job, '架空の独立確認', '隔離済みの合成結果を画面へ表示する。', 'reviewer')
        app.store.update(review['id'], status='succeeded', result={
            'status': 'done', 'summary': '架空の文字列課題。自動採点は合格。人の画面確認2項目は未記録。',
            'checks': ['空配列・前後空白・空白のみ・重複・入力不変を隔離採点で確認。'],
            'pending_checks': ['受領画面で未確認項目と根拠の表示を確認',
                               '条件付き受領後も残る未確認項目が表示されていることを確認']})
        app.store.update(job['id'], status='awaiting_acceptance')
        app.engine.new_approval(review, 'completion', {
            'summary': '架空課題の受領画面です。実際に確認した項目だけ根拠を入力してください。',
            'checks': ['隔離採点器の結果：文字列の正規化は合格。'],
            'review_findings': [],
            'pending_items': [{'title': '利用者による画面確認',
                               'note': '画面で実際に見たことだけを記録してください。',
                               'checks': ['受領画面で未確認項目と根拠の表示を確認',
                                          '条件付き受領後も残る未確認項目が表示されていることを確認']}],
            'note': 'これは隔離された架空案件です。通常サービスの案件ではありません。',
        })
        (base / 'manifest.json').write_text(json.dumps({
            'purpose': 'synthetic human acceptance UI', 'job_id': job['id'],
            'url': app.origin, 'project': str(project), 'runtime': str(runtime)},
            ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'url': app.origin, 'job_id': job['id'], 'fixture': str(base)}, ensure_ascii=False), flush=True)
        server.serve_forever()
    finally:
        app.engine.shutdown.set()
        server.server_close()
        app.engine.handoff.close()
        app.store.db.close()
        app.data_lock.close()


if __name__ == '__main__':
    main()
