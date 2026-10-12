"""Real HomeAppStatus UI on synthetic API, plus separately labelled paging fixture."""
import argparse,hashlib,http.cookiejar,json,secrets,threading,time,urllib.request,urllib.error
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from team_config import ROOT,default_config
from team_ui_actions import Context,decide
from team_adapters import ClaudeAdapter,Cancelled
from team_config import discover
from types import SimpleNamespace
import uuid
APP=Path('C:/AI_Work/Projects/Operations/HomeAppStatus')
ACTIONS={'SEARCH':'Search alpha','RUNNING':'Filter 起動中','ALL':'Filter すべて','APPS':'Select アプリ状態 tab',
 'SCHEDULES':'Select 再起動・スケジュール tab','REFRESH':'Press 更新','NEXT':'Next page','PREVIOUS':'Previous page','STOP':'Stop without executing'}
GOALS={'compound':'Show only alpha02 and alpha04, running, then open the schedules tab and read evaluation schedule.',
 'changed':'Show only alpha02 and alpha04 in the apps tab with search alpha and running filter.',
 'paging':'On the paginated evaluation fixture show page 3 and item21 through item30.'}
def prepare():
 root=(ROOT/'data/benchmarks'/('extended-'+time.strftime('%Y%m%d-%H%M%S'))).resolve();root.mkdir();(root/'decisions').mkdir();(root/'source').mkdir()
 for name in ['index.html','app.js','styles.css']:(root/'source'/name).write_bytes((APP/name).read_bytes())
 (root/'AGENTS.md').write_text('Decision only. No tools, file access, commands, questions or write actions.\n',encoding='utf-8')
 cfg=default_config();cfg['approved_roots']=[];(root/'config.json').write_text(json.dumps(cfg),encoding='utf-8')
 manifest={'started':time.time(),'application':'Home App Status unchanged UI; synthetic API only','paging':'separate evaluation fixture, not a production feature',
 'goals':GOALS,'actions':ACTIONS,'normal_repetitions':10,'fault_repetitions':10,'limits':{'cli_calls':100,'input_tokens':2500000,'seconds':2700,'actions':8},
 'source_hashes':{n:hashlib.sha256((APP/n).read_bytes()).hexdigest() for n in ['index.html','app.js','styles.css']},
 'adoption':'zero out-of-scope execution, success not lower than both baselines; benefit including failures; no general-adoption claim from fixtures'}
 (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
 (root/'PLAN.md').write_text('''# 追加評価の事前計画\n実Home App Status UIを合成APIで隔離起動。検索＋状態＋タブの複合目標、変化した初期状態、別集計のページング評価画面。各10回×3方式=90通常ケース。\nアプリAPIの503・300ms遅延・DOM変更・不正候補・中止・途中再開を各10回×3方式で境界／実行層検証する。障害層は固定済み計画を再生し、モデル判断性能と混同しない。実モデルを使う障害回復も別に実行する。\n上限CLI100回・入力250万・45分・8操作。固定、有限計画の通常モデル、Ollama1操作＋通常モデルへの引継ぎ。失敗・中止を除外しない。\nCPUはprocess CPU seconds/working setを記録。GPU除外、電力は機器なしで未測定。長期は継続記録を用意し未経過期間を結果としない。保存／削除は事前許可・受入・復元計画のみ。実行しない。\n''',encoding='utf-8')
 return root
class Engine:
 def __init__(self,root):
  self.root=root;self.cancel=threading.Event();self.lock=threading.Lock();self.fault='';self.remaining=0
  hist=[json.loads(p.read_text(encoding='utf-8')) for p in (root/'decisions').glob('*.json')]
  self.calls=sum(r['arm']=='model' for r in hist);self.tokens=sum(r.get('normalized_usage',{}).get('input',r.get('usage',{}).get('inputTokens',0)) for r in hist);self.index=max((r['id'] for r in hist),default=0)
 def select(self,b):
  p=b['provider'];s=b['state'];ctx=None
  if p not in ('model','ollama') or s.get('goal') not in GOALS.values() or not set(s['choices'])<=set(ACTIONS) or len(json.dumps(s))>12000:raise ValueError('scope')
  limits=json.loads((self.root/'manifest.json').read_text(encoding='utf-8'))
  if self.calls>=100 or self.tokens>=2500000 or time.time()-limits['started']>2700:return {'arm':p,'actions':['STOP'],'error':'budget_limit'}
  self.cancel.clear();t=time.monotonic()
  with self.lock:self.index+=1;ident=self.index
  try:
   if p=='ollama':r=decide(self.root,p,s);r.pop('session_id',None);r['actions']=[r['action']]
   else:
    self.calls+=1;ctx=Context(self.root);original=ctx.check
    ctx.command=discover()['claude'];ctx.task={'id':'evaluation-decision','profile':{'model':'sonnet','effort':'medium'}}
    ctx.token=secrets.token_hex(24);ctx.endpoint='http://127.0.0.1:8794';ctx.approval_timeout=1
    ctx.agent_definition=SimpleNamespace(name='evaluation-decider',description='Read-only safe action selection',tools=(),sandbox='read-only')
    ctx.agent_run={'id':str(uuid.uuid4())}
    def agent_started(session,*args):ctx.agent_run['session_id']=session
    ctx.agent_started=agent_started
    def check():
     original()
     if self.cancel.is_set():raise Cancelled('cancelled')
    ctx.check=check
    schema={'type':'object','additionalProperties':False,'properties':{'actions':{'type':'array','items':{'type':'string','enum':list(ACTIONS)},'minItems':1,'maxItems':8}},'required':['actions']}
    payload=dict(s);payload['registered_actions']=ACTIONS;payload['instruction']='Return a short finite sequence; use only visible safe actions. Later actions may become available. Do not refresh without error. STOP if uncertain.'
    value=ClaudeAdapter().run(ctx,json.dumps(payload,ensure_ascii=False),schema)
    r={'arm':p,'actions':value['actions'],'usage':ctx.usage,'denied_requests':ctx.denied_requests}
   if self.cancel.is_set():r.update(actions=['STOP'],error='Cancelled')
  except Exception as e:
   message=str(e)
   category=next((word for word in ['sandbox','auth','login','expired','unsupported','not found','model','schema','instructions'] if word in message.lower()),'other')
   r={'arm':p,'actions':['STOP'],'error':type(e).__name__,'diagnostic_category':category,'usage':ctx.usage if ctx else {}}
   # Native error details stay local and are bounded; redact common credential forms.
   import re
   safe=re.sub(r'(?:sk-[\w-]+|Bearer\s+\S+|https?://\S+)','[REDACTED]',message)
   (self.root/'diagnostic.txt').write_text(safe[:1200],encoding='utf-8')
  if p=='model':
   u=r.get('usage',{});r['normal_model']='Claude/sonnet/medium';r['normalized_usage']={'input':u.get('input_tokens',0)+u.get('cache_read_input_tokens',0)+u.get('cache_creation_input_tokens',0),'cached':u.get('cache_read_input_tokens',0),'cache_write':u.get('cache_creation_input_tokens',0),'output':u.get('output_tokens',0)}
  r.update(id=ident,latency_ms=round((time.monotonic()-t)*1000));self.tokens+=r.get('normalized_usage',{}).get('input',0)
  (self.root/'decisions'/f'{ident:04}.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');return r
def fixture():
 apps=[{'id':f'item{i:02}','name':f'alpha{i:02}' if i<6 else f'app{i:02}','category':'evaluation','description':'synthetic',
 'path':'<PROJECT_ROOT>','version':{'value':'1.0'},'health':{'status':'running' if i%2==0 else 'stopped','label':'起動中' if i%2==0 else '停止'},'restart':{'available':False}} for i in range(1,21)]
 return {'apps':apps,'summary':{'total':20,'running':10,'stopped':10,'attention':0},'schedules':{'items':[{'name':'evaluation schedule','status':'registered','lastResult':0}]},'podcasts':{},'generatedAt':'2026-10-02T00:00:00Z'}
PAGING='''<!doctype html><html lang="ja"><meta charset="utf-8"><title>ページング評価画面</title><h1>ページング評価画面</h1><p>実アプリとは別の合成評価</p><button id="prev">前のページ</button><strong id="page"></strong><button id="next">次のページ</button><ul id="items"></ul><p id="error" role="alert"></p><script>let page=1;async function render(){try{const r=await fetch('/api/page?page='+page);if(!r.ok)throw Error('HTTP '+r.status);const d=await r.json();document.querySelector('#page').textContent='ページ '+d.page;document.querySelector('#items').replaceChildren(...d.items.map(t=>{const e=document.createElement('li');e.textContent=t;return e}));document.querySelector('#prev').disabled=page===1;document.querySelector('#next').disabled=page===4;document.querySelector('#error').textContent=''}catch(e){document.querySelector('#error').textContent=e.message}}document.querySelector('#next').onclick=()=>{page=Math.min(4,page+1);render()};document.querySelector('#prev').onclick=()=>{page=Math.max(1,page-1);render()};render()</script></html>'''
def serve(root):
 engine=Engine(root);cookie=secrets.token_hex(24)
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def send(self,status,value,ctype='application/json'):
   raw=value.encode() if isinstance(value,str) else json.dumps(value,ensure_ascii=False).encode()
   self.send_response(status);self.send_header('Content-Type',ctype+'; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Set-Cookie','evaluation='+cookie+'; HttpOnly; SameSite=Strict');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
  def do_GET(self):
   path=urlparse(self.path).path
   if path in ('/api/status','/api/page') and engine.remaining>0:
    engine.remaining-=1
    if engine.fault=='disconnect':return self.send(503,{'error':'injected application outage'})
    if engine.fault=='delay':time.sleep(.3)
   if path=='/health':return self.send(200,{'app':'isolated-extended-evaluation','calls':engine.calls,'tokens':engine.tokens})
   if path=='/api/status':return self.send(200,fixture())
   if path=='/api/page':
    from urllib.parse import parse_qs
    page=int(parse_qs(urlparse(self.path).query).get('page',['1'])[0]);page=max(1,min(4,page));return self.send(200,{'page':page,'items':[f'item{i}' for i in range((page-1)*10+1,page*10+1)]})
   if path=='/paging':return self.send(200,PAGING,'text/html')
   name={'/':'index.html','/app.js':'app.js','/styles.css':'styles.css'}.get(path)
   if name:
    text=(root/'source'/name).read_text(encoding='utf-8')
    if name=='index.html' and engine.fault=='dom':text=text.replace('id="search-input"','id="search-input" aria-label="アプリ検索（改訂）"').replace('>起動中</button>','>稼働中</button>')
    return self.send(200,text,'text/html' if name.endswith('html') else 'text/javascript' if name.endswith('js') else 'text/css')
   return self.send(404,{'error':'not found'})
  def do_POST(self):
   if self.headers.get('Host')!='127.0.0.1:8794' or cookie not in self.headers.get('Cookie','') or self.headers.get('X-Evaluation')!='1' or self.headers.get('Origin','http://127.0.0.1:8794')!='http://127.0.0.1:8794':return self.send(403,{'error':'scope'})
   path=urlparse(self.path).path
   if path not in ['/evaluation/decide','/evaluation/control','/evaluation/cancel']:return self.send(403,{'error':'all application writes blocked'})
   size=int(self.headers.get('Content-Length',0))
   if not 0<size<=16000:return self.send(400,{'error':'size'})
   try:
    b=json.loads(self.rfile.read(size))
    if path.endswith('cancel'):engine.cancel.set();return self.send(200,{'cancelled':True})
    if path.endswith('control'):
     if b.get('fault') not in ['','disconnect','delay','dom']:raise ValueError('invalid fault')
     engine.fault=b['fault'];engine.remaining=1;return self.send(200,{'set':engine.fault})
    return self.send(200,engine.select(b))
   except (ValueError,KeyError,TypeError):return self.send(400,{'error':'invalid request'})
 server=ThreadingHTTPServer(('127.0.0.1',8794),Handler);print('Isolated HomeAppStatus evaluation: 8794',flush=True)
 try:server.serve_forever()
 finally:engine.cancel.set();server.server_close()
def request(root,endpoint,body):
 client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()));client.open('http://127.0.0.1:8794/health').close()
 req=urllib.request.Request('http://127.0.0.1:8794/evaluation/'+endpoint,data=json.dumps(body).encode(),headers={'X-Evaluation':'1','Content-Type':'application/json'})
 with client.open(req,timeout=110) as r:value=json.load(r)
 (root/'response.json').write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');print(json.dumps({'actions':value.get('actions'),'error':value.get('error'),'set':value.get('set')}))
def cancel_probe(root,provider):
 client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()));client.open('http://127.0.0.1:8794/health').close()
 state=json.loads((root/'state.json').read_text(encoding='utf-8'));result={};started=time.monotonic()
 def post(endpoint,body):
  req=urllib.request.Request('http://127.0.0.1:8794/evaluation/'+endpoint,data=json.dumps(body).encode(),headers={'X-Evaluation':'1','Content-Type':'application/json'})
  with client.open(req,timeout=110) as r:return json.load(r)
 def pending():result.update(post('decide',{'provider':provider,'state':state}))
 worker=threading.Thread(target=pending);worker.start();time.sleep(.03);signal=time.monotonic();post('cancel',{});worker.join(110)
 result['cancel_signal_ms']=round((signal-started)*1000);result['cancel_response_ms']=round((time.monotonic()-signal)*1000);result['cancel_raced']=result.get('error')!='Cancelled'
 (root/'cancel-response.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8');print(json.dumps({'error':result.get('error'),'cancel_response_ms':result['cancel_response_ms'],'raced':result['cancel_raced']}))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','serve','decide','control','cancel','cancel-probe']);p.add_argument('--root',type=Path);p.add_argument('--provider');p.add_argument('--fault',default='');a=p.parse_args()
 if a.mode=='prepare':print(prepare())
 elif a.mode=='serve':serve(a.root.resolve())
 elif a.mode=='decide':request(a.root,'decide',{'provider':a.provider,'state':json.loads((a.root/'state.json').read_text(encoding='utf-8'))})
 elif a.mode=='cancel-probe':cancel_probe(a.root,a.provider)
 else:request(a.root,a.mode,{'fault':a.fault})
