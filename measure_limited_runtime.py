"""Native calls through deployed intake/UI controllers; isolated synthetic executor."""
import hashlib,json,pathlib,time,statistics
from team_config import ROOT
from team_ui_automation import UiAutomation
from team_ui_actions import decide
from team_limited_routing import classify_intake

run=ROOT/'data/benchmarks'/('limited-runtime-'+time.strftime('%Y%m%d-%H%M%S'));run.mkdir()
started=time.monotonic();budget={'calls':54,'input':0};cases=[];intake=[]
manifest={'scope':'production Python controllers; synthetic executor matching operation-tests.js; browser workflow separately unverified','repetitions':3,'limits':{'cli_calls':80,'input':200000,'seconds':1500},'normal_model':'Claude/sonnet/medium','source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in ['team_ui_automation.py','team_ui_actions.py','team_limited_routing.py','web/operation-tests.js']}}
(run/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
def selector(root,provider,state):
 if time.monotonic()-started>1500 or budget['calls']>=80 or budget['input']>=200000:raise RuntimeError('budget_limit')
 if provider=='model':budget['calls']+=1
 r=decide(root,provider,state)
 if provider=='model':budget['input']+=r.get('usage',{}).get('input_tokens',r.get('usage',{}).get('inputTokens',0))
 return r
controller=UiAutomation(run,selector=selector)
def fixture():
 return {'query':'','status':'','page':1,'message':'','rows':[], 'nextEnabled':True},[{'id':i,'title':('alpha' if i in [21,22,23,31] else 'task')+str(i),'status':'done' if i==23 else 'todo'} for i in range(1,42)]
def render(s,allrows,q,status):
 filtered=[r for r in allrows if q in r['title'] and (not status or r['status']==status)]
 s['rows']=[dict(r) for r in filtered[(s['page']-1)*20:s['page']*20]];s['nextEnabled']=s['page']*20<len(filtered);s['message']='' if filtered else '該当するタスクはありません。'
for rep in range(1,3):
 for goal in ['search','page','update','empty']:
  if rep == 2 and goal not in ('search','page'):continue
  for mode in (['model','hybrid'] if rep%2 else ['hybrid','model']):
   s,allrows=fixture();q=status='';render(s,allrows,q,status);t=time.monotonic()
   session=controller.start({'mode':mode,'goal':goal,'consent':True,'normal_adapter':'claude'});ident=session['id'];revision=0
   for attempt in range(14):
    result=controller.next({'id':ident,'revision':revision,'visible':s})
    if result['status']=='retry':continue
    if result['status']!='action':break
    a=result['action'];revision=result['revision']
    if a=='FILL_QUERY':s['query']='zzznomatch' if goal=='empty' else 'alpha'
    elif a=='FILTER_TODO':s['status']='todo'
    elif a=='SEARCH':q,status=s['query'],s['status'];s['page']=1;render(s,allrows,q,status)
    elif a=='NEXT':s['page']+=1;render(s,allrows,q,status)
    elif a=='UPDATE_21':
     allrows[20]['status']='done'
     for row in s['rows']:
      if row['id']==21:row['status']='done'
    else:raise RuntimeError('unregistered executor action')
   if controller.sessions[ident]['status']=='running':controller.cancel({'id':ident})
   record=controller.sessions[ident];record['elapsed_seconds']=round(time.monotonic()-t,3);record['repetition']=rep
   cases.append(record);(run/'cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2),encoding='utf-8')
   print(json.dumps({'rep':rep,'goal':goal,'mode':mode,'status':record['status'],'seconds':record['elapsed_seconds'],'steps':len(record['steps']),'budget':budget}),flush=True)
examples=[('research','調査','既存ソースを調査してください'),('implementation','実装','検索機能を追加してください'),('review','レビュー','変更差分をレビューしてください'),('normal','複合依頼','調査して修正してレビューしてください'),('human','重要操作','プロジェクトを削除してください')]
for rep in range(1,4):
 for expected,title,goal in examples:
  for enabled in [False,True]:
   t=time.monotonic();value=classify_intake(title,goal,{'provider':'ollama' if enabled else 'disabled','model':'tev1:0.8b'})
   intake.append({'repetition':rep,'enabled':enabled,'expected':expected,'seconds':round(time.monotonic()-t,3),'result':value,'exact':value['classification']==expected,'permission_safe':value['permission_granted'] is False})
   (run/'intake.json').write_text(json.dumps(intake,ensure_ascii=False,indent=2),encoding='utf-8')
  print(json.dumps({'intake_rep':rep,'category':expected}),flush=True)
summary={}
for goal in ['search','page','update','empty']:
 summary[goal]={}
 for mode in ['model','hybrid']:
  rs=[r for r in cases if r['name']==goal and r['arm']==mode];qs=[q for r in rs for q in [*[step['decision'] for step in r['steps']],*r['decisionFailures'],*([r['error']] if r.get('error') else [])]]
  native=[q for q in qs if q.get('arm')=='model'];local=[q for q in qs if q.get('arm')=='ollama']
  summary[goal][mode]={'cases':len(rs),'success':sum(r['status']=='passed' for r in rs),'seconds':round(sum(r['elapsed_seconds'] for r in rs),3),'cli_calls':len(native),'local_calls':len(local),'input':sum(q.get('usage',{}).get('input_tokens',0) for q in native),'cached':sum(q.get('usage',{}).get('cached_input_tokens',0) for q in native),'output':sum(q.get('usage',{}).get('output_tokens',0) for q in native),'switches':sum(len(r['switches']) for r in rs),'errors':sum(bool(q.get('error')) for q in qs)}
data={'operation':summary,'intake':{'cases':len(intake),'enabled_exact':sum(r['enabled'] and r['exact'] for r in intake),'enabled_cases':sum(r['enabled'] for r in intake),'enabled_local_calls':sum(r['enabled'] and r['result']['provider']=='ollama' for r in intake),'enabled_seconds':round(sum(r['seconds'] for r in intake if r['enabled']),3),'disabled_seconds':round(sum(r['seconds'] for r in intake if not r['enabled']),3),'all_permissions_safe':all(r['permission_safe'] for r in intake)},'budget':budget}
(run/'summary.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'root':str(run),'summary':data}),flush=True)
