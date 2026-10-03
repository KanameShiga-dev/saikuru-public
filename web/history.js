'use strict';
// プロジェクト履歴（2026-10-03 UI改善：一覧と詳細の2列、集計、目盛り付きの時系列、承認とイベントの統合表示）
// データは /api/project-history の読み取りのみ。経緯: docs/UI_REDESIGN_20261003.md
const $=id=>document.getElementById(id);
const statuses={planning:'計画中',queued:'待機',running:'実行中',awaiting_approval:'確認・承認待ち',blocked:'判断待ち',failed:'失敗',interrupted:'中断',succeeded:'担当工程終了',completed:'完了',cancelled:'中止',handed_off:'引き継ぎ済み',
  awaiting_acceptance:'成果の確認待ち',accepted:'成果受け入れ済み',accepted_with_pending_checks:'成果受領・未完了項目あり'};
const tone={running:'run',awaiting_approval:'ask',blocked:'ask',awaiting_acceptance:'ask',failed:'stop',interrupted:'stop',cancelled:'muted',queued:'muted',planning:'run',
  succeeded:'done',completed:'done',handed_off:'done',accepted:'done',accepted_with_pending_checks:'done'};
const icon={run:'▶',ask:'！',stop:'×',muted:'◷',done:'✓'};
const origins={ledger:'台帳からの依頼',new:'新規依頼',unrecorded:'入口未記録'};
const roles={planner:'計画',researcher:'調査',builder:'実装',reviewer:'レビュー'};
const approvalKinds={plan:'計画の承認',tool:'操作の承認',question:'質問への回答',completion:'成果の確認',context_conflict:'情報の矛盾の確認',scope_transfer:'範囲の引き継ぎ'};
const approvalStates={pending:'未判断',approved:'承認',denied:'却下',expired:'期限切れ',superseded:'置き換え',cancelled:'取消'};
const feedTypes=[['approval','確認・承認'],['step','工程の進行'],['stop','再試行・停止'],['other','その他']];
const eventKind=t=>/approv|scope_transferred|benchmark_permission/.test(t)?'approval':/^(retry|repair_limit|task_error|cancelled|scheduler_error|recovery_advice|conflict_)/.test(t)?'stop':/^(job_created|task_|progress|review_followup|manual_review_repair|model_|shared_document_read|handoff_fact)/.test(t)?'step':'other';
let state={jobs:[],tasks:[]},projectNames=new Map(),selected='',detailData=null,cursor=null,version=0,loading=false;
const shownTypes=new Set(feedTypes.map(t=>t[0]));
const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined&&text!==null)e.textContent=text;if(cls)e.className=cls;return e;};
const date=t=>t?new Date(t*1000).toLocaleString('ja-JP'):'未記録';
const hm=t=>new Date(t*1000).toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit'});
const md=t=>new Date(t*1000).toLocaleDateString('ja-JP',{month:'numeric',day:'numeric'});
const key=p=>String(p||'').replaceAll('\\','/').replace(/\/$/,'').toLowerCase();
const projectName=p=>projectNames.get(key(p))||String(p||'').replaceAll('\\','/').split('/').filter(Boolean).pop()||'名称未登録';
const dur=s=>{if(s===null||s===undefined||s<0)return '未記録';s=Math.floor(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h}時間${m}分`:m?`${m}分${s%60}秒`:`${s%60}秒`;};
const paths=j=>[j.project,j.document_source,j.origin.project].filter(Boolean);
const now=()=>Date.now()/1000;
const active=t=>['running','awaiting_approval'].includes(t.status);
const taskEnd=t=>t.finished_at||(active(t)&&t.started_at?now():null);
const chip=(status,label)=>{const k=tone[status]||'muted';return node('span',`${icon[k]} ${label||statuses[status]||status}`,'state s-'+k);};
async function api(query=''){const r=await fetch('/api/project-history'+query);const d=await r.json();if(!r.ok)throw Error(d.error||'履歴を取得できません。');return d;}
const tokenText=v=>typeof v==='number'?v.toLocaleString('ja-JP'):'未取得';
function usageText(c){return '入力 '+tokenText(c?.input)+' / キャッシュ読取 '+tokenText(c?.cached)+' / キャッシュ作成 '+tokenText(c?.cache_write)+' / 出力 '+tokenText(c?.output);}
function quotaText(q){if(!q)return '未取得';const rows=(q.rows||[]).map(r=>r.label+'：短期 '+(typeof r.short?.remaining_percent==='number'?r.short.remaining_percent+'%':'未取得')+' / 週間 '+(typeof r.weekly?.remaining_percent==='number'?r.weekly.remaining_percent+'%':'未取得'));return (q.stale||q.status!=='ok'?'参考値・古い／取得失敗：':'残量：')+(rows.join('、')||'未取得')+'（取得 '+date(q.updated_at)+'）';}
function filtered(){
  const p=$('project').value,o=$('origin').value,q=$('search').value.toLowerCase();
  return state.jobs.filter(j=>(!p||paths(j).some(x=>key(x)===p))&&(!o||j.origin.kind===o)&&j.title.toLowerCase().includes(q)).sort((a,b)=>b.created_at-a.created_at);
}
function renderList(){
  const list=filtered();
  $('status').textContent=list.length+'件の依頼。入口が記録されていない過去の依頼は新規依頼と断定しません。';
  $('jobs').replaceChildren(...list.map(j=>{
    const tasks=state.tasks.filter(t=>t.job_id===j.id);
    const b=node('button',undefined,'job');b.type='button';b.setAttribute('aria-current',String(j.id===selected));
    const top=node('div',undefined,'chips');top.append(chip(j.status),node('span',origins[j.origin.kind]||'入口未記録','tag'));
    b.append(top,node('h2',j.title),node('div',`${projectName(j.document_source||j.origin.project||j.project)}・受付 ${md(j.created_at)} ${hm(j.created_at)}・工程 ${tasks.length}件`,'sub'));
    b.onclick=()=>{selected=j.id;renderList();detail(j.id,true);};return b;
  }));
  if(!list.length)$('jobs').append(node('p','条件に一致する依頼がありません。','hint'));
}
function stats(tasks,approvals){
  const run=tasks.reduce((a,t)=>{const e=taskEnd(t);return a+(t.started_at&&e?e-t.started_at:0);},0);
  const retries=tasks.reduce((a,t)=>a+Math.max(0,(t.attempt||1)-1),0);
  const box=node('div',undefined,'stats');
  for(const [l,v] of [['工程',tasks.length+'件'],['実行時間の合計',dur(run)],['再試行',retries+'回'],['確認・承認',approvals.length+'件']]){const c=node('div');c.append(node('div',l,'l'),node('div',v,'v'));box.append(c);}
  return box;
}
function gantt(job,tasks){
  const wrap=node('div',undefined,'gantt');
  if(!tasks.length){wrap.append(node('p','作業記録はありません。','hint'));return wrap;}
  const times=[job.created_at,...tasks.flatMap(t=>[t.created_at,t.started_at,taskEnd(t)])].filter(Boolean);
  const min=Math.min(...times),max=Math.max(min+60,...times),span=max-min,multiDay=md(min)!==md(max);
  wrap.setAttribute('role','img');wrap.setAttribute('aria-label','工程の時系列。'+tasks.map(t=>t.title+'：'+(statuses[t.status]||t.status)).join('、'));
  const axis=node('div',undefined,'axis'),ticks=node('div',undefined,'ticks');axis.append(node('span'),ticks);
  for(const r of [0,.25,.5,.75,1]){const t=min+r*span,s=node('span',(multiDay?md(t)+' ':'')+hm(t));s.style.left=(r*100)+'%';ticks.append(s);}
  wrap.append(axis);
  for(const t of tasks){
    const lane=node('div',undefined,'lane'),name=node('div',undefined,'name'),p=t.profile||{},end=taskEnd(t);
    name.append(node('strong',t.title),node('small',[roles[t.role]||t.role,[p.adapter,p.model].filter(Boolean).join(' '),'試行'+(t.attempt||1)].filter(Boolean).join('・')),
      node('small',`${t.started_at?date(t.started_at):'開始前'} 〜 ${t.finished_at?hm(t.finished_at):active(t)?'実行中':'未記録'}（${active(t)?'経過 ':''}${t.started_at&&end?dur(end-t.started_at):'未記録'}）`),
      node('small','トークン：'+usageText(t.consumption)));
    const track=node('div',undefined,'track'),start=t.started_at||t.created_at,stop=end||start;
    const left=Math.min(96,Math.max(0,(start-min)/span*100)),width=Math.min(100-left,Math.max(3,(stop-start)/span*100));
    const k=tone[t.status]||'muted',label=`${icon[k]} ${statuses[t.status]||t.status}${t.started_at&&end?'・'+dur(end-t.started_at):''}`;
    const bar=node('span',width<30?'':label,'bar b-'+k);bar.style.left=left+'%';bar.style.width=width+'%';bar.title='開始: '+date(t.started_at)+' / 終了: '+date(t.finished_at)+(t.summary?'\n'+t.summary:'');
    track.append(bar);
    if(width<30){const out=node('span',label,'outlabel o-'+k);if(left+width>55)out.style.right=`calc(${100-left}% + 6px)`;else out.style.left=`calc(${left+width}% + 6px)`;track.append(out);}
    lane.append(name,track);wrap.append(lane);
  }
  const legend=node('div',undefined,'legend');
  for(const [k,l] of [['done','担当工程終了'],['run','実行中（ページ更新時点まで）'],['ask','判断・確認待ち'],['stop','失敗・中断']]){const s=node('span',l);s.prepend(node('i',undefined,'sw b-'+k));legend.append(s);}
  legend.append(node('span','各工程は最新の試行の開始〜終了'));wrap.append(legend);
  return wrap;
}
function feedItems(d){
  const items=d.approvals.flatMap(a=>{
    const label=approvalKinds[a.kind]||a.kind,out=[{at:a.created_at,kind:'approval',text:label+'を求めました（'+(approvalStates[a.status]||a.status)+'）',note:''}];
    if(a.decided_at)out.push({at:a.decided_at,kind:'approval',text:label+'：'+(approvalStates[a.status]||a.status),note:(a.note?'メモ「'+a.note+'」／':'')+'判断者：未記録'});
    return out;
  });
  for(const e of d.events)items.push({at:e.at,kind:eventKind(e.type||''),text:e.message,note:e.type});
  return items.sort((a,b)=>(b.at||0)-(a.at||0));
}
function drawFeed(){
  const list=$('feed');if(!list||!detailData)return;
  const items=feedItems(detailData).filter(i=>shownTypes.has(i.kind));
  list.replaceChildren(...items.map(i=>{const li=node('li',undefined,'k-'+i.kind),t=node('time',date(i.at));li.append(t,node('span',i.text));if(i.note)li.append(node('small',i.note,'note'));return li;}));
  if(!items.length)list.append(node('li','選んだ種類の記録はありません。'));
}
async function detail(id,scroll){
  const token=++version;cursor=null;detailData=null;
  $('detail').replaceChildren(node('p','記録を取得しています…','hint'));
  try{
    const d=await api('?job_id='+encodeURIComponent(id));if(token!==version)return;
    const j=d.jobs.find(x=>x.id===id),tasks=d.tasks.filter(t=>t.job_id===id).sort((a,b)=>a.created_at-b.created_at);
    detailData={approvals:d.approvals,events:d.events};cursor=d.next_before;
    const head=node('div',undefined,'chips');head.append(chip(j.status),node('span',origins[j.origin.kind]||'入口未記録','tag'));
    const sum={input:null,cached:null,cache_write:null,output:null,measured_runs:0,expected_runs:0};
    for(const t of tasks){const c=t.consumption||{};for(const k of ['input','cached','cache_write','output'])if(typeof c[k]==='number')sum[k]=(sum[k]??0)+c[k];sum.measured_runs+=c.measured_runs||0;sum.expected_runs+=c.expected_runs||0;}
    const usage=node('details',undefined,'usage');usage.append(node('summary','消費トークンと利用枠（取得済み分）'),node('p','依頼合計：'+usageText(sum)),
      node('p','トークン記録あり '+sum.measured_runs+' / 試行 '+sum.expected_runs+'。未取得項目は合計に含まれません。入力とキャッシュの定義はCLIごとに異なるため単純加算しません。','hint'));
    for(const t of tasks){const c=t.consumption||{},q=node('div',undefined,'quota-row');q.append(node('strong',t.title+' — 利用枠（最新試行）'),node('p','開始前：'+quotaText(c.quota_before)),node('p','終了後：'+quotaText(c.quota_after)));usage.append(q);}
    const types=node('div',undefined,'types');types.setAttribute('role','group');types.setAttribute('aria-label','表示する記録の種類');
    for(const [k,l] of feedTypes){const lab=node('label'),cb=node('input');cb.type='checkbox';cb.checked=shownTypes.has(k);cb.onchange=()=>{cb.checked?shownTypes.add(k):shownTypes.delete(k);drawFeed();};lab.append(cb,document.createTextNode(l));types.append(lab);}
    const older=node('button','過去の記録をさらに表示','older');older.type='button';older.hidden=!cursor;
    older.onclick=async()=>{older.disabled=true;try{const more=await api('?job_id='+encodeURIComponent(id)+'&before='+cursor);if(token!==version)return;detailData.events.push(...more.events);cursor=more.next_before;older.hidden=!cursor;drawFeed();}catch(e){$('status').textContent=e.message;}finally{older.disabled=false;}};
    $('detail').replaceChildren(head,node('h2',j.title),node('div',projectName(j.document_source||j.origin.project||j.project)+' ／ 依頼者・承認者：未記録（利用者ログイン未実装）','sub'),
      node('div','対象：'+paths(j).join(' / '),'path'),stats(tasks,d.approvals),
      node('h3','① 工程の時系列'),gantt(j,tasks),
      node('h3','②③ 承認の記録と作業の動き'),types,Object.assign(node('ol',undefined,'feed'),{id:'feed'}),older,
      node('p','新しい順。作業の動きは200件ずつ過去へさかのぼれます。','hint'),usage);
    drawFeed();
    if(scroll&&matchMedia('(max-width:980px)').matches)$('detail').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(e){if(token===version)$('detail').replaceChildren(node('p',e.message+' 「最新の状態に更新」で再試行できます。','hint'));}
}
async function load(){
  if(loading)return;loading=true;$('refresh').disabled=true;
  try{
    const [history,ledger]=await Promise.all([api(),fetch('/api/ledger').then(async r=>{const d=await r.json();if(!r.ok)throw Error(d.error||'プロジェクト名を取得できません。');return d;})]);
    state=history;$('quota').replaceChildren(...Object.entries(history.quota||{}).map(([p,q])=>node('p',p+' — '+quotaText(q))));
    projectNames=new Map(ledger.projects.filter(p=>p.name?.trim()).map(p=>[key(p.path),p.name]));
    const old=$('project').value,projects=new Map();state.jobs.forEach(j=>paths(j).forEach(p=>projects.set(key(p),p)));
    $('project').replaceChildren(new Option('すべて',''),...[...projects].sort((a,b)=>projectName(a[1]).localeCompare(projectName(b[1]))).map(([k,p])=>new Option(projectName(p),k)));
    $('project').value=projects.has(old)?old:'';
    const list=filtered();if(!list.some(j=>j.id===selected))selected=list[0]?.id||'';
    renderList();if(selected)await detail(selected,false);else $('detail').replaceChildren(node('p','表示できる依頼がありません。','hint'));
  }catch(e){$('status').textContent=e.message+' 「最新の状態に更新」で再試行できます。';}
  finally{loading=false;$('refresh').disabled=false;}
}
for(const id of ['project','origin','search'])$(id).addEventListener('input',renderList);
$('refresh').onclick=()=>load();load();
