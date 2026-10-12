"""Manual screenshot fixture. No workers, monitors or destructive actions started."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from team_config import default_config
from server import make_server
import server as server_module
import team_ledger
import team_harness
import shutil
from team_store import now

root=Path(__file__).resolve().parent
team_ledger.ROOT=Path('C:/Projects')
team_harness.ROOT=Path('C:/Projects')
server_module.ROOT=root/'capture-runtime'
shutil.copytree(root.parents[1]/'web',server_module.ROOT/'web',dirs_exist_ok=True)
for display_file in (server_module.ROOT/'web').glob('*'):
    if display_file.suffix in ('.html','.js'):
        text=display_file.read_text(encoding='utf-8').replace('C:\\AI_Work','C:\\Projects').replace('C:\AI_Work','C:\Projects')
        display_file.write_text(text,encoding='utf-8')
data=root/'capture-data-anonymous'; data.mkdir(exist_ok=True)
project=Path('C:/Projects/Demo'); project.mkdir(parents=True,exist_ok=True)
(project/'README.md').write_text('# Demo project\nManual screenshot example.\n',encoding='utf-8')
config=default_config(); config['approved_roots']=[str(project)]
(data/'config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
server=make_server(data,8798)
app=server.app; app.engine.paused=True
if not app.store.all('job'):
    job=app.engine.create_job('説明用：画面を改善する','一覧の見やすさを改善する。資料撮影用データです。',str(project),False)
    task=app.store.all('task')[0]
    app.store.update(task['id'],status='succeeded',summary='説明用の計画が完成しました。')
    pending=app.engine.new_task(job,'説明用：実装前の判断','変更対象と検証方法を確認してください。','builder',task['id'])
    app.store.update(pending['id'],status='blocked',summary='対象範囲の確認が必要です。',failure_code='scope',
        result={'status':'blocked','summary':'説明用：変更範囲を確認してください。','checks':['未実行：撮影用'],
                'question':'どの画面を対象にしますか？','questions':[],'context_updates':[]})
    app.store.update(job['id'],status='blocked')
    app.engine.new_task(job,'説明用：独立レビュー','変更と証拠を確認します。','reviewer',pending['id'])
app.ledger.add({'path':str(project)})
print('Manual fixture: http://127.0.0.1:8798',flush=True)
try:
    server.serve_forever()
finally:
    app.engine.shutdown.set(); server.server_close(); app.engine.handoff.close()
    app.store.db.close(); app.ledger.db.close(); app.data_lock.close()
