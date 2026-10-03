'use strict';
const $=id=>document.getElementById(id);
const statuses={planning:'計画中',queued:'待機',running:'実行中',awaiting_approval:'確認・承認待ち',blocked:'判断待ち',failed:'失敗',interrupted:'中断',succeeded:'担当工程終了',completed:'完了',cancelled:'中止',handed_off:'引き継ぎ済み'};
const origins={ledger:'台帳からの依頼',new:'新規依頼',unrecorded:'入口未記録'};
let state={jobs:[],tasks:[]},selected='',cursor=null,version=0,loading=false;
const node=(tag,text)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;return e;};
const date=t=>t?new Date(t*1000).toLocaleString('ja-JP'):'未記録';
const key=p=>String(p||'').replaceAll('\\','/').replace(/\/$/,'').toLowerCase();
const expanded=new Map();
let projectNames=new Map();
const projectName=p=>projectNames.get(key(p))||String(p).replaceAll('\\','/').split('/').filter(Boolean).pop()||'名称未登録';
const elapsed=(start,end)=>{if(!start||!end||end<start)return '未記録';const seconds=Math.floor(end-start);return Math.floor(seconds/3600)+'時間 '+Math.floor(seconds%3600/60)+'分 '+seconds%60+'秒';};
const paths=j=>[j.project,j.document_source,j.origin.project].filter(Boolean);
async function api(query=''){const r=await fetch('/api/project-history'+query);const d=await r.json();if(!r.ok)throw Error(d.error||'履歴を取得できません。');return d;}
function render(){
 const list=state.jobs.filter(j=>(!$('project').value||paths(j).some(p=>key(p)===$('project').value))&&(!$('origin').value||j.origin.kind===$('origin').value)&&j.title.toLowerCase().includes($('search').value.toLowerCase())).sort((a,b)=>b.created_at-a.created_at);
 $('status').textContent=list.length+'件の依頼。入口が記録されていない過去の依頼は新規依頼と断定しません。';$('timeline').replaceChildren();
 const groups=new Map();
 for(const j of list){const project=$('project').value?paths(j).find(p=>key(p)===$('project').value):(j.document_source||j.origin.project||j.project||'対象未記録');const k=key(project);if(!groups.has(k))groups.set(k,{project,jobs:[]});groups.get(k).jobs.push(j);}
 for(const [k,group] of groups){
 const section=node('details');section.className='project-group';section.open=expanded.get(k)??Boolean($('project').value);section.addEventListener('toggle',()=>expanded.set(k,section.open));
 const heading=node('summary',projectName(group.project)+' — '+group.jobs.length+'件の依頼');heading.title=group.project;section.append(heading);$('timeline').append(section);
 for(const j of group.jobs){
  const card=node('article');card.className='job';const head=node('div');head.className='job-head';const open=node('button',j.title);open.onclick=()=>detail(j.id);head.append(open,node('span',(origins[j.origin.kind]||'入口未記録')+' / '+(statuses[j.status]||j.status)));card.append(head);
  const p=node('p',paths(j).join(' / '));p.className='path';card.append(p);
  const tasks=state.tasks.filter(t=>t.job_id===j.id).sort((a,b)=>a.created_at-b.created_at);
  const times=[j.created_at,...tasks.flatMap(t=>[t.created_at,t.started_at,t.finished_at])].filter(Boolean);
  const min=Math.min(...times),max=Math.max(min+60,...times,...(tasks.some(t=>t.started_at&&!t.finished_at)?[Date.now()/1000]:[]));
  const axis=node('div');axis.className='axis';axis.append(node('span',date(min)),node('span',date(max)));card.append(axis);
  for(const t of tasks){const row=node('div');row.className='task';const label=node('div',t.title);const profile=t.profile||{};label.append(node('small',[profile.adapter,profile.model,t.role,'試行 '+t.attempt].filter(Boolean).join(' / ')));const active=['running','awaiting_approval'].includes(t.status);const endTime=t.finished_at||(active?Date.now()/1000:null);label.append(node('small','開始：'+date(t.started_at)),node('small','終了：'+(t.finished_at?date(t.finished_at):(active?'実行中・未終了':'未記録'))),node('small',(active?'経過時間：':'実行時間：')+elapsed(t.started_at,endTime))); const track=node('div');track.className='track';const bar=node('span',statuses[t.status]||t.status);bar.className='bar';bar.dataset.status=t.status;
   const start=t.started_at||t.created_at;const end=t.finished_at||(['running','awaiting_approval'].includes(t.status)&&t.started_at?Date.now()/1000:start);const left=Math.min(95,Math.max(0,(start-min)/(max-min)*100));bar.style.left=left+'%';bar.style.width=Math.min(100-left,Math.max(5,(end-start)/(max-min)*100))+'%';bar.title='開始: '+date(t.started_at)+' / 終了: '+date(t.finished_at)+(t.summary?'\n'+t.summary:'');track.append(bar);row.append(label,track);card.append(row);}
  if(!tasks.length)card.append(node('p','作業記録はありません。'));section.append(card);
 }
 }
 if(!list.length)$('timeline').append(node('p','条件に一致する依頼がありません。'));
}
function appendEvents(events){for(const e of events){const li=node('li',date(e.at)+' — '+e.message);li.append(node('small',' ['+e.type+']'));$('events').append(li);}}
async function detail(id){selected=id;cursor=null;const token=++version;$('detail').hidden=false;$('detail-title').textContent='記録を取得しています…';$('events').replaceChildren();$('approvals').replaceChildren();$('older').hidden=true;
 try{const d=await api('?job_id='+encodeURIComponent(id));if(token!==version)return;const j=d.jobs.find(j=>j.id===id);$('detail-title').textContent=j.title;$('people').textContent='依頼者・進捗確認者・成果確認者・承認者：未記録（利用者ログイン未実装）';$('paths').textContent='対象: '+paths(j).join(' / ');
  for(const a of d.approvals){const e=node('div',a.kind+' / '+a.status+' — 作成 '+date(a.created_at)+' / 判断 '+date(a.decided_at));e.className='approval';if(a.note)e.append(node('p',a.note));$('approvals').append(e);}if(!d.approvals.length)$('approvals').append(node('p','確認・承認の記録はありません。'));
  appendEvents(d.events);cursor=d.next_before;$('older').hidden=!cursor;$('detail').scrollIntoView({behavior:'smooth',block:'start'});
 }catch(e){if(token===version)$('detail-title').textContent=e.message;}
}
$('older').onclick=async()=>{const token=version,id=selected;$('older').disabled=true;try{const d=await api('?job_id='+encodeURIComponent(id)+'&before='+cursor);if(token!==version)return;appendEvents(d.events);cursor=d.next_before;$('older').hidden=!cursor;}catch(e){$('status').textContent=e.message;}finally{$('older').disabled=false;}};
async function load(){if(loading)return;loading=true;$('refresh').disabled=true;try{const [history,ledger]=await Promise.all([api(),fetch('/api/ledger').then(async r=>{const d=await r.json();if(!r.ok)throw Error(d.error||'プロジェクト名を取得できません。');return d;})]);state=history;projectNames=new Map(ledger.projects.filter(p=>p.name?.trim()).map(p=>[key(p.path),p.name]));const old=$('project').value;const projects=new Map();state.jobs.forEach(j=>paths(j).forEach(p=>projects.set(key(p),p)));$('project').replaceChildren(new Option('すべて',''));[...projects].sort((a,b)=>a[1].localeCompare(b[1])).forEach(([k,p])=>$('project').append(new Option(projectName(p),k)));$('project').value=projects.has(old)?old:'';render();}catch(e){$('status').textContent=e.message+' 更新ボタンで再試行できます。';}finally{loading=false;$('refresh').disabled=false;}}
for(const id of ['project','origin','search'])$(id).addEventListener('input',render);
$('refresh').onclick=()=>load();load();
