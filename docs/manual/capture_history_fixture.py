"""Read-only screenshot fixture; synthetic data, no workers or production DB."""
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse,parse_qs
ROOT=Path(__file__).resolve().parents[2]
BASE=1790989200
P='C:/Projects/SamplePortal'
Q='C:/Projects/SampleInventory'
def task(id,job,title,role,start,end,status):
 return dict(id=id,job_id=job,title=title,role=role,profile={'adapter':'codex' if role!='builder' else 'claude','model':'gpt-6-sol' if role!='builder' else 'Claude'},status=status,created_at=BASE,started_at=BASE+start,finished_at=BASE+end if end else None,attempt=1,summary='説明用サンプル：工程の記録')
JOBS=[dict(id='demo-ledger',title='サンプル：一覧画面の検索を改善',project=P,document_source=None,status='completed',created_at=BASE,origin={'kind':'ledger','project':P}),dict(id='demo-new',title='サンプル：利用ガイドを作成',project=Q,document_source=None,status='blocked',created_at=BASE+2400,origin={'kind':'new'})]
TASKS=[task('plan','demo-ledger','作業計画を作る','planner',0,180,'succeeded'),task('build','demo-ledger','検索とフィルターを改善','builder',180,720,'succeeded'),task('review','demo-ledger','成果をレビューする','reviewer',720,960,'succeeded'),task('guide','demo-new','資料の対象範囲を整理','planner',2400,2580,'succeeded'),task('wait','demo-new','資料を作成する','builder',2580,2640,'blocked')]
APPROVALS=[dict(id='a1',job_id='demo-ledger',kind='plan',status='approved',created_at=BASE+180,decided_at=BASE+210,note='サンプル：検索・フィルターの変更範囲を確認。'),dict(id='a2',job_id='demo-ledger',kind='completion',status='approved',created_at=BASE+960,decided_at=BASE+1080,note='サンプル：成果物と検証結果を確認し受け入れ。')]
EVENTS=[dict(seq=i+1,job_id='demo-ledger',type=kind,message=msg,at=BASE+at) for i,(at,kind,msg) in enumerate([(0,'job_created','台帳の相談内容から依頼を作成しました。'),(180,'approval','計画の確認を依頼しました。'),(210,'decision','計画が承認されました。'),(720,'task_finished','検索・フィルターの改善工程が終了しました。'),(960,'review','レビューが終了しました。'),(1080,'completion','成果を受け入れ、依頼を完了しました。')])][::-1]
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  path=urlparse(self.path).path
  if path=='/api/ledger':value={'projects':[{'path':P,'name':'業務ポータル（サンプル）'},{'path':Q,'name':'在庫管理（サンプル）'}]}
  elif path=='/api/project-history':
   job=parse_qs(urlparse(self.path).query).get('job_id',[''])[0];value=dict(jobs=JOBS,tasks=TASKS,approvals=APPROVALS if job=='demo-ledger' else [],events=EVENTS if job=='demo-ledger' else [],next_before=None)
  else:
   files={'/history':'history.html','/history.js':'history.js','/history.css':'history.css'}
   if path not in files:self.send_error(404);return
   data=(ROOT/'web'/files[path]).read_bytes();self.send_response(200);self.send_header('Content-Type',{'html':'text/html','js':'text/javascript','css':'text/css'}[files[path].split('.')[-1]]+'; charset=utf-8');self.end_headers();self.wfile.write(data);return
  data=json.dumps(value,ensure_ascii=False).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
HTTPServer(('127.0.0.1',8841),Handler).serve_forever()
