'use strict';
let state = null, selected = null, detailVersion = '', busy = false, providerVersion = '';
const $ = id => document.getElementById(id);
const roles = {planner:'計画',researcher:'調査',builder:'実装',reviewer:'レビュー'};
const statuses = {queued:'待機',running:'実行中',awaiting_approval:'承認待ち',succeeded:'担当作業終了',handed_off:'未完了項目を残して引き継ぎ済み',failed:'失敗',cancelled:'中止',interrupted:'中断',blocked:'判断が必要'};
const jobStatuses = {...statuses,planning:'計画中',awaiting_acceptance:'成果の確認待ち',accepted:'利用者が確認済み',accepted_with_pending_checks:'成果を受領・未完了項目あり'};
function el(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
function notice(text) { $('notice').textContent=text; }
function date(t) { return new Date(t*1000).toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit',second:'2-digit'}); }
let sessionRecovery = null;
async function recoverSession() {
  if(!sessionRecovery)sessionRecovery=(async()=>{
    const response=await fetch('/',{credentials:'same-origin',cache:'no-store',redirect:'error'});
    if(!response.ok)throw new Error('画面の接続を復旧できませんでした。再読み込みしてください。');
    await response.text();
  })().finally(()=>{sessionRecovery=null;});
  return sessionRecovery;
}
async function api(path, data, retried=false) {
  const response=await fetch(path,data===undefined?{credentials:'same-origin',cache:'no-store'}:{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Agent-Team-UI':'1'},body:JSON.stringify(data)});
  const result=await response.json();
  // These exact errors are returned before any operation is performed. Never retry
  // timeouts, network failures, or other responses that could duplicate a mutation.
  if(!retried&&response.status===403&&['画面を再読み込みしてください。','ブラウザからの認証済み操作が必要です。'].includes(result.error)){
    await recoverSession();return api(path,data,true);
  }
  if(!response.ok)throw new Error(result.error||'通信に失敗しました。'); return result;
}
async function action(fn) { if(busy)return; busy=true; try{await fn();await refresh();}catch(e){notice(e.message);}finally{busy=false;} }
function button(text, fn, cls) { const b=el('button',text,cls);b.type='button';b.addEventListener('click',()=>action(fn));return b; }
function pending(id) { return state.approvals.filter(a=>a.task_id===id&&a.status==='pending'); }
function endedJob(task){return ['cancelled','accepted','accepted_with_pending_checks'].includes(state.jobs.find(j=>j.id===task.job_id)?.status);}
function stopReason(task){
  const summary=task.summary||'',claude=task.profile?.adapter==='claude'||/^Claude Code:/i.test(summary);
  if(claude&&/hit your session limit/i.test(summary)){
    const match=summary.match(/resets\s+(.+)/i);let reset=match?.[1]?.trim()||'';
    reset=reset.replace(/(\d{1,2}):(\d{2})\s*(am|pm)/i,(_,h,m,period)=>`${String((Number(h)%12)+(period.toLowerCase()==='pm'?12:0)).padStart(2,'0')}:${m}`);
    return {code:'claude-five-hour-limit',title:'Claudeの5時間枠を使い切りました',
      detail:(reset?`Claudeのリセット案内：${reset}。`:'')+'Codexに切り替えるか、利用枠の回復後に再試行してください。Claude内のモデル変更では共通枠のため解消しません。'};
  }
  if(claude&&/Failed to authenticate|OAuth.*expired|authentication/i.test(summary))return {code:'claude-auth',title:'Claudeのログインが失効しています',detail:'Claudeに再ログインしてから再試行してください。'};
  return null;
}
function group(task) { if(endedJob(task))return 3; if(pending(task.id).length||['failed','blocked','interrupted'].includes(task.status))return 2; if(task.status==='queued')return 0; if(['running','awaiting_approval'].includes(task.status))return 1; return 3; }
const completedFolds=new Map();
function completedTime(task){
  const job=state.jobs.find(j=>j.id===task.job_id);
  return task.finished_at||task.cancelled_at||(endedJob(task)?job?.updated_at:null)||task.updated_at||task.created_at||0;
}
function completedPeriod(task){
  const stamp=completedTime(task);
  if(!stamp)return {key:'unknown',label:'終了日時不明'};
  const parts=Object.fromEntries(new Intl.DateTimeFormat('ja-JP',{timeZone:'Asia/Tokyo',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',hourCycle:'h23'}).formatToParts(new Date(stamp*1000)).map(p=>[p.type,p.value]));
  const morning=Number(parts.hour)<12;
  return {key:`${parts.year}-${parts.month}-${parts.day}-${morning?'00':'12'}`,label:`${parts.year}/${parts.month}/${parts.day} ${morning?'午前（00:00〜11:59）':'午後（12:00〜23:59）'}`};
}
function quotaView(name) {
  const usage=state.usage?.[name],box=el('section',undefined,'quota');
  box.setAttribute('aria-label',(name==='codex'?'Codex':'Claude Code')+'の利用枠の残量');
  const models=[...new Set(Object.values(state.config.roles).map(id=>state.config.profiles[id]).filter(p=>p?.adapter===name).map(p=>p.model))];
  box.append(el('p','設定モデル: '+(models.join(' / ')||'未設定'),'quota-models'));
  const percent=w=>typeof w?.remaining_percent==='number'?`${w.remaining_percent}％`:'不明';
  const rows=usage?.rows?.length?usage.rows:[{label:'利用枠',short:null,weekly:null}];
  for(const row of rows){
    const item=el('div',undefined,'quota-row');
    const period=row.minutes?(row.minutes%60===0?`${row.minutes/60}時間枠`:`${row.minutes}分枠`):'';
    item.append(el('small',`${row.label}${period?' · '+period:''}`));
    const line=el('div',undefined,'quota-value');
    if(row.weekly_only)line.append(el('span',`（週間残り ${percent(row.weekly)}）`));
    else line.append(el('strong',`残り ${percent(row.short)}`),el('span',`（週間残り ${percent(row.weekly)}）`));
    item.append(line);
    if(usage?.stale&&usage.updated_at)item.append(el('small','前回取得値・最新値は未確認','quota-warning'));
    const meterWindow=row.weekly_only?row.weekly:row.short;
    if(meterWindow){const meter=el('meter');meter.min=0;meter.max=100;meter.low=20;meter.high=50;meter.optimum=100;meter.value=meterWindow.remaining_percent;meter.setAttribute('aria-label',row.label+'の残量');item.append(meter);}
    const resets=[];for(const [key,label] of [['short','短期'],['weekly','週間']])if(row[key]?.resets_at)resets.push(`${label}リセット: ${new Date(row[key].resets_at*1000).toLocaleString('ja-JP')}`);
    if(resets.length)item.append(el('small',resets.join(' / '),'quota-reset'));
    box.append(item);
  }
  box.append(el('small',usage?.note||'利用枠を取得中…','quota-note'));
  if(usage?.updated_at)box.append(el('small','取得: '+new Date(usage.updated_at*1000).toLocaleString('ja-JP'),'quota-note'));
  if(usage?.status==='unavailable'&&usage.next_update_at)box.append(el('small','次回取得: '+new Date(usage.next_update_at*1000).toLocaleTimeString('ja-JP'),'quota-note'));
  box.append(el('small','アカウントの利用枠です。会話のコンテキスト残量とは異なります。','quota-note'));
  box.append(button('停止・待機タスクのモデルを切り替える',()=>openModelSwitch(null,name)));
  return box;
}
function renderCliUpdate(){
  function badge(id,info){
    const fold=$(id),label=fold.querySelector('.cli-update-badge');
    label.textContent=info.state==='available'?'更新あり':info.state==='updating'?'更新中':
      info.state==='current'?'最新版':info.state==='error'?'確認できません':'確認中';
    fold.classList.toggle('has-update',info.state==='available');
    label.classList.toggle('available',info.state==='available');
  }
  const info=state.cli_update||{},installed=info.installed||'不明',latest=info.latest||'不明';
  badge('codex-update-fold',info);
  const label=info.state==='available'?`更新あり：${installed} → ${latest}。${info.message}`:
    `現在 ${installed}／公開版 ${latest}。${info.message||'更新情報を確認しています。'}`;
  $('cli-update-status').textContent=label;
  $('cli-update-checked').textContent=info.checked_at?'最終確認: '+new Date(info.checked_at*1000).toLocaleString('ja-JP'):'';
  $('cli-update-check').disabled=['checking','updating'].includes(info.state);
  $('cli-update-install').disabled=info.state!=='available';
  $('cli-update-install').textContent=info.state==='updating'?'更新中…':`CLIを${info.state==='available'?latest+'へ':''}更新`;
  const claude=state.claude_update||{},current=claude.installed||'不明',released=claude.latest||'不明';
  badge('claude-update-fold',claude);
  $('claude-update-status').textContent=claude.state==='available'?`更新あり：${current} → ${released}。${claude.message}`:
    `現在 ${current}／公開版 ${released}。${claude.message||'更新情報を確認しています。'}`;
  $('claude-update-checked').textContent=claude.checked_at?'最終確認: '+new Date(claude.checked_at*1000).toLocaleString('ja-JP'):'';
  $('claude-update-check').disabled=['checking','updating'].includes(claude.state);
  $('claude-update-install').disabled=claude.state!=='available';
  $('claude-update-install').textContent=claude.state==='updating'?'更新中…':'Claude Code CLIを更新';
}
function render() {
  $('connection').textContent=state.paused?'新規着手を一時停止中':'● ローカル接続中';
  $('pause').textContent=state.paused?'新規着手を再開':'新規着手を一時停止';
  renderCliUpdate();
  const nextProviderVersion=JSON.stringify([state.providers,state.usage,state.config.roles,state.config.profiles,state.cli_update,state.claude_update]);
  if(nextProviderVersion!==providerVersion){
  providerVersion=nextProviderVersion;
  $('providers').replaceChildren();
  for(const [name,p] of Object.entries(state.providers)){
    const box=el('article',undefined,'provider'), text=el('div');box.append(el('div',name==='codex'?'C':'✳','symbol'));
    text.append(el('h2',name==='codex'?'Codex':'Claude Code'),el('p',p.available?p.version:'接続の準備が必要'),el('small',p.note),quotaView(name));
    if(name==='codex'&&state.cli_update?.state==='available')text.append(button(`CLI更新あり：${state.cli_update.latest}。設定を開く`,()=>{$('settings-open').click();$('codex-update-fold').open=true;}));
    if(name==='claude'&&state.claude_update?.state==='available')text.append(button(`CLI更新あり：${state.claude_update.latest}。設定を開く`,()=>{$('settings-open').click();$('claude-update-fold').open=true;}));
    if(name==='codex')text.append(button('利用枠を再取得',()=>action(async()=>{
      const result=await api('/api/codex-usage/refresh',{});
      notice(result.success?'Codexの利用枠を更新しました。':result.usage.note);
    })));
    if(name==='claude'){
      text.append(el('small','采来専用ログインを使用。通常のClaude Codeとは保存先を分けています。'));
      text.append(button('ログイン状態・残量を再確認',()=>action(async()=>{await api('/api/claude-auth/refresh',{});} )));
      if(p.authenticated===false)text.append(el('p','PCで采来の Login-ClaudeCode.ps1 を実行してログイン後、再確認を押してください。'));
    }
    box.append(text);$('providers').append(box);
  }
  }
  const filter=$('filter').value;
  $('filter').replaceChildren(new Option('すべての依頼',''),...state.jobs.map(j=>new Option(j.title,j.id)));$('filter').value=filter;
  $('job-count').textContent=`${state.jobs.length}件`;
  const labels=['待機している作業','チームが作業中','あなたの判断','終了した作業'];
  $('board').querySelectorAll('details[data-period-key]').forEach(d=>completedFolds.set(d.dataset.periodKey,d.open));
  $('board').replaceChildren(...labels.map((label,index)=>{
    const column=el('section',undefined,'column'),tasks=state.tasks.filter(t=>(!filter||t.job_id===filter)&&group(t)===index);
    const heading=el('h3',label);heading.append(el('span',String(tasks.length)));column.append(heading);
    const buckets=new Map();
    if(index===3){
      column.append(el('p','履歴は自動削除しません。日付・午前／午後を開いて確認できます。','hint'));
      tasks.sort((a,b)=>completedTime(b)-completedTime(a));
      for(const task of tasks){const period=completedPeriod(task);if(!buckets.has(period.key))buckets.set(period.key,{...period,tasks:[]});buckets.get(period.key).tasks.push(task);}
      for(const bucket of buckets.values()){
        const fold=el('details',undefined,'completed-period'),summary=el('summary',bucket.label),count=el('span',`${bucket.tasks.length}件`),content=el('div',undefined,'completed-period-cards');
        summary.append(count);fold.append(summary,content);fold.dataset.periodKey=bucket.key;fold.open=completedFolds.get(bucket.key)===true;
        fold.addEventListener('toggle',()=>{if(fold.isConnected)completedFolds.set(bucket.key,fold.open);});
        bucket.content=content;column.append(fold);
      }
    }
    for(const task of tasks){
      const job=state.jobs.find(j=>j.id===task.job_id),card=button('',()=>openTask(task.id),'card'+(index===2?' attention':''));
      const badge=el('span',endedJob(task)?(job.status==='cancelled'?'中止済みの依頼':jobStatuses[job.status]):pending(task.id).length?'確認・承認してください':statuses[task.status]||task.status,'badge'+(index===2?' approval':''));
      card.append(badge,el('strong',task.title));
      const reason=stopReason(task);
      if(reason)card.append(el('p',reason.title,'summary'));
      else if(task.summary)card.append(el('p',task.summary,'summary'));
      const meta=el('div',undefined,'meta');meta.append(el('span',`${roles[task.role]} · ${task.profile.adapter}`),el('span',job?.title||''));card.append(meta);
      if(task.common_agents?.length){const run=task.agent_run,labels={starting:'起動中',running:'実行中',completed:'実行完了',failed:'失敗',cancelled:'中止'};
        card.append(el('p','共通Agent: '+task.common_agents.join('、')+' · '+(run?labels[run.status]||run.status:task.agent_name?'起動待ち':'旧方式の役割指示'),'hint'));}
      if(index===2&&!pending(task.id).some(a=>a.payload.questions?.length)){
        const entry=el('div',undefined,'task-entry'),help=el('button','回答方法のヘルプ','decision-help-button');help.type='button';
        help.setAttribute('aria-label',task.title+'の回答方法のヘルプ');
        help.onclick=()=>{openTask(task.id);const toggle=$('task-help-toggle');if(toggle){revealSection(toggle);toggle.click();}};entry.append(card,help);column.append(entry);
      }else (index===3?buckets.get(completedPeriod(task).key).content:column).append(card);
    }
    if(!tasks.length)column.append(el('p',index===0&&!state.jobs.length?'「依頼を追加」から始められます。':'ここに該当する作業はありません。','empty'));
    return column;
  }));
  $('events').replaceChildren(...state.events.slice(0,30).map(e=>{const li=el('li');li.append(el('time',date(e.at)),el('span',e.message));return li;}));
  if(!state.events.length)$('events').append(el('li','まだ実行履歴はありません。サンプルデータを実績として表示していません。'));
  if(selected&&$('detail-dialog').open){
    const task=state.tasks.find(t=>t.id===selected);if(task){const version=JSON.stringify([task.updated_at,state.jobs.find(j=>j.id===task.job_id)?.updated_at,state.approvals.filter(a=>a.task_id===selected).map(a=>[a.id,a.status,a.updated_at,state.tasks.find(t=>t.id===a.payload?.investigation_task_id)?.updated_at])]);if(version!==detailVersion)renderDetail(task,version);}
  }
}
function section(title,text) { const s=el('section',undefined,'detail-section');s.append(el('h3',title),el('div',text,'text-block'));return s; }
const detailFolds=new Map();let renderedDetailTask=null;
function revealSection(node){
  for(let parent=node.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
}
function foldDetailSections(body,task){
  const previous=detailFolds.get(task.id)||{signature:null,open:{}};
  const requests=endedJob(task)?[]:pending(task.id);
  const signature=JSON.stringify([task.status,task.attempt,endedJob(task),task.recovery_advice?.code||null,requests.map(a=>[a.id,a.status])]);
  const changed=previous.signature!==signature;
  const stopped=!endedJob(task)&&['failed','blocked','interrupted'].includes(task.status);
  const scopeError=/未完了項目の引き継ぎには利用者の範囲指定/.test(task.summary||'');
  const completedReport=task.recovery_advice?.code==='task_completion';
  const focus=requests.length?'.approval-box':stopped?(!completedReport&&(stopReason(task)||scopeError)?'#task-recovery':'#recovery-advice'):null;
  const focusNode=focus?body.querySelector(focus):null;
  let focusFold=null;
  for(const [index,node] of [...body.children].entries()){
    if(!['SECTION','FORM','DETAILS'].includes(node.tagName))continue;
    const heading=node.querySelector(':scope > h3'),title=heading?.textContent||node.querySelector(':scope > summary')?.textContent||
      (node.querySelector('#task-help-toggle')?'回答方法のヘルプ':'操作・情報');
    const key=node.id||title+'-'+(node.classList.contains('approval-box')?index:'');
    let fold=node;
    if(node.tagName!=='DETAILS'){
      fold=el('details',undefined,'detail-section detail-fold');const summary=el('summary',title);
      node.before(fold);fold.append(summary,node);heading?.remove();node.dataset.folded='true';
    }else fold.classList.add('detail-fold');
    fold.dataset.foldKey=key;
    const isFocus=focusNode&&(node===focusNode||node.contains(focusNode));
    fold.open=changed?Boolean(isFocus):Boolean(previous.open[key]);
    if(isFocus)focusFold=fold;
    fold.addEventListener('toggle',()=>{const saved=detailFolds.get(task.id);if(saved&&fold.isConnected)saved.open[key]=fold.open;});
  }
  detailFolds.set(task.id,{signature,open:Object.fromEntries([...body.querySelectorAll(':scope > details[data-fold-key]')].map(d=>[d.dataset.foldKey,d.open]))});
  if(changed&&focusFold)requestAnimationFrame(()=>{if(focusFold.isConnected)focusFold.scrollIntoView({block:'start'});});
}
function reportText(result){
  let text=result.summary||'';
  if(result.tasks)text+='\n\n'+result.tasks.map((t,i)=>`${i+1}. ${t.title}（${roles[t.role]||t.role}）\n${t.instruction}`).join('\n\n');
  if(result.security_review_required===true)text+='\n\n通常レビューの後、common-security-reviewerを独立セッションで起動します。';
  if(result.checks?.length)text+='\n\n報告された検証:\n'+result.checks.map(c=>'・'+c).join('\n');
  if(result.question)text+='\n\n確認したいこと:\n'+result.question;
  if(result.note)text+='\n\n'+result.note;
  if(result.pending_items?.length)text+='\n\n未完了として残す項目（合格・全体完了ではありません）:\n'+result.pending_items.map(item=>`${item.title}\n${item.note}\n${item.checks.join('\n')}`).join('\n\n');
  return text;
}
async function showHandoffHistory(jobId,container,before){
  try{
    const query='/api/handoff?job='+encodeURIComponent(jobId)+(before?'&before='+before:'');
    const data=await api(query);
    if(!container.isConnected||selected!==container.dataset.task)return;
    if(!before){
      container.replaceChildren();
      if(!container.dataset.folded)container.append(el('h3','引き継ぎDB：現在の情報と履歴'));
      const facts=el('div',undefined,'text-block');
      facts.textContent=data.facts.length?data.facts.map(f=>`${f.key}: ${f.value}\n出典: ${f.authority} / ${f.evidence}`).join('\n\n'):'確定した項目はまだありません。';
      container.append(facts);
      if(data.conflicts.length)container.append(el('p','矛盾する情報があります。判断欄で新旧を確認してください。','hint'));
    }
    for(const record of data.records){
      const item=el('details'),title=record.payload.title||record.payload.kind||record.payload.questions?.[0]?.text||'';
      item.append(el('summary',`${date(record.at)} · ${record.kind} · ${title}`),
                  el('div',JSON.stringify(record.payload,null,2),'text-block'));
      container.append(item);
    }
    if(data.records.length===50){
      const more=el('button','古い履歴を表示');more.type='button';
      more.onclick=()=>{more.remove();showHandoffHistory(jobId,container,data.records.at(-1).seq);};
      container.append(more);
    }
  }catch(error){if(container.isConnected)container.append(el('p','引き継ぎ履歴を読み込めません: '+error.message,'hint'));}
}
const answerDrafts=new Map();
function questionForm(approval){
  const box=el('section',undefined,'approval-box'),form=el('form'),message=el('p','','hint');
  message.setAttribute('role','status');
  box.append(el('h3','判断が必要です'),el('p','各項目のボタンを1つずつ選んでください。補足は必要な場合だけ入力できます。'));
  if(approval.kind==='context_conflict'){
    box.append(el('p','文章の違いだけでは矛盾と断定できません。利用者が観測していない事実は、推測して選ばないでください。','hint'));
    const investigate=el('button','判断材料が足りないので、AIに根拠を確認させる','primary'),status=el('p','','hint');investigate.type='button';status.setAttribute('role','status');
    const task=state.tasks.find(t=>t.id===approval.payload.investigation_task_id);
    if(task&&['queued','running','awaiting_approval'].includes(task.status)){investigate.disabled=true;status.textContent='AIが根拠を調査中です。回答せずに結果を待てます。';}
    else if(task&&['failed','interrupted','cancelled'].includes(task.status))status.textContent='根拠確認が停止しました：'+(task.summary||'原因を確認してください。');
    investigate.onclick=async()=>{investigate.disabled=true;status.textContent='根拠の確認を依頼しています…';
      try{await api('/api/conflicts/investigate',{id:approval.id});await refresh();}catch(error){status.textContent=error.message;investigate.disabled=false;}};
    box.append(investigate,status);
    if(approval.payload.analysis){box.append(section('AIによる確認結果・違い・影響・推奨案',approval.payload.analysis));if(approval.payload.analysis_checks?.length)box.append(section('確認した根拠',approval.payload.analysis_checks.join('\n')));}
  }
  if(approval.payload.summary){const details=el('details');details.append(el('summary',approval.kind==='context_conflict'?'新旧の内容と根拠を読む':'作業計画と背景を読む'),el('div',approval.payload.summary,'text-block'));box.append(details);}
  const drafts=answerDrafts.get(approval.id)||{};answerDrafts.set(approval.id,drafts);
  for(const q of approval.payload.questions){
    const field=el('fieldset',undefined,'question-field');field.append(el('legend',q.text));
    if(q.context){
      const c=q.context,info=el('div',undefined,'conflict-context');
      info.append(el('p',c.reason),el('p','判断による影響：'+c.impact),el('p','未確認点：'+c.uncertainty));
      for(const [prefix,title] of [['old','従来の報告'],['new','新しい報告']]){
        const part=el('div',undefined,'conflict-report');part.append(el('h4',title),el('p',c[prefix]),el('p',`記録日時：${c[prefix+'_time']?new Date(c[prefix+'_time']*1000).toLocaleString('ja-JP'):'不明'}／出典：${c[prefix+'_authority']==='model_reported'?'AIの報告（利用者確認ではありません）':c[prefix+'_authority']||'不明'}`,'hint'),el('p','根拠：'+(c[prefix+'_evidence']||'未提示'),'hint'));info.append(part);
      }
      field.append(info);
    }
    const draft=drafts[q.id]||(drafts[q.id]={option_id:null,texts:{}}),buttons=[];
    function select(id){draft.option_id=id;buttons.forEach(([key,b])=>b.setAttribute('aria-pressed',String(key===id)));}
    for(const option of q.options){
      const row=el('div',undefined,'answer-option'),choice=el('button',option.label,'answer-choice');choice.type='button';
      choice.setAttribute('aria-pressed',String(draft.option_id===option.id));buttons.push([option.id,choice]);
      choice.addEventListener('click',()=>select(option.id));row.append(choice);
      if(option.input_label||option.input_required){
        const label=el('label',option.input_label||'補足'),input=el('input');input.type='text';input.maxLength=1000;
        input.placeholder=option.input_required?'選択した場合は入力必須':'任意で入力';input.value=draft.texts[option.id]||'';
        input.setAttribute('aria-label',q.text+' / '+option.label+' / '+(option.input_label||'補足'));
        input.addEventListener('input',()=>{draft.texts[option.id]=input.value;select(option.id);});label.append(input);row.append(label);
      }
      field.append(row);
    }
    form.append(field);
  }
  if(approval.kind==='tool')form.append(el('p','回答期限: '+date(approval.expires_at),'hint'));
  else form.append(el('p','送信すると、回答を引き継いでこの担当を再開します。','hint'));
  form.append(el('p','「保留して閉じる」では、この担当と後続の作業は進みません。','hint'));
  const actions=el('div',undefined,'actions'),later=el('button','保留して閉じる'),submit=el('button','まとめて回答して続行','primary');
  later.type='button';later.addEventListener('click',()=> $('detail-dialog').close());submit.type='submit';
  actions.append(later,submit);form.append(message,actions);box.append(form);
  form.addEventListener('submit',async e=>{
    e.preventDefault();if(submit.disabled)return;
    const answers={};
    for(const q of approval.payload.questions){
      const d=drafts[q.id],option=q.options.find(o=>o.id===d.option_id),text=d.texts[d.option_id]||'';
      if(!option){message.textContent='未回答の質問があります。すべて選択してください。';return;}
      if(option.input_required&&!text.trim()){message.textContent='選択した項目の入力欄を記入してください。';return;}
      answers[q.id]={option_id:option.id,text};
    }
    submit.disabled=true;message.textContent='回答を送信しています…';
    try{await api('/api/decide',{id:approval.id,allow:true,answers});answerDrafts.delete(approval.id);await refresh();}
    catch(error){message.textContent=error.message;}
    finally{submit.disabled=false;}
  });
  return box;
}
const recoveryDrafts=new Map();
function pythonFailure(task){const text=(task.summary||'')+' '+JSON.stringify(task.result||{});return /Python/i.test(text)&&/起動失敗|起動でき|プロセス作成失敗|Unable to create process/i.test(text);}
function advicePanel(task){
  const box=el('section',undefined,'decision-help');box.id='recovery-advice';
  box.append(el('h3','停止理由と次の操作'),el('p','原因に応じた案内と回答案を作ります。Python起動失敗では、統括側で対象のPythonを確認します。担当側の環境とは区別して表示します。','hint'));
  const message=el('p','','hint'),check=el('button','原因を確認して回答案を作る','primary');check.type='button';message.setAttribute('role','status');box.append(check,message);
  check.onclick=async()=>{check.disabled=true;message.textContent='停止理由を確認しています…';
    try{await api('/api/tasks/advice',{id:task.id});await refresh();}
    catch(error){message.textContent=error.message;}finally{check.disabled=false;}};
  const advice=task.recovery_advice;
  if(advice&&advice.attempt===task.attempt){
    const reason=el('div',undefined,'stop-reason-box');reason.setAttribute('role','note');
    reason.append(el('h4',advice.title),el('p',advice.explanation,'stop-reason-text'));
    box.append(reason,el('p',advice.next_step,'stop-next-step'));
    box.append(el('p','確認: '+new Date(advice.checked_at*1000).toLocaleString('ja-JP')+'。過去の確認結果です。再開前に担当側でも再確認します。','hint'));
    if(advice.evidence?.length)box.append(section('統括側の確認結果',advice.evidence.map(e=>`${e.ok?'成功':'未確認・失敗'}：${e.file}\n${e.result}`).join('\n\n')));
    if(advice.draft){
      const label=el('label','担当へ渡す回答案'),reply=el('textarea');reply.rows=6;reply.readOnly=true;reply.id='advice-draft';reply.value=advice.draft;label.htmlFor=reply.id;
      const fill=el('button','回答案を再試行の入力欄へ入れる');fill.type='button';
      if(advice.code==='computer_use' && advice.can_retry===false){fill.disabled=true;fill.title='PCにサインインして画面を接続した後、原因を再確認してください。';}
      fill.onclick=()=>{const input=$('retry-note');if(!input){message.textContent='保留中の質問・承認に先に回答してください。回答案はこの欄から選択・コピーできます。';return;}
        if(!input.value.includes(advice.draft))input.value=input.value.trim()?input.value+'\n\n'+advice.draft:advice.draft;
        revealSection(input);input.scrollIntoView({block:'center'});input.focus();notice('回答案を入力しました。内容を確認して「回答・補足を送って再試行」を押してください。');};
      box.append(label,reply,fill);
    }
  }
  return box;
}
function recoveryPanel(task,job){
  const box=el('section',undefined,'decision-help');box.id='task-recovery';
  box.append(el('h3','ボードから復旧する'));
  if(task.recovery_advice?.code==='computer_use'){
    box.append(el('p','ゲームの実画面操作が停止しています。工程の移管では解消しません。上の「停止理由と次の操作」で、PC画面の接続状態と再開手順を確認してください。'));
    return box;
  }
  if(task.recovery_advice?.code==='task_completion'){
    box.append(el('p','担当は工程の完了を報告しています。上の「停止理由と次の操作」に、完了として再報告する回答案を表示しています。今回の移管欄は使いません。','hint'));
    return box;
  }
  const internal=task.recovery?.code==='handoff_rejected'||/未完了項目の引き継ぎには利用者の範囲指定/.test(task.summary||'');
  box.append(el('p',internal?'引き継ぎ範囲を統括が処理できず停止しています。後続工程へ移す必須作業を指定して保存できます。':'後続工程へ移す作業がある場合は、ここで範囲と担当を確定できます。認証・権限などの障害は、原因を解消してから下の再試行を使ってください。'));
  if(task.recovery?.reported_summary)box.append(section('停止直前の担当報告（未検証）',task.recovery.reported_summary));
  if(pythonFailure(task)){
    box.append(el('p','Python環境の起動失敗で停止しています。上の「停止理由と次の操作」で起動確認と回答案を取得してください。工程3への移管欄は使いません。','hint'));
    return box;
  }
  const transfers=(job.scope_transfers||[]).filter(x=>x.origin_task_id===task.id);
  for(const x of transfers){const target=state.tasks.find(t=>t.id===x.target_task_id);box.append(el('p',`登録済み → ${target?.title||'後続工程'}：${x.scope}`));}
  if(/hit your.*limit|rate.?limit|quota|usage.?limit|利用枠|Failed to authenticate|OAuth.*expired|authentication/i.test(task.summary||'')){
    const reason=stopReason(task);
    box.append(el('strong',reason?.title||'利用枠またはログイン状態で停止しています'),el('p',reason?.detail||'モデルを切り替えるか、利用枠の回復・ログイン後に下の再試行で再開してください。','hint'),button('担当・モデルを切り替える',()=>openModelSwitch(task.id)));
    return box;
  }
  const targets=state.tasks.filter(t=>t.job_id===job.id&&t.after===task.id&&t.role==='builder'&&t.status==='queued');
  if(pending(task.id).length){box.append(el('p','保留中の質問・承認へ先に回答してください。移管はその後に保存できます。','hint'));return box;}
  if(transfers.length){
    const confirmLabel=el('label',undefined,'check'),confirmInput=el('input'),restart=el('button','登録済みの移管で再開','primary'),message=el('p','','hint');
    confirmInput.type='checkbox';confirmLabel.append(confirmInput,document.createTextNode('登録済みの移管内容と実施済みの変更を確認しました。'));
    restart.type='button';restart.disabled=true;confirmInput.onchange=()=>{restart.disabled=!confirmInput.checked;};message.setAttribute('role','status');
    restart.onclick=async()=>{restart.disabled=true;message.textContent='担当を再開しています…';
      try{await api('/api/retry',{id:task.id,checked_changes:true,note:'ボードで登録済みの工程間移管を確認しました。最新の移管範囲に従って再開してください。'});await refresh();}
      catch(error){message.textContent=error.message;restart.disabled=!confirmInput.checked;}};
    box.append(confirmLabel,message,restart);
  }
  if(!targets.length){box.append(el('p','移管できる直後の待機中実装工程がありません。作業を移さず再開する場合は、下の再試行を使ってください。','hint'));return box;}
  const form=el('form'),draft=recoveryDrafts.get(task.id)||{target:targets[0].id,scope:'',checked:false};
  const target=el('select'),scope=el('textarea'),checked=el('input'),message=el('p','','hint');
  target.id='transfer-target';scope.id='transfer-scope';scope.required=true;scope.maxLength=4000;scope.rows=4;
  for(const t of targets){const option=el('option',t.title);option.value=t.id;target.append(option);}
  target.value=targets.some(t=>t.id===draft.target)?draft.target:targets[0].id;scope.value=draft.scope;
  scope.placeholder='例：HEAD・ステージ状態・変更一覧・SHA256を、工程2の退避・編集前に測定して記録する。';
  checked.type='checkbox';checked.checked=draft.checked;checked.required=true;
  const targetLabel=el('label','引き継ぐ後続工程'),scopeLabel=el('label','後続工程で必ず実施する作業');targetLabel.htmlFor=target.id;scopeLabel.htmlFor=scope.id;
  const checkLabel=el('label',undefined,'check');checkLabel.append(checked,document.createTextNode('現在の報告と実施済みの変更を確認しました。指定した作業は後続工程で実施し、検証を省略しません。'));
  const save=el('button','移管を保存'),resume=el('button','移管を保存して再開','primary'),actions=el('div',undefined,'actions');save.type=resume.type='submit';actions.append(save,resume);
  message.setAttribute('role','status');form.append(targetLabel,target,scopeLabel,scope,checkLabel,el('p','保存だけでは担当は再開しません。再開時は現在の担当を再試行し、その範囲を終えてから後続工程へ進みます。','hint'),message,actions);box.append(form);
  const remember=()=>recoveryDrafts.set(task.id,{target:target.value,scope:scope.value,checked:checked.checked});
  target.addEventListener('change',remember);scope.addEventListener('input',remember);checked.addEventListener('change',remember);
  form.addEventListener('submit',async event=>{event.preventDefault();if(save.disabled)return;const doResume=event.submitter!==save;
    save.disabled=resume.disabled=true;message.textContent='移管範囲を保存しています…';
    try{await api('/api/tasks/transfer',{id:task.id,target_id:target.value,scope:scope.value,checked_changes:checked.checked,resume:doResume});
      recoveryDrafts.delete(task.id);notice(doResume?'移管を保存して担当を再開しました。':'移管を保存しました。担当は停止したままです。');await refresh();}
    catch(error){message.textContent=error.message;remember();}finally{save.disabled=resume.disabled=false;}});
  return box;
}
function renderDetail(task,version) {
  if(renderedDetailTask){const saved=detailFolds.get(renderedDetailTask);if(saved)$('detail-body').querySelectorAll(':scope > details[data-fold-key]').forEach(d=>saved.open[d.dataset.foldKey]=d.open);}
  renderedDetailTask=task.id;
  const drafts={};$('detail-body').querySelectorAll('textarea').forEach(t=>drafts[t.id]=t.value);
  detailVersion=version;const job=state.jobs.find(j=>j.id===task.job_id);$('detail-title').textContent=task.title;
  const body=$('detail-body');body.replaceChildren(el('p',`${roles[task.role]} / ${task.profile.adapter} / ${task.profile.model} / ${task.profile.effort} · 試行 ${task.attempt}`),el('p',`状態: ${statuses[task.status]||task.status} · 依頼の状態: ${jobStatuses[job.status]||job.status}`));
  if(task.common_agents?.length)body.append(el('p','担当する共通Agent: '+task.common_agents.join('、'),'hint'));
  const intake=state.jobs.find(j=>j.id===task.job_id)?.intake_classification;
  if(intake)body.append(section('依頼の一次分類',`${intake.label} / ${intake.provider}\n理由: ${intake.reason}\n分類は作業範囲や権限の承認ではありません。`));
  if(task.decision_record){const d=task.decision_record;body.append(section('判断レイヤーの記録',`動作: ${d.shadow?'シャドー（既存判断を維持）':'適用'}\nProvider: ${d.provider}\n状態: ${d.status}\n理由コード: ${d.reason_code}\n既存判断: ${d.baseline}\n候補: ${d.decision}\n採用: ${d.effective}\n差異: ${d.differs?'あり':'なし'}\n時間: ${d.latency_ms} ms`+(d.usage?'\n'+(d.provider==='ollama'?'ローカル':'API')+'トークン: '+JSON.stringify(d.usage):'\nトークン: 未取得')+(d.confidence!==undefined?'\n集中度（正解確率ではありません）: '+d.confidence:'')+(d.probabilities?'\n候補確率: '+JSON.stringify(d.probabilities):'')));}
  if(task.agent_run){const run=task.agent_run,labels={starting:'起動中',running:'実行中',completed:'実行完了',failed:'失敗',cancelled:'中止'};
    body.append(section('Agentの実行情報',`Agent: ${run.name}\n実行方式: ${run.launch_method||'起動確認待ち'}\n状態: ${labels[run.status]||run.status}\nセッションID: ${run.session_id||'未取得'}\n試行: ${run.attempt}\n権限: ${run.sandbox}\n定義SHA256: ${run.definition_hash}`));
    if(run.usage&&Object.keys(run.usage).length)body.append(section('今回のCLIトークン記録',Object.entries(run.usage).map(([key,value])=>`${key}: ${value}`).join('\n')+'\nCLIが返した値です。アカウント利用枠の残量とは異なります。'));
    if(run.context_size)body.append(section('入力サイズの記録',`依頼本文: ${run.context_size.user_prompt_chars}文字\nセッション指示: ${run.context_size.native_instructions_chars}文字\nSkill案内: ${run.context_size.skill_catalog_chars}文字\n文字数はトークン数の代用指標です。`));
    if(run.shared_reads?.length)body.append(section('共通Skillの読み取り要求',run.shared_reads.map(item=>`${item.skill} / ${item.kind}: ${item.file}`).join('\n')+'\nClaude Readの許可記録です。Codexの読込数や未取得の値をゼロとは判定しません。'));
    if(run.failure_code==='claude_auth')body.append(el('p','復旧対象：Claude Code CLIへのログイン。モデル実行前の認証確認で停止しています。','notice'));
    if(run.failure_code==='codex_sandbox')body.append(el('p','復旧対象：CodexのWindowsサンドボックス。権限を広げて迂回せず、実行環境を復旧して再試行してください。モデル実行前に停止しています。','notice'));
  }else if(task.agent_name)body.append(el('p','実行時に共通定義を読み込み、専用セッションを起動します。','hint'));
  else if(task.common_agent_hashes)body.append(el('p','この過去の試行は役割指示を読み込む旧方式でした。独立Agentの起動記録はありません。','hint'));
  if(task.agent_run_history?.length)body.append(section('過去のAgent実行',task.agent_run_history.map(run=>`${run.name} · 試行 ${run.attempt} · ${run.status}\nセッションID: ${run.session_id||'未取得'}`).join('\n\n')));
  if(!endedJob(task)&&group(task)===2)body.append(decisionHelp(task));
  if(!endedJob(task)&&['failed','blocked','interrupted'].includes(task.status))body.append(advicePanel(task));
  if(!endedJob(task)&&['failed','blocked','interrupted'].includes(task.status)&&['researcher','builder'].includes(task.role))body.append(recoveryPanel(task,job));
  body.append(section('今回の作業',task.instruction),section('対象と元の依頼',job.project+'\n\n'+job.goal));
  if(task.instruction_history?.length)body.append(section('過去の指示（履歴・現在の命令ではありません）',task.instruction_history.map(h=>`試行 ${h.attempt} · ${new Date(h.at*1000).toLocaleString()} · ${h.reason}\n${h.instruction}${h.submitted_note?'\n\n当時の追加内容:\n'+h.submitted_note:''}`).join('\n\n────────\n\n')));
  if(task.result)body.append(section('担当の報告（AIによる報告）',reportText(task.result)));
  else if(task.summary)body.append(section('状況',task.summary));
  if(endedJob(task))body.append(el('p','この依頼は終了しています。以下の報告と判断は過去の履歴です。現在の回答は不要です。','hint'));
  if(task.handoff_note)body.append(section('引き継ぎ範囲・未完了項目',task.handoff_note));
  if(task.auto_return){const p=state.config.profiles[state.config.roles[task.role]];
    body.append(section('一時モデル・自動復帰',`通常: ${p.adapter} / ${p.model} / ${p.effort}\n現在: ${task.profile.adapter} / ${task.profile.model}\n利用枠の取得値が新しく、短期・週間・該当する専用枠が各10％以上なら、実行前に通常モデルへ戻します。不明な間は復帰しません。実行中の担当は継続します。`));}
  if(task.status==='blocked'&&task.result&&['builder','researcher'].includes(task.role)&&
     pending(task.id).every(a=>a.kind==='question')&&!['accepted','accepted_with_pending_checks','cancelled','interrupted'].includes(job.status)){
    const handoffForm=el('form',undefined,'detail-section'),label=el('label','後回しにする項目と、次へ進める範囲'),note=el('textarea'),message=el('p','','hint');
    note.id='handoff-note-'+task.id;note.required=true;note.maxLength=4000;note.rows=3;note.value=drafts[note.id]||'';
    note.placeholder='例：Unityでの実行検証は未実施として残し、コード整合の確認と次の担当へ進める。';label.append(note);
    const submit=el('button','未完了項目を残して次の担当へ進む','primary');submit.type='submit';message.setAttribute('role','status');
    handoffForm.append(el('h3','この担当を引き継ぎ終了する'),el('p','現在の報告を保存し、未完了項目を合格扱いせず次へ進めます。入力した範囲は後続の担当にも渡します。'),label,message,submit);body.append(handoffForm);
    handoffForm.addEventListener('submit',async event=>{event.preventDefault();if(submit.disabled)return;submit.disabled=true;
      try{await api('/api/tasks/handoff',{id:task.id,note:note.value});await refresh();}
      catch(error){message.textContent=error.message;}finally{submit.disabled=false;}});
  }
  if(['queued','failed','interrupted','blocked'].includes(task.status)&&!['accepted','accepted_with_pending_checks','cancelled'].includes(job.status))body.append(button('担当・モデルを切り替える',()=>openModelSwitch(task.id)));
  for(const a of endedJob(task)?[]:pending(task.id)){
    if(a.payload.questions?.length){body.append(questionForm(a));continue;}
    const box=el('section',undefined,'approval-box');box.append(el('h3',a.kind==='plan'?'作業計画を承認':a.kind==='completion'?'成果を確認して受け入れる':'操作の承認'),el('div',a.kind==='tool'?JSON.stringify(a.payload,null,2):reportText(a.payload),'text-block'));
    const input=el('textarea');input.id='note-'+a.id;input.rows=3;input.placeholder='回答・判断の理由（任意）';input.setAttribute('aria-label','承認または拒否の補足');input.value=drafts[input.id]||'';
    box.append(el('p',a.kind==='tool'?'この操作1回だけに適用されます。期限: '+date(a.expires_at):'内容を確認して承認または拒否してください。','hint'),input);
    const actions=el('div',undefined,'actions');
    actions.append(button('拒否する',()=>api('/api/decide',{id:a.id,allow:false,note:input.value}),'danger'),button(a.kind==='completion'?(a.payload.pending_items?.length?'未完了項目を残して成果を受領':'確認済みとして受け入れる'):'承認する',()=>api('/api/decide',{id:a.id,allow:true,note:input.value}),'primary'));box.append(actions);body.append(box);
  }
  const history=state.approvals.filter(a=>a.task_id===task.id&&a.status!=='pending');
  if(history.length){const historySection=section('判断の履歴',history.map(a=>`${date(a.created_at)} ${a.kind} / ${a.status}\n${a.note||''}`).join('\n\n'));historySection.id='decision-history';body.append(historySection);}
  const addFact=el('details',undefined,'detail-section'),factSummary=el('summary','引き継ぎ情報を登録・訂正する');
  const factForm=el('form'),factMessage=el('p','','hint');factMessage.setAttribute('role','status');
  const factKey=el('input'),factValue=el('textarea'),factEvidence=el('input');
  factKey.required=true;factKey.maxLength=160;factValue.required=true;factValue.maxLength=2000;factValue.rows=3;factEvidence.maxLength=500;
  function factField(name,input){const label=el('label',name);label.append(input);factForm.append(label);}
  factField('項目（例：保存時の確認動作）',factKey);factField('現在の正しい内容',factValue);factField('根拠・確認した場所（任意）',factEvidence);
  factForm.append(el('p','この依頼の次の担当へ渡します。パスワードや秘密情報は登録しないでください。','hint'),factMessage);
  const factSubmit=el('button','DBに保存','primary');factSubmit.type='submit';factForm.append(factSubmit);
  addFact.append(factSummary,factForm);body.append(addFact);
  const handoff=el('section',undefined,'detail-section');handoff.dataset.task=task.id;
  handoff.append(el('h3','引き継ぎDBの履歴'),el('p','読み込み中…','hint'));body.append(handoff);
  showHandoffHistory(job.id,handoff);
  factForm.addEventListener('submit',async event=>{
    event.preventDefault();factSubmit.disabled=true;factMessage.textContent='保存中…';
    try{await api('/api/handoff/fact',{job_id:job.id,key:factKey.value,value:factValue.value,evidence:factEvidence.value});
      factMessage.textContent='保存しました。';factForm.reset();await showHandoffHistory(job.id,handoff);}
    catch(error){factMessage.textContent=error.message;}finally{factSubmit.disabled=false;}
  });
  if(!endedJob(task)&&['failed','interrupted','blocked','cancelled'].includes(task.status)&&!pending(task.id).some(a=>a.payload.questions?.length)){
    const s=el('section',undefined,'detail-section'),input=el('textarea');input.id='retry-note';input.rows=4;input.placeholder='ここに回答・追加の作業指示、または解決した条件を入力してください。';input.value=drafts[input.id]||'';input.setAttribute('aria-label','回答・再開の補足');
    const label=el('label','回答・補足の入力欄');label.htmlFor=input.id;
    s.append(el('h3','回答・補足を入力して再開'),label,input,el('p','入力は再開する担当へ渡します。システムエラーの場合は、原因を解消してから再開してください。','hint'),button('回答・補足を送って再試行',async()=>{if(confirm('既に行われた編集や処理を確認しましたか？ 同じ依頼が再実行されます。'))await api('/api/retry',{id:task.id,note:input.value,checked_changes:true});}));body.append(s);
  }
  if(!['accepted','accepted_with_pending_checks','cancelled'].includes(job.status))body.append(button('この依頼全体を中止',async()=>{if(confirm('実行中・待機中の作業を中止します。既存の変更は自動では戻しません。'))await api('/api/cancel',{id:job.id});},'danger'));
  body.querySelectorAll('textarea').forEach(t=>{if(drafts[t.id])t.value=drafts[t.id];});
  foldDetailSections(body,task);
}
function openTask(id){selected=id;detailVersion='';const t=state.tasks.find(t=>t.id===id);renderDetail(t,'');if(!$('detail-dialog').open)$('detail-dialog').showModal();}
async function refresh(){try{state=await api('/api/state');render();notifyDecisions();}catch(e){$('connection').textContent='接続できません';notice('接続が切れました。サーバーの状態を確認し、画面を再読み込みしてください。 '+e.message);}}
const decisionNotificationObjects=new Map(),decisionPopupObjects=new Map();
let seenDecisions=new Set();
try{seenDecisions=new Set(JSON.parse(localStorage.getItem('agent-team-decision-seen')||'[]'));}catch(error){}
function notificationSetting(){try{return localStorage.getItem('agent-team-pc-notifications')==='on';}catch(error){return false;}}
let voiceEnabled=false,voiceUtterance=null;
try{voiceEnabled=localStorage.getItem('agent-team-voice-notifications')==='on';}catch(error){}
function voiceSupported(){return 'speechSynthesis' in window&&'SpeechSynthesisUtterance' in window;}
function voiceButton(){
  $('decision-voice').disabled=$('decision-voice-preview').disabled=!voiceSupported();
  $('decision-voice').textContent=!voiceSupported()?'音声通知は非対応':voiceEnabled?'音声通知：有効（クリックで停止）':'音声通知を有効にする';
  $('decision-voice').setAttribute('aria-pressed',String(voiceEnabled));
}
function decisionHelp(task){
  const box=el('section',undefined,'decision-help'),toggle=el('button','回答方法のヘルプ'),content=el('div');toggle.type='button';toggle.id='task-help-toggle';content.id='task-help-content';content.hidden=true;
  toggle.setAttribute('aria-expanded','false');toggle.setAttribute('aria-controls',content.id);
  const requests=pending(task.id),hasChoices=requests.some(a=>a.payload.questions?.length),internalError=task.status==='failed'&&/未完了項目の引き継ぎには利用者の範囲指定/.test(task.summary||'');
  let text,target;
  if(hasChoices){text='「判断が必要です」の各選択肢を選び、必要な補足を入力して「まとめて回答して続行」を押してください。';target='.approval-box';}
  else if(requests.length){text='「操作の承認」または成果の確認欄で回答します。補足はその欄の入力欄に記入し、承認・受領または拒否を選んでください。';target='.approval-box';}
  else if(stopReason(task)){const reason=stopReason(task);text=reason.title+'。'+reason.detail;target='#task-recovery';}
  else if(pythonFailure(task)){text='Pythonの起動失敗で停止しています。「停止理由と次の操作」の「原因を確認して回答案を作る」で統括側の起動確認を行い、結果に応じた回答案を取得してください。工程間の移管欄は使いません。';target='#recovery-advice';}
  else if(task.recovery_advice?.code==='task_completion'){text='担当工程の完了報告を、未完了作業の移管として返したため停止しています。「停止理由と次の操作」の回答案を入力して再報告してください。移管欄は使いません。';target='#recovery-advice';}
  else if(internalError){text='統括が引き継ぎ範囲を処理できず停止しています。下の「ボードから復旧する」で、後続工程に移す必須作業と担当を指定してください。既に登録済みなら、同じ範囲を追加せず下の再試行で再開できます。';target='#task-recovery';}
  else{text='選択式の質問は届いていません。担当に追加情報や指示を渡す場合は、下の「回答・補足の入力欄」へ記入し、「回答・補足を送って再試行」を押してください。失敗原因が認証・権限・実行環境の場合は、その原因を先に解消してください。';target='#retry-note';}
  content.append(el('p',text),el('p','「引き継ぎ情報を登録・訂正する」は事実をDBに保存する欄です。そこへ入力しても、質問への回答や担当の再開にはなりません。','hint'));
  const jump=el('button',internalError||stopReason(task)?'復旧操作へ移動':'回答する場所へ移動');jump.type='button';jump.onclick=()=>{const destination=$('detail-body').querySelector(target);if(destination){revealSection(destination);destination.scrollIntoView({block:'center'});if(destination.matches('textarea,input'))destination.focus();else{destination.tabIndex=-1;destination.focus();}}};content.append(jump);
  toggle.onclick=()=>{content.hidden=!content.hidden;toggle.setAttribute('aria-expanded',String(!content.hidden));};box.append(toggle,content);return box;
}
function speakDecision(preview=false,message='作業の判断事項が発生しました'){
  if(!voiceSupported()||(!preview&&!voiceEnabled))return;
  const synth=window.speechSynthesis;
  if(!preview&&(synth.speaking||synth.pending))return;
  if(preview)synth.cancel();
  const japanese=synth.getVoices().filter(v=>/^ja(?:-|_)/i.test(v.lang)||v.lang==='ja');
  const preferred=japanese.find(v=>v.localService&&/ichiro|keita|男性/i.test(v.name))||japanese.find(v=>/ichiro|keita|男性/i.test(v.name))||japanese.find(v=>v.localService)||japanese[0];
  const utterance=new SpeechSynthesisUtterance(message);
  utterance.lang='ja-JP';utterance.pitch=0.65;utterance.rate=0.85;utterance.volume=0.85;
  if(preferred)utterance.voice=preferred;
  voiceUtterance=utterance;
  utterance.onend=()=>{if(voiceUtterance===utterance)voiceUtterance=null;};
  utterance.onerror=event=>{
    if(voiceUtterance===utterance)voiceUtterance=null;
    if(!['canceled','interrupted'].includes(event.error))notice('音声を再生できませんでした。「声を試聴」を押し、端末の音量も確認してください。判断は画面内の通知から確認できます。');
  };
  try{synth.speak(utterance);}catch(error){notice('音声を再生できません。画面内の通知を使ってください。');voiceUtterance=null;}
}
function notificationButton(){
  const supported='Notification' in window&&window.isSecureContext;
  $('decision-notifications').textContent=!supported?'画面内で判断を通知':notificationSetting()&&Notification.permission==='granted'?'PC通知：有効（クリックで停止）':'PC通知を有効にする';
  $('decision-notifications').disabled=!supported;
  voiceButton();
}
function decisionItems(){
  return state.tasks.filter(t=>!endedJob(t)).flatMap(task=>{
    const approvals=pending(task.id).filter(a=>a.kind!=='tool'||a.payload.questions?.length);
    if(approvals.length)return [{task,key:approvals.map(a=>a.id).sort().join(':'),label:'判断・確認をお願いします'}];
    if(['blocked','failed','interrupted'].includes(task.status)){const reason=stopReason(task);return [{task,key:`${task.id}:${task.attempt}:${task.status}:${reason?.code||'generic'}`,label:reason?.title||(task.status==='blocked'?'判断が必要です':'作業が停止しています'),detail:reason?.detail,voice:reason?.title}];}
    return [];
  });
}
function openNotifiedTask(id){
  const task=state?.tasks.find(t=>t.id===id);
  if(!task||endedJob(task)){notice('この依頼は終了しています。');return;}
  const other=[...document.querySelectorAll('dialog[open]')].some(d=>d.id!=='detail-dialog');
  if(other){notice('開いている入力画面を閉じてから、作業カードを選択してください。');return;}
  openTask(id);
}
function notifyDecisions(){
  const items=decisionItems(),active=new Set(items.map(item=>item.key));
  document.title=items.length?`判断待ち ${items.length}件 — 采来 — サイクル —`:'采来 — サイクル — — 作業と判断';
  notificationButton();
  for(const [key,popup] of decisionPopupObjects)if(!active.has(key)){popup.remove();decisionPopupObjects.delete(key);}
  for(const [key,n] of decisionNotificationObjects)if(!active.has(key)){n.close();decisionNotificationObjects.delete(key);}
  let newDecisions=0,newVoice=null;
  for(const item of items){
    try{for(const key of JSON.parse(localStorage.getItem('agent-team-decision-seen')||'[]'))seenDecisions.add(key);}catch(error){}
    if(seenDecisions.has(item.key))continue;
    newDecisions++;
    if(item.voice&&!newVoice)newVoice=item.voice;
    seenDecisions.add(item.key);
    seenDecisions=new Set([...seenDecisions].slice(-300));
    try{localStorage.setItem('agent-team-decision-seen',JSON.stringify([...seenDecisions]));}catch(error){}
    const popup=el('section',undefined,'decision-popup'),title=el('strong',item.label),description=el('p',item.task.title),actions=el('div',undefined,'actions');
    const open=el('button','内容を確認','primary'),later=el('button','通知を閉じる');open.type=later.type='button';
    open.onclick=()=>{openNotifiedTask(item.task.id);popup.remove();decisionPopupObjects.delete(item.key);};
    later.onclick=()=>{popup.remove();decisionPopupObjects.delete(item.key);};
    actions.append(open,later);popup.append(title,description);
    if(item.detail)popup.append(el('p',item.detail));
    popup.append(el('small','通知を閉じても、作業の停止状態は続きます。'),actions);
    $('decision-popups').append(popup);decisionPopupObjects.set(item.key,popup);
    if('Notification' in window&&notificationSetting()&&Notification.permission==='granted'){
      try{
        const n=new Notification('采来 — サイクル —：'+item.label,{body:item.detail||'作業ダッシュボードを開いて判断してください。',tag:'agent-team-'+item.task.id});
        decisionNotificationObjects.set(item.key,n);
        n.onclick=()=>{window.focus();openNotifiedTask(item.task.id);n.close();};
      }catch(error){notice('PC通知を表示できませんでした。画面内の通知から確認できます。');}
    }
  }
  if(newDecisions)speakDecision(false,newVoice||'作業の判断事項が発生しました');
}
$('decision-voice').addEventListener('click',()=>{
  voiceEnabled=!voiceEnabled;
  try{localStorage.setItem('agent-team-voice-notifications',voiceEnabled?'on':'off');}catch(error){}
  voiceButton();
  if(voiceEnabled){speakDecision(true);notice('音声通知を有効にしました。新しい判断待ちを低めの声で読み上げます。');}
  else{if(voiceSupported())speechSynthesis.cancel();notice('音声通知を停止しました。');}
});
$('decision-voice-preview').addEventListener('click',()=>speakDecision(true));
$('decision-notifications').addEventListener('click',async()=>{
  if(!('Notification' in window))return;
  try{
    if(notificationSetting()&&Notification.permission==='granted'){
      localStorage.setItem('agent-team-pc-notifications','off');notice('PC通知を停止しました。画面内の通知は続きます。');
    }else{
      const permission=await Notification.requestPermission();
      localStorage.setItem('agent-team-pc-notifications',permission==='granted'?'on':'off');
      notice(permission==='granted'?'今後の判断待ちをPCにも通知します。':'PC通知は許可されていません。画面内の通知を使います。');
    }
  }catch(error){notice('このブラウザではPC通知を有効にできません。画面内の通知を使います。');}
  notificationButton();
});
$('filter').addEventListener('change',render);
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
$('new-job').addEventListener('click',()=>{if(!state)return;$('project-select').replaceChildren(...state.config.approved_roots.map(p=>new Option(p,p)));$('new-dialog').showModal();});
let folderState=null, folderBusy=false, folderGeneration=0;
function folderControls(loading){
  folderBusy=loading;
  $('folder-dialog').setAttribute('aria-busy',String(loading));
  $('folder-up').disabled=loading||!folderState?.parent;
  $('folder-use').disabled=loading||!folderState?.selectable;
  $('folder-create-submit').disabled=loading||!folderState;
  $('folder-path-form').querySelector('button').disabled=loading;
  $('folder-list').querySelectorAll('button').forEach(b=>b.disabled=loading);
}
async function folderAction(fn){
  if(folderBusy)return;
  folderControls(true);$('folder-message').textContent='読み込み中…';
  try{await fn();}catch(e){$('folder-message').textContent=e.message;if(folderState)$('folder-path').value=folderState.path;}finally{folderControls(false);}
}
async function loadFolder(path){
  const generation=folderGeneration;
  const result=await api('/api/folders/list',{path});
  if(generation!==folderGeneration||!$('folder-dialog').open)return;
  folderState=result;$('folder-path').value=result.path;
  $('folder-list').replaceChildren(...result.entries.map(entry=>{
    const b=el('button',entry.name+'  ›','folder-entry');b.type='button';
    b.addEventListener('click',()=>folderAction(()=>loadFolder(entry.path)));return b;
  }));
  if(!result.entries.length)$('folder-list').append(el('p','子フォルダはありません。','hint'));
  $('folder-message').textContent=result.selectable?'このフォルダを対象に選択できます。':'中にある個別プロジェクトを選択してください。';
}
function openFolders(create){
  if(folderBusy)return;
  folderGeneration++;folderState=null;
  $('folder-title').textContent=create?'新規プロジェクトのフォルダを作成':'既存フォルダを選択';
  $('folder-create-form').hidden=!create;$('folder-name').value='';
  $('folder-list').replaceChildren();$('folder-dialog').showModal();
  folderAction(()=>loadFolder('C:\\Projects'));
}
$('choose-folder').addEventListener('click',()=>openFolders(false));
$('new-folder').addEventListener('click',()=>openFolders(true));
$('folder-dialog').addEventListener('close',()=>{folderGeneration++;});
$('folder-path-form').addEventListener('submit',e=>{e.preventDefault();folderAction(()=>loadFolder($('folder-path').value));});
$('folder-up').addEventListener('click',()=>{if(folderState?.parent)folderAction(()=>loadFolder(folderState.parent));});
$('folder-create-form').addEventListener('submit',e=>{
  e.preventDefault();if(!folderState)return;
  folderAction(async()=>{
    const result=await api('/api/folders/create',{parent:folderState.path,name:$('folder-name').value});
    $('project-notice').textContent='作成したフォルダ: '+result.path;
    await loadFolder(result.path);$('folder-name').value='';
    if($('folder-dialog').open){$('folder-message').textContent='作成しました。「このフォルダを選択」で依頼に使用できます。';$('folder-use').disabled=false;$('folder-use').focus();}
  });
});
$('folder-use').addEventListener('click',()=>{
  if(!folderState?.selectable)return;
  folderAction(async()=>{
    const result=await api('/api/projects',{path:folderState.path,consent:true});
    const select=$('project-select');
    if(![...select.options].some(o=>o.value===result.path))select.add(new Option(result.path,result.path));
    select.value=result.path;$('project-notice').textContent='対象フォルダ: '+result.path;
    $('folder-dialog').close();select.focus();await refresh();
  });
});
$('job-form').addEventListener('submit',e=>{e.preventDefault();const f=new FormData(e.target);action(async()=>{await api('/api/jobs',{title:f.get('title'),goal:f.get('goal'),project:f.get('project'),auto_execute:f.has('auto_execute'),consent:f.has('consent')});$('new-dialog').close();e.target.reset();notice('依頼を受け付けました。計画の作成を始めます。');});});
$('pause').addEventListener('click',()=>action(()=>api('/api/pause',{paused:!state.paused})));
$('stop').addEventListener('click',()=>action(async()=>{if(confirm('全ての実行中の依頼を中止し、新規着手を停止しますか？'))await api('/api/stop',{});}));
$('backup').addEventListener('click',()=>action(async()=>{const r=await api('/api/backup',{});notice('DBのバックアップを保存しました: data/'+r.file+'、data/'+r.handoff_file);}));
let modelCatalog={}, catalogLoading=false;
function modelSaveState(){
  $('models-save').disabled=catalogLoading||Object.keys(roles).some(role=>{
    const m=document.querySelector(`[name="${role}-model"]`);
    return !m?.value||m.selectedOptions[0]?.disabled;
  });
}
function fillModelOptions(role,preferred){
  const provider=document.querySelector(`[name="${role}-adapter"]`).value;
  const select=document.querySelector(`[name="${role}-model"]`);
  const rows=modelCatalog[provider]?.models||[];
  select.replaceChildren(new Option(rows.length?'モデルを選択してください':'モデル一覧を取得できません',''));
  for(const m of rows)select.add(new Option(`${m.label} — ${m.id}`,m.id));
  if(preferred&&rows.some(m=>m.id===preferred))select.value=preferred;
  else if(preferred){const old=new Option(`現在の設定: ${preferred}（候補にありません）`,preferred);old.disabled=true;select.add(old);select.value=preferred;}
  else select.value='';
  select.disabled=catalogLoading||!rows.length;
  fillEfforts(role);modelSaveState();
}
function fillEfforts(role){
  const provider=document.querySelector(`[name="${role}-adapter"]`).value;
  const model=document.querySelector(`[name="${role}-model"]`).value;
  const effort=document.querySelector(`[name="${role}-effort"]`),old=effort.value||'medium';
  const options=modelCatalog[provider]?.models?.find(m=>m.id===model)?.efforts||[];
  effort.replaceChildren(...options.map(v=>new Option(v,v)));effort.disabled=!options.length;
  if(options.includes(old))effort.value=old;else if(options.includes('medium'))effort.value='medium';
}
async function loadModelCatalog(force=false){
  if(catalogLoading)return;
  catalogLoading=true;modelSaveState();$('models-reload').disabled=true;
  $('models-message').textContent='Codex・Claudeのモデル一覧を取得しています…';
  try{
    const results=await Promise.allSettled(['codex','claude'].map(adapter=>api('/api/models',{adapter,refresh:force})));
    ['codex','claude'].forEach((name,i)=>{modelCatalog[name]=results[i].status==='fulfilled'?results[i].value:{models:[],note:'一覧を取得できませんでした。'};});
    $('models-message').textContent=['codex','claude'].map(name=>`${name==='codex'?'Codex':'Claude'}: ${modelCatalog[name].models.length}件。${modelCatalog[name].models.length?'':modelCatalog[name].note}`).join(' ');
  }finally{
    catalogLoading=false;$('models-reload').disabled=false;
    for(const role of Object.keys(roles)){const select=document.querySelector(`[name="${role}-model"]`);if(select)fillModelOptions(role,select.value);}
    modelSaveState();
  }
}
const decisionSettings=el('details');
const decisionSummary=el('summary','判断Provider');decisionSettings.append(decisionSummary);
const decisionProvider=el('select');decisionProvider.setAttribute('aria-label','判断Provider');
for(const [value,label] of [['ollama','ローカルOllamaのみ'],['disabled','判断層を停止'],['auto','自動（ローカル優先・クラウド候補あり）'],['luna','Luna API（別途従量課金）'],['decisions','Decisions API（現在未対応）']])decisionProvider.append(new Option(label,value));
const decisionModel=el('select');decisionModel.setAttribute('aria-label','Ollama判断モデル');
decisionModel.append(new Option('Tev1 0.8B','tev1:0.8b'),new Option('Tev1 4B（未導入なら別途導入が必要）','tev1:4b'));
const shadowLabel=el('label'),decisionShadow=el('input');decisionShadow.type='checkbox';decisionShadow.checked=true;decisionShadow.disabled=true;
shadowLabel.append(decisionShadow,document.createTextNode(' 開発Agent選択は既存担当を維持（受付分類は実適用）'));
const decisionMessage=el('p');decisionMessage.setAttribute('role','status');decisionMessage.setAttribute('aria-live','polite');
decisionSettings.append(el('p','依頼の一次分類と操作テストの軽い選択に限定して使用します。複雑・曖昧・接続失敗は通常モデルへ渡します。開発Agentの切り替えは行わず、重要な承認は人が行います。','hint'),decisionProvider,decisionModel,shadowLabel,
  el('p','Luna APIは別料金です。選択だけではAPI認証や課金許可を有効化しません。Decisions APIは未対応です。モデルの自動ダウンロードは行いません。','hint'));
const decisionSave=el('button','判断設定を保存');decisionSave.type='button';
decisionSave.addEventListener('click',async()=>{decisionSave.disabled=true;decisionMessage.textContent='保存中…';try{await api('/api/decision-settings',{provider:decisionProvider.value,model:decisionModel.value,shadow:true});await refresh();decisionMessage.textContent='保存しました。次に開始する作業から使用します。';}catch(e){decisionMessage.textContent=e.message;}finally{decisionSave.disabled=false;}});
const decisionCheck=el('button','保存済み設定で接続確認');decisionCheck.type='button';
decisionCheck.addEventListener('click',async()=>{decisionCheck.disabled=true;decisionMessage.textContent='接続確認中…';try{const d=await api('/api/decision-check',{});decisionMessage.textContent=d.status==='ok'?`確認成功：${d.provider}、${d.latency_ms} ms`:`確認失敗：${d.reason_code}。Ollamaの起動とモデルの導入を確認してください。`;}catch(e){decisionMessage.textContent=e.message;}finally{decisionCheck.disabled=false;}});
decisionSettings.append(decisionSave,decisionCheck,decisionMessage);$('settings-dialog').querySelector('.cli-update').before(decisionSettings);
$('settings-open').addEventListener('click',()=>{
  const d=state?.config.decision||{provider:'ollama',model:'tev1:0.8b',shadow:true};decisionProvider.value=d.provider;decisionModel.value=d.model;decisionMessage.textContent='';decisionSummary.textContent='判断Provider：'+decisionProvider.selectedOptions[0].textContent;decisionSettings.open=false;
  if(!state)return;$('profile-inputs').replaceChildren(el('p',state.config.automatic_operations?'操作の承認：自動。仕様・方針の質問は利用者に確認します。':'操作の承認：個別確認（既知の読み取りは自動）。','hint'));
  $('profile-inputs').append(el('p','共通Agentを自動起動：計画・調査はcommon-explorer、実装はcommon-implementer、レビューはcommon-reviewer。セキュリティに関わる変更ではcommon-security-reviewerも順番に実行します。Agent名・セッションIDは各カードの詳細で確認できます。'));
  for(const role of Object.keys(roles)){
    const p=state.config.profiles[state.config.roles[role]],row=el('div',undefined,'profile-row');row.append(el('span',roles[role]));
    const provider=el('select');provider.name=role+'-adapter';provider.setAttribute('aria-label',roles[role]+'の担当');provider.append(new Option('Codex','codex'),new Option('Claude','claude'));provider.value=p.adapter;
    const model=el('select');model.name=role+'-model';model.required=true;model.setAttribute('aria-label',roles[role]+'のモデル');model.append(new Option(p.model,p.model));model.disabled=true;
    const effort=el('select');effort.name=role+'-effort';effort.setAttribute('aria-label',roles[role]+'の推論設定');effort.append(new Option('low','low'),new Option('medium','medium'));effort.value=p.effort;
    provider.addEventListener('change',()=>fillModelOptions(role,''));
    model.addEventListener('change',()=>{fillEfforts(role);modelSaveState();});
    row.append(provider,model,effort);$('profile-inputs').append(row);
  }$('settings-dialog').showModal();loadModelCatalog();
});
const claudeUpdateSection=el('section',undefined,'cli-update');
claudeUpdateSection.append(el('h3','Claude Code CLIの更新'));
const claudeUpdateStatus=el('p','更新情報を確認しています。');claudeUpdateStatus.id='claude-update-status';claudeUpdateStatus.setAttribute('role','status');claudeUpdateStatus.setAttribute('aria-live','polite');
const claudeUpdateChecked=el('p',undefined,'hint');claudeUpdateChecked.id='claude-update-checked';
const claudeUpdateActions=el('div',undefined,'folder-actions');
const claudeUpdateCheck=el('button','今すぐ確認');claudeUpdateCheck.id='claude-update-check';claudeUpdateCheck.type='button';
const claudeUpdateInstall=el('button','Claude Code CLIを更新','primary');claudeUpdateInstall.id='claude-update-install';claudeUpdateInstall.type='button';claudeUpdateInstall.disabled=true;
claudeUpdateActions.append(claudeUpdateCheck,claudeUpdateInstall);
claudeUpdateSection.append(claudeUpdateStatus,claudeUpdateChecked,claudeUpdateActions,
  el('p','起動時と24時間ごとに確認します。更新はボタンを押した場合だけ実行します。現在のネイティブ版CLIに公式の更新コマンドを使い、更新後の版数を確かめます。','hint'));
$('settings-dialog').querySelector('.cli-update').after(claudeUpdateSection);
function foldCliUpdate(section,title,id){
  const fold=el('details',undefined,'cli-update-fold');fold.id=id;
  const summary=el('summary'),status=el('span','確認中','cli-update-badge');
  summary.append(el('strong',title),status);
  section.querySelector('h3')?.remove();
  section.before(fold);fold.append(summary,section);
}
foldCliUpdate($('settings-dialog').querySelector('.cli-update'),'Codex CLIの更新','codex-update-fold');
foldCliUpdate(claudeUpdateSection,'Claude Code CLIの更新','claude-update-fold');
$('models-reload').addEventListener('click',()=>loadModelCatalog(true));
$('cli-update-check').addEventListener('click',async()=>{try{await api('/api/cli-update/check',{});await refresh();}catch(e){notice(e.message);}});
$('cli-update-install').addEventListener('click',async()=>{
  const version=state?.cli_update?.latest;
  if(!version||!confirm(`Codex CLIを${version}へ更新します。実行中の作業がある場合は開始できません。更新中は新規着手を止め、完了後に元の状態へ戻します。続けますか？`))return;
  try{await api('/api/cli-update/install',{version});await refresh();}catch(e){notice(e.message);}
});
$('claude-update-check').addEventListener('click',async()=>{try{await api('/api/claude-update/check',{});await refresh();}catch(e){notice(e.message);}});
$('claude-update-install').addEventListener('click',async()=>{
  const version=state?.claude_update?.latest;
  if(!version||!confirm(`Claude Code CLIの更新を実行します。公開版の目安は${version}です。実行中の作業がある場合は開始できません。続けますか？`))return;
  try{await api('/api/claude-update/install',{version});await refresh();}catch(e){notice(e.message);}
});
$('profiles-form').addEventListener('submit',e=>{
  e.preventDefault();if(catalogLoading||$('models-save').disabled)return;
  const f=new FormData(e.target),data={};
  for(const role of Object.keys(roles))data[role]={adapter:f.get(role+'-adapter'),model:f.get(role+'-model'),effort:f.get(role+'-effort')};
  $('models-save').disabled=true;
  api('/api/profiles',data).then(async()=>{await refresh();notice('今後作成するタスクのモデル設定を保存しました。');$('settings-dialog').close();}).catch(e=>{$('models-message').textContent=e.message;}).finally(modelSaveState);
});
$('project-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{const f=new FormData(e.target);await api('/api/projects',{path:f.get('path'),consent:f.has('consent')});notice('対象プロジェクトを登録しました。');e.target.reset();$('settings-dialog').close();});});
refresh();setInterval(refresh,2500);

async function mobileAccess(){
  try{
    const response=await fetch('/api/mobile',{credentials:'same-origin',cache:'no-store'});
    if(!response.ok)return; // The phone gateway never exposes desktop connection credentials.
    const info=await response.json(),section=$('mobile-access');
    section.hidden=false;
    if(!info.available){$('mobile-url').textContent='Wi-Fiの接続を確認中';$('mobile-warning').textContent=info.error||'接続先を準備中です。';return;}
    $('mobile-url').href=info.url;$('mobile-url').textContent=info.url;
    $('mobile-code').textContent=info.code;
    $('mobile-fingerprint').textContent=info.fingerprint.match(/.{1,2}/g).join(':').toUpperCase();
    $('mobile-warning').textContent='PCとスマホが同じ自宅Wi-Fiにあるときだけ使えます。合言葉を他の人に渡さないでください。';
  }catch(e){$('mobile-access').hidden=false;$('mobile-warning').textContent='スマホ接続の情報を読み込めませんでした。';}
}
mobileAccess();setInterval(mobileAccess,30000);

function openModelSwitch(taskId,source){
  const candidates=state.tasks.filter(t=>['queued','failed','interrupted','blocked'].includes(t.status)&&(!source||t.profile.adapter===source)&&!['accepted','accepted_with_pending_checks','cancelled'].includes(state.jobs.find(j=>j.id===t.job_id)?.status));
  const dialog=el('dialog'),form=el('form'),head=el('div',undefined,'dialog-title'),close=el('button','×');close.type='button';close.setAttribute('aria-label','モデル切替を閉じる');close.onclick=()=>dialog.close();head.append(el('h2','担当・モデルを切り替える'),close);form.append(head);
  form.append(el('p','共通の利用枠が尽きた場合は、同じAI内のモデル変更だけでは再開できないことがあります。ClaudeからCodexへの切り替えも選べます。','hint'));
  const target=el('select'),provider=el('select'),model=el('select'),effort=el('select'),message=el('p','','hint');message.setAttribute('role','status');
  function field(title,input){const label=el('label',title);label.append(input);form.append(label);}
  target.required=model.required=true;
  for(const t of candidates){const job=state.jobs.find(j=>j.id===t.job_id);target.add(new Option(`${job.title} / ${t.title}（${statuses[t.status]}）`,t.id));}
  if(taskId)target.value=taskId;
  provider.append(new Option('Codex','codex'),new Option('Claude','claude'));
  field('切り替えるタスク',target);field('切替先の担当',provider);field('切替先のモデル',model);field('推論設定',effort);
  const include=el('input');include.type='checkbox';const incLabel=el('label',undefined,'check');incLabel.append(include,el('span','同じ依頼・同じ役割の後続待機タスクと、今後の修正担当にも適用'));form.append(incLabel);
  const checked=el('input');checked.type='checkbox';const ckLabel=el('label',undefined,'check');ckLabel.append(checked,el('span','実施済みの変更を確認しました。回答済みの内容と作業指示を引き継ぎ、再実行してよい'));form.append(ckLabel);
  const autoReturn=el('input');autoReturn.type='checkbox';autoReturn.checked=true;
  const returnLabel=el('label',undefined,'check');returnLabel.append(autoReturn,el('span','利用枠不足による一時切り替え：回復後はチーム設定のモデルに自動で戻す'));form.append(returnLabel);
  form.append(el('p','自動復帰は短期・週間・該当する専用枠が各10％以上と確認できた場合です。不明な間は現在のモデルを継続します。質問待ちはモデルのみ戻し、回答待ちを維持します。','hint'));
  form.append(el('p','実行中のタスクは一覧に出ません。質問・承認待ちの場合は「切替のみ保存」後に回答してください。チーム全体の既定モデルは変更しません。','hint'));
  const reload=el('button','モデル一覧を再取得');reload.type='button';
  const actions=el('div',undefined,'actions'),save=el('button','切替のみ保存'),resume=el('button','切替えて再開','primary'),restore=el('button','チーム設定に戻す');save.type=resume.type=restore.type='button';actions.append(restore,save,resume);form.append(reload,message,actions);dialog.append(form);document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();
  let rows=[],loading=false,sending=false,generation=0;
  const current=()=>candidates.find(t=>t.id===target.value);
  function enabled(){restore.disabled=sending||!current();reload.disabled=loading||sending||!current();save.disabled=loading||sending||!current()||!model.value||!effort.value;resume.disabled=save.disabled||!checked.checked||pending(target.value).length>0;}
  function efforts(){effort.replaceChildren(...(rows.find(m=>m.id===model.value)?.efforts||[]).map(e=>new Option(e,e)));if([...effort.options].some(o=>o.value==='medium'))effort.value='medium';enabled();}
  async function load(force=false){const seq=++generation;loading=true;rows=[];model.replaceChildren();effort.replaceChildren();enabled();message.textContent='モデル一覧を取得中…';try{const data=await api('/api/models',{adapter:provider.value,refresh:force===true});if(seq!==generation||!dialog.open)return;rows=data.models;model.replaceChildren(new Option('モデルを選択',''),...rows.map(m=>new Option(`${m.label} — ${m.id}`,m.id)));message.textContent=data.note;efforts();}catch(e){if(seq===generation&&dialog.open)message.textContent=e.message+' 「モデル一覧を再取得」でやり直せます。';}finally{if(seq===generation){loading=false;enabled();}}}
  function targetChanged(){checked.checked=false;include.checked=false;autoReturn.checked=true;const t=current();provider.value=t?.profile.adapter==='claude'?'codex':'claude';load();}
  reload.onclick=()=>load(true);provider.onchange=()=>load();target.onchange=targetChanged;model.onchange=efforts;checked.onchange=enabled;
  async function send(restart){
    if(save.disabled||(restart&&resume.disabled))return;sending=true;enabled();
    try{const result=await api('/api/tasks/model',{id:target.value,profile:{adapter:provider.value,model:model.value,effort:effort.value},include_waiting:include.checked,resume:restart,checked_changes:checked.checked,auto_return:autoReturn.checked});dialog.close();await refresh();notice(`${result.changed}件の担当モデルを変更しました。${restart?'再開を待機しています。':'質問や作業状態は保持しています。'}`);}
    catch(e){message.textContent=e.message;}finally{sending=false;enabled();}
  }
  save.onclick=()=>send(false);resume.onclick=()=>send(true);form.onsubmit=e=>e.preventDefault();
  restore.onclick=async()=>{if(restore.disabled)return;sending=true;enabled();try{
    const result=await api('/api/tasks/restore',{id:target.value,include_waiting:include.checked});dialog.close();await refresh();notice(`${result.changed}件をチーム設定に戻しました。作業状態と回答待ちは維持しています。`);
  }catch(error){message.textContent=error.message;}finally{sending=false;enabled();}};
  if(candidates.length)targetChanged();else{message.textContent='切替可能な停止・待機タスクはありません。新規タスクのモデルは「チーム設定」で変更できます。';enabled();}
}
