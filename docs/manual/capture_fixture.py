"""Manual screenshot fixture. No workers, monitors or destructive actions started."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from team_config import default_config
from server import make_server
import server as server_module
import shutil
import tempfile

root=Path(__file__).resolve().parent
server_module.ROOT=root/'capture-runtime'
shutil.copytree(root.parents[1]/'web',server_module.ROOT/'web',dirs_exist_ok=True)
data=root/'capture-data-anonymous'; data.mkdir(exist_ok=True)
# The work folder must lie outside Saikuru's own folder, so the demo uses the OS temp folder.
workspace=Path(tempfile.gettempdir())/'saikuru-capture-workspace'
project=workspace/'Demo'; project.mkdir(parents=True,exist_ok=True)
(project/'README.md').write_text('# Demo project\nManual screenshot example.\n',encoding='utf-8')
config=default_config(); config['approved_roots']=[str(project)]; config['workspace_root']=str(workspace)
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
