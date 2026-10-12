"""Bounded semantic decision benchmark. No browser or production writes."""
import datetime,json,pathlib,time,statistics,html
import extended_evaluation as ev
ROOT=ev.ROOT/'data/benchmarks'/('final-provider-'+time.strftime('%Y%m%d-%H%M%S'))
ROOT.mkdir();(ROOT/'decisions').mkdir()
(ROOT/'AGENTS.md').write_text('Decision-only isolated evaluation. No tools or external actions.\n',encoding='utf-8')
(ROOT/'config.json').write_text(json.dumps(ev.default_config()),encoding='utf-8')
started=time.time();manifest={'started':started,'limits':{'cli_calls':100,'input_tokens':250000,'seconds':1800,'actions':8},'scope':'synthetic semantic decisions only; no real application execution','normal_model':'Claude/sonnet/medium','local_model':'tev1:0.8b','repetitions':10,'criteria':'all cases including failures; no claim of broad application superiority'}
(ROOT/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
engine=ev.Engine(ROOT); rows=[]
def fixture(category,rep):
    target=2+rep%3
    state={'query':'','filter':'all','tab':'apps','page':1,'error':'','target_page':target}
    goal={'simple':'Select the running filter. Do not change search or tab.', 'compound':'Search alpha, select the running filter, then open schedules.', 'changed':'Return to apps, search alpha and select running, regardless of the current state.', 'paging':f'Reach page {target}; do not change the tab or filter.', 'uncertain':'The requested target record is missing from the supplied state. Do not guess or modify anything. Stop.'}[category]
    if category=='changed':state.update(query='app',filter='stopped',tab='schedules')
    ev.GOALS[category]=goal
    return state,goal
def complete(cat,s):
    return {'simple':s['filter']=='running','compound':s['query']=='alpha' and s['filter']=='running' and s['tab']=='schedules','changed':s['query']=='alpha' and s['filter']=='running' and s['tab']=='apps','paging':s['page']==s['target_page'],'uncertain':False}[cat]
def choices(cat,s):
    if cat=='uncertain':return {'STOP':'Insufficient evidence; stop without guessing'}
    if cat=='paging':return {'NEXT':'Advance one page','PREVIOUS':'Go back one page','STOP':'Stop'}
    if s['tab']!='apps':return {'APPS':'Open apps','STOP':'Stop'}
    return {k:ev.ACTIONS[k] for k in ['SEARCH','RUNNING','SCHEDULES','STOP']}
def apply(s,a):
    if a=='SEARCH':s['query']='alpha'
    elif a=='RUNNING':s['filter']='running'
    elif a=='APPS':s['tab']='apps'
    elif a=='SCHEDULES':s['tab']='schedules'
    elif a=='NEXT':s['page']=min(4,s['page']+1)
    elif a=='PREVIOUS':s['page']=max(1,s['page']-1)
def fixed(cat,s):
    if cat=='uncertain':return 'STOP'
    if cat=='paging':return 'NEXT' if s['page']<s['target_page'] else 'PREVIOUS'
    if cat=='changed' and s['tab']!='apps':return 'APPS'
    if cat=='simple':return 'RUNNING'
    if s['query']!='alpha':return 'SEARCH'
    if s['filter']!='running':return 'RUNNING'
    return 'SCHEDULES'
for rep in range(1,11):
 for cat in ['simple','compound','changed','paging','uncertain']:
  for mode in (['fixed','model','hybrid'] if rep%2 else ['hybrid','model','fixed']):
   state,goal=fixture(cat,rep); t=time.monotonic();r={'category':cat,'rep':rep,'mode':mode,'requests':[],'steps':[],'fallbacks':0,'status':'failed','out_of_scope':0}; plan=[];seen=set();provider='ollama' if mode=='hybrid' else 'model'
   for step in range(8):
    if complete(cat,state):r['status']='passed';break
    candidates=choices(cat,state); sig=json.dumps(state,sort_keys=True)
    if mode=='hybrid' and provider=='ollama' and sig in seen:provider='model';r['fallbacks']+=1
    seen.add(sig)
    if mode=='fixed':action=fixed(cat,state)
    else:
     if not plan:
      if time.time()-started>1800 or engine.calls>=100 or engine.tokens>=250000:r['status']='budget_limit';break
      result=engine.select({'provider':provider,'state':{'goal':goal,'visible':state,'choices':candidates}});r['requests'].append(result)
      if result.get('error'):
       if mode=='hybrid' and provider=='ollama':provider='model';r['fallbacks']+=1;continue
       r['status']='provider_error';break
      plan=list(result['actions'])
     action=plan.pop(0)
    if action not in candidates:
     plan=[]
     if mode=='hybrid' and provider=='ollama':provider='model';r['fallbacks']+=1;continue
     r['status']='unavailable_action';break
    r['steps'].append(action)
    if action=='STOP':r['status']='passed' if cat=='uncertain' else 'stopped';break
    apply(state,action)
   if complete(cat,state):r['status']='passed'
   r['seconds']=round(time.monotonic()-t,3);rows.append(r);(ROOT/'cases.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
  print(json.dumps({'rep':rep,'category':cat,'completed':len(rows),'cli_calls':engine.calls,'input':engine.tokens}),flush=True)
summary={}
for cat in ['simple','compound','changed','paging','uncertain']:
 summary[cat]={}
 for mode in ['fixed','model','hybrid']:
  cases=[r for r in rows if r['category']==cat and r['mode']==mode];qs=[q for r in cases for q in r['requests']];native=[q for q in qs if q['arm']=='model'];local=[q for q in qs if q['arm']=='ollama']
  summary[cat][mode]={'success':sum(r['status']=='passed' for r in cases),'cases':len(cases),'seconds':round(sum(r['seconds'] for r in cases),3),'cli_calls':len(native),'local_calls':len(local),'input':sum(q.get('normalized_usage',{}).get('input',0) for q in native),'cached':sum(q.get('normalized_usage',{}).get('cached',0) for q in native),'output':sum(q.get('normalized_usage',{}).get('output',0) for q in native),'fallbacks':sum(r['fallbacks'] for r in cases)}
(ROOT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
table=''.join('<tr><td>'+cat+'</td><td>'+mode+'</td>'+''.join(f'<td>{v[k]}</td>' for k in ['success','cases','seconds','cli_calls','local_calls','input','fallbacks'])+'</tr>' for cat,ms in summary.items() for mode,v in ms.items())
report='<!doctype html><html lang="ja"><meta charset="utf-8"><title>采来 適用範囲の最終追加測定</title><style>body{max-width:1100px;margin:40px auto;font-family:system-ui;color:#183535}table{border-collapse:collapse}td,th{padding:9px;border:1px solid #bbb}</style><h1>判断プロバイダー：適用範囲の追加測定</h1><p>2026年10月2日。5分類×10反復×3方式。ClaudeとOllamaへの実通信、操作は合成状態機械。実アプリの新たな操作試験ではありません。</p><p>順序を反復ごとに交互にし、初期状態・候補・受入条件を共通化。失敗と引き継ぎも時間・利用量へ含めます。固定手順は目標を実装した参照方式で、汎用AIではありません。</p><table><tr><th>分類</th><th>方式</th><th>成功</th><th>件数</th><th>総秒</th><th>CLI呼出</th><th>ローカル呼出</th><th>CLI入力</th><th>引き継ぎ</th></tr>'+table+'</table><h2>解釈の限界</h2><p>分類別に成功率が同等の場合だけ速度・利用量を比較してください。合成ケースの結果を未知の実アプリへ一般化できません。キャッシュ読取を含む入力であり、料金・サブスクリプション利用枠の削減率ではありません。CPU/GPU/電力・長期観測・GUI自由操作はこの測定の対象外です。今回で評価系列を一区切りにできますが、汎用優位性の保証にはなりません。</p></html>'
out=ev.ROOT/'docs/reports/final-provider-evaluation-report.html';out.write_text(report,encoding='utf-8');print(json.dumps({'root':str(ROOT),'summary':summary}),flush=True)
