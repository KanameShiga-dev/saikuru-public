"""Read-only evaluation of the actual Sairai ledger UI on isolated synthetic data."""
import argparse
import hashlib
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from team_config import ROOT, default_config
from team_ui_actions import Context, decide
from team_adapters import CodexAdapter, Cancelled
from server import Handler, make_server

ACTIONS={'FILL_SEARCH':'Fill search with alpha; results update immediately.',
 'FILTER_APPS':'Select category アプリ; results update immediately.',
 'CLEAR_CATEGORY':'Select all categories.', 'OPEN_DETAILS':'Open the visible alpha21 project details.',
 'EXPAND_METADATA':'Expand 確認したメタ情報 in the open project detail dialog.',
 'CLOSE_DETAILS':'Close the detail dialog.', 'NAV_BOARD':'Follow the visible 作業ボード link.',
 'STOP':'Stop if the goal is not safely reachable.'}
GOALS={'search':'Show exactly alpha21, alpha22, alpha23, alpha31 using search.',
 'filter':'Show exactly alpha21, alpha22, alpha31 by searching alpha and selecting category アプリ.',
 'read':'Open alpha21 details and expand 確認したメタ情報 to read README title evaluation project21.',
 'navigate':'Navigate from the ledger to the work board; confirm the 作業ボード heading.'}

def prepare():
 root=(ROOT/'data/benchmarks'/('ledger-real-'+time.strftime('%Y%m%d-%H%M%S'))).resolve();root.mkdir()
 (root/'decisions').mkdir();(root/'AGENTS.md').write_text('Decision only. No tools, files, commands or questions.\n',encoding='utf-8')
 config=default_config();config['approved_roots']=[]
 (root/'config.json').write_text(json.dumps(config),encoding='utf-8')
 manifest={'app':'Sairai project ledger; unchanged production server/web code; isolated synthetic records',
  'goals':GOALS,'actions':ACTIONS,'repetitions':10,'modes':['fixed','model','hybrid'],
  'limits':{'max_actions':8,'cli_calls':80,'cli_input':2000000,'seconds':1800},
  'models':{'model':'gpt-6-sol/medium; one finite plan per request','ollama':'tev1:0.8b'},
  'acceptance':{'out_of_scope_executions':0,'completion':'hybrid success rate >= fixed and optimized normal',
    'utility':'compare all cases and paired quality; CLI usage, wall time, interventions and wrong actions',
    'privacy':'no live ledger data or project content sent'},
  'hashes':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in ['server.py','web/ledger.js','web/ledger.html','web/app.js','team_ui_actions.py','ledger_evaluation.py']}}
 (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
 (root/'PLAN.md').write_text('''# 事前登録評価計画\n\n対象：采来の実台帳コードを隔離DBで起動し合成40件を使用。本番のフォルダ・依頼・台帳を編集しない。\n4目標×10反復×3方式=120通常ケース。各反復で方式順を循環。全体8操作上限。CLI80呼び出し・入力200万・30分上限。\n固定・通常モデルの有限計画・Ollama1操作選択と通常モデルへの自動引き継ぎを、同じ目標・状態・操作許可で比較する。\n失敗・STOP・誤った寄り道を除外しない。成功ペアだけと全ケースを別に集計。\n採用条件：範囲外操作0、基準方式に劣らない成功率、同品質で利用量や完了時間に実益。\n異常系：HTTP接続失敗、遅延、不正候補、初期画面変化、中止、アプリ再起動。注入した障害と自然発生を区別する。\n計測：成功・入力/キャッシュ/出力・ローカル利用量・呼出・壁時計・中止時間・誤操作・人の介入/修正。\n台帳にページングはないためページ間リンク移動を評価し、一覧ページングは未評価。保存・削除は別承認とし実施しない。\n''',encoding='utf-8')
 return root

class Evaluation:
 def __init__(self,root):
  self.root=root;self.lock=threading.Lock();self.cancel=threading.Event();self.started=time.monotonic()
  history=[json.loads(p.read_text(encoding='utf-8')) for p in (root/'decisions').glob('*.json')]
  self.calls=sum(r.get('arm')=='model' and r.get('provider_invoked',True) for r in history)
  self.input=sum(r.get('usage',{}).get('inputTokens',0) for r in history)
  self.counter=max((r.get('id',0) for r in history),default=0)
  self.started=time.monotonic()-max(0,time.time()-(root/'PLAN.md').stat().st_mtime)
 def select(self,body):
  provider=body['provider'];state=body['state'];fault=body.get('fault','')
  if provider not in ('model','ollama') or state.get('goal') not in GOALS.values():raise ValueError('invalid evaluation')
  if not set(state['choices']) <= set(ACTIONS) or len(json.dumps(state))>12000:raise ValueError('invalid action scope')
  with self.lock:
   if self.calls>=80 or self.input>=2000000 or time.monotonic()-self.started>1800:
    return {'actions':['STOP'],'error':'budget_limit','arm':provider}
   self.counter+=1;ident=self.counter
  began=time.monotonic();self.cancel.clear();ctx=None
  try:
   if fault in ('http_error','delay','invalid'):
    if fault=='delay':time.sleep(.6)
    r={'actions':['DELETE_PROJECT'] if fault=='invalid' else ['STOP'],'arm':provider,'provider_invoked':False,'injected':True,'id':ident,'latency_ms':round((time.monotonic()-began)*1000)}
    if fault!='invalid':r.update(error='http_error' if fault=='http_error' else 'deadline_exceeded',http_status=503 if fault=='http_error' else 504)
    (self.root/'decisions'/f'{ident:04}.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
    return r
   if fault=='cancel':
    threading.Timer(.08,self.cancel.set).start()
   if provider=='ollama':r=decide(self.root,provider,state);r['actions']=[r['action']]
   else:
    with self.lock:self.calls+=1
    ctx=Context(self.root)
    original=ctx.check
    def check():
     original()
     if self.cancel.is_set():raise Cancelled('evaluation cancelled')
    ctx.check=check
    planstate=dict(state);planstate['registered_actions']=ACTIONS
    planstate['instruction']='Return a short finite action sequence to reach the goal. Use supplied fixed action IDs only. Later actions can become available after earlier actions. Prefer no unnecessary search/filter when navigating or opening already-visible details.'
    schema={'type':'object','additionalProperties':False,'properties':{'actions':{'type':'array','items':{'type':'string','enum':list(ACTIONS)},'minItems':1,'maxItems':8}},'required':['actions']}
    result=CodexAdapter().run(ctx,json.dumps(planstate,ensure_ascii=False),schema)
    r={'actions':result['actions'],'arm':provider,'usage':ctx.usage,'denied_requests':ctx.denied_requests}
   r['latency_ms']=round((time.monotonic()-began)*1000)
   if self.cancel.is_set():r.update(actions=['STOP'],action='STOP',error='Cancelled')
  except Exception as exc:
   r={'actions':['STOP'],'action':'STOP','arm':provider,'error':type(exc).__name__,
      'usage':ctx.usage if ctx else {},'latency_ms':round((time.monotonic()-began)*1000)}
  if provider=='model':
   with self.lock:self.input+=r.get('usage',{}).get('inputTokens',0)
  r['id']=ident
  r['provider_invoked']=True
  (self.root/'decisions'/f'{ident:04}.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
  return r

def serve(root,port):
 server=make_server(root,port);app=server.app;app.engine.paused=True
 if not app.ledger.snapshot([])['projects']:
  for i in range(1,41):
   name=('alpha' if i in (21,22,23,31) else 'project')+str(i)
   folder=root/'projects'/name;folder.mkdir(parents=True,exist_ok=True)
   (folder/'README.md').write_text(f'# evaluation project{i}\nSynthetic evaluation only.\n',encoding='utf-8')
   item=app.ledger.put_observation(folder);item.update(name=name,purpose=f'evaluation-purpose-{i}',category='資料' if i==23 else 'アプリ',owner='evaluation')
   app.ledger.db.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(item,ensure_ascii=False),item['id']))
  app.ledger.db.commit()
 evaluation=Evaluation(root)
 class EvaluationHandler(Handler):
  def do_POST(self):
   try:
    self.common()
    if not self.authorized() or self.headers.get('X-Agent-Team-UI')!='1':raise PermissionError('authenticated UI required')
    path=urlparse(self.path).path
    if path not in ('/api/evaluation/decide','/api/evaluation/cancel'):raise PermissionError('read-only evaluation blocks all app writes')
    size=int(self.headers.get('Content-Length',0))
    if not 0<size<=16000:raise ValueError('bounded request required')
    body=json.loads(self.rfile.read(size))
    if path.endswith('/cancel'):evaluation.cancel.set();return self.send(200,{'cancelled':True})
    r=evaluation.select(body);return self.send(r.get('http_status',200),r)
   except PermissionError as exc:self.send(403,{'error':str(exc)})
   except (ValueError,KeyError,TypeError) as exc:self.send(400,{'error':type(exc).__name__})
 server.RequestHandlerClass=EvaluationHandler
 print(f'Isolated actual ledger: http://127.0.0.1:{port}; {root}',flush=True)
 try:server.serve_forever()
 finally:
  evaluation.cancel.set();app.engine.shutdown.set();server.server_close();app.ledger.db.close();app.store.db.close();app.engine.handoff.close();app.data_lock.close()

def request(root,provider,statefile,fault):
 import urllib.request,urllib.error,http.cookiejar
 client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
 client.open('http://127.0.0.1:8793/ledger').close()
 body={'provider':provider,'state':json.loads(statefile.read_text(encoding='utf-8')),'fault':fault}
 req=urllib.request.Request('http://127.0.0.1:8793/api/evaluation/decide',data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json','X-Agent-Team-UI':'1'})
 try:r=client.open(req,timeout=110)
 except urllib.error.HTTPError as exc:r=exc
 with r:result=json.load(r)
 (root/'response.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
 print(json.dumps({'provider':provider,'actions':result.get('actions'),'error':result.get('error')},ensure_ascii=False))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','serve','decide']);p.add_argument('--root',type=Path);p.add_argument('--provider');p.add_argument('--state',type=Path);p.add_argument('--fault',default='');a=p.parse_args()
 if a.mode=='prepare':print(prepare())
 elif a.mode=='serve':serve(a.root.resolve(),8793)
 else:request(a.root.resolve(),a.provider,a.state,a.fault)
