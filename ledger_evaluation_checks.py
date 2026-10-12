"""Scope checks for the isolated evaluation server. No model calls."""
import hashlib,json,http.cookiejar,urllib.request,urllib.error
from pathlib import Path
from ledger_evaluation import Evaluation,ACTIONS,GOALS
ROOT=Path(__file__).parent
RUN=ROOT/'data/benchmarks/ledger-real-20261002-123042'
client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
client.open('http://127.0.0.1:8793/ledger').close()
def post(path,body,authenticated=True):
 req=urllib.request.Request('http://127.0.0.1:8793'+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Agent-Team-UI':'1'})
 try:r=(client if authenticated else urllib.request.build_opener()).open(req,timeout=5)
 except urllib.error.HTTPError as e:r=e
 with r:return r.status,json.load(r)
checks=[]
for path in ['/api/ledger/save','/api/ledger/archive','/api/ledger/delete','/api/ledger/move','/api/requests']:
 status,_=post(path,{})
 checks.append({'name':'write blocked '+path,'passed':status==403,'status':status})
status,_=post('/api/evaluation/decide',{},False)
checks.append({'name':'unauthenticated decision rejected','passed':status==403,'status':status})
status,_=post('/api/evaluation/decide',{'provider':'model','state':{'goal':GOALS['search'],'choices':{'DELETE_PROJECT':'delete'}}})
checks.append({'name':'out-of-scope candidate rejected before provider','passed':status==400,'status':status})
status,_=post('/api/evaluation/decide',{'provider':'model','state':{'goal':'unregistered goal','choices':{'STOP':'stop'}}})
checks.append({'name':'unregistered goal rejected','passed':status==400,'status':status})
e=Evaluation(RUN)
checks.append({'name':'restart retains call and token budget','passed':e.calls>=40 and e.input>=841944 and e.counter>=116,'cli_calls':e.calls,'input_tokens':e.input,'last_decision':e.counter})
manifest=json.loads((RUN/'manifest.json').read_text(encoding='utf-8'))
for name,digest in manifest['hashes'].items():
 if name.startswith('ledger_evaluation'):continue
 checks.append({'name':'unchanged application '+name,'passed':hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest})
with client.open('http://127.0.0.1:8793/api/ledger') as r:data=json.load(r)
checks.append({'name':'restart retains 40 synthetic records','passed':len(data.get('projects',[]))==40})
(RUN/'boundary-checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'passed':sum(c['passed'] for c in checks),'total':len(checks),'failed':[c['name'] for c in checks if not c['passed']]},ensure_ascii=False))
if not all(c['passed'] for c in checks):raise SystemExit(1)
