'use strict';
let state = null, selected = null, detailVersion = '', busy = false, providerVersion = '';
const $ = id => document.getElementById(id);
const jobImages = new ImageAttachments(document.getElementById('job-attachments'));
const jobSkills = new SkillPicker(document.getElementById('job-skills'));
const roles = {planner:'計画',researcher:'調査',builder:'実装',reviewer:'レビュー'};
const statuses = {queued:'待機',running:'実行中',awaiting_approval:'承認待ち',succeeded:'担当作業終了',handed_off:'未完了項目を残して引き継ぎ済み',failed:'失敗',cancelled:'中止',interrupted:'中断',blocked:'判断が必要'};
const jobStatuses = {...statuses,planning:'計画中',awaiting_acceptance:'成果の確認待ち',accepted:'利用者が確認済み',accepted_with_pending_checks:'成果を受領・未完了項目あり'};
function el(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
function notice(text) { $('notice').textContent=text; }
function waitingReason(task){
 if(task.status!=='queued')return '';
 const job=state.jobs.find(j=>j.id===task.job_id),prior=state.tasks.find(t=>t.id===task.after);
 if(job?.status==='blocked'){
  if(prior?.result?.status==='needs_changes')return '直前のレビューに未解決の指摘があります。自動修正上限で依頼が停止中のため、修正後の再レビューを待っています。';
  return '依頼全体が停止中です。停止した担当の理由を確認してください。';
 }
 if(state.paused)return '新規着手を一時停止しています。';
 if(prior&&!['succeeded','handed_off'].includes(prior.status))return '前の作業が完了するまで待っています。';
 return '';
}
function reviewRepairProposal(review,job){
 const artifact=job.artifacts?.[0]?.path;
 const findings=(review.result?.summary||'').trim();
 const shown=findings.length<=2400?findings:findings.slice(0,2400)+'\n（表示上限です。最新レビュー全文は担当に別途渡されます。）';
 if(job.document_source)return `${artifact?'修正対象は '+artifact+' です。この依頼で作った資料は同じ名前で作り直せます（旧版は .saikuru-versions/ に退避されます）。':'修正対象の資料を確認し、この依頼の範囲外のファイルは変更しないでください。'}
【直す内容】
次の最新レビューの指摘を、資料の本文・構成・表・図に反映してください。
${shown}
【残す内容】
指摘のない部分（構成・表現・デザイン）はそのまま残してください。作り直す前に最新の資料を read_document で読み、変える箇所と残す箇所を確認してください。
【確認すること】
指摘ごとに、直したページ・節と内容を対応づけて報告してください。作り直した資料を read_document で読み、指摘が反映されていることと、以前に直した指摘が戻っていないことを確かめてください。見た目（配色・レイアウト）の確認は受け入れ時に利用者が行うため、未確認として残してかまいません。確認していないことを確認済みとは報告しないでください。`;
 return `${artifact?'修正対象は '+artifact+' だけです。':'修正対象と許可範囲を確認し、範囲外の変更は行わないでください。'}
【変更箇所・修正方針】
次の最新レビューの指摘を、設計・対応表・受入条件・確認方法へ反映してください。具体的な遷移・音声ID・終了条件の修正案が指摘にある場合は、それを落とさず扱ってください。
${shown}
【維持する動作】
変更前に、指摘が参照する既存コードの前後と関連経路、最新成果物、過去の修正結果を読み取り、維持する動作を明示してください。前回報告だけを根拠に推測しないでください。新しい仕様が必要なら確定せずUNKNOWNとして残してください。
【確認条件】
指摘ごとに変更箇所・維持した動作・根拠ファイルと行・計画上の検証項目を対応づけて報告してください。修正後は、元の依頼と計画にある検証（テスト・完了条件のコマンドなど）を現行版でやり直し、実行した内容と結果を記録してください。過去の解消済み指摘が再発していないことも確認してください。
依頼範囲外の変更、モデル取得・更新、公開は行わないでください。未実施の検証を実施済みとは報告しないでください。`;
}
function reviewResolutionHint(review,job){
 const box=el('div',undefined,'stop-reason-box');
 box.append(el('strong','原因：レビュー指摘が未解決のため、依頼が停止しています。'));
 const items=(review.result?.summary||'').split('\n').filter(line=>/^\s*(?:\d+[.．、]|【(?:高|中|低|重大)】)/.test(line)).slice(0,5);
 box.append(el('p','最新レビューで報告された指摘（抜粋）：'));
 if(items.length){const list=el('ul');for(const item of items)list.append(el('li',item.length>420?item.slice(0,420)+'…':item));box.append(list);}
 else box.append(el('p',(review.result?.summary||'指摘本文を確認してください。').slice(0,900)));
 box.append(el('strong','解消のヒント'),el('p','① 指摘ごとの修正対象を確認 → ② 下の修正指示案を確認・編集 → ③「この範囲で修正を依頼」。修正後は通常レビューと必要なセキュリティレビューへ進みます。'));
 if(job.artifacts?.length)box.append(el('p','先に「この依頼の成果物」で最新版を読めます。修正指示には対象文書・維持する条件・変更禁止範囲を残してください。'));
 box.append(el('p','読み取り不可・未測定の指摘は、取得できない理由と後続の確認方法を明記します。未確認を確認済みに置き換えないでください。'));
 return box;
}
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
// 2026-10-03：自動修正の上限で依頼が止まった場合、最後のレビュー工程は「担当作業終了」のまま依頼だけが判断待ちになる。
// この工程を「あなたの判断」と判断待ちバナーに出す（表示の判定のみ。作業の処理は変えない）。
function stalledReview(job){
  if(job?.status!=='blocked')return null;
  const tasks=state.tasks.filter(t=>t.job_id===job.id);
  if(tasks.some(t=>pending(t.id).length||['failed','blocked','interrupted','running','awaiting_approval'].includes(t.status)))return null;
  return tasks.filter(t=>t.role==='reviewer'&&t.status==='succeeded'&&t.result?.status==='needs_changes')
    .sort((a,b)=>(a.finished_at||a.updated_at||0)-(b.finished_at||b.updated_at||0)).pop()||null;
}
function isStalledReview(task){return stalledReview(state.jobs.find(j=>j.id===task.job_id))?.id===task.id;}
// 停止理由の欄の先頭に出す、止まった原因の一文（担当の報告の前に、何が止めたのかを示す）。
function stopCause(task,job){
  if(isStalledReview(task))return `原因：自動修正の上限（${state.config?.max_repairs??'設定'}回）に達しました。最後のレビューが、まだ修正・確認を求めています（下の報告）。`;
  if(pending(task.id).length)return '原因：担当が利用者の回答・承認を待っています。';
  if(task.status==='blocked')return '原因：担当が停止を報告しました（下の報告）。';
  if(task.status==='failed')return '原因：担当の実行が失敗しました（下の報告）。';
  if(task.status==='interrupted')return '原因：担当の実行が中断されました。';
  return '原因：依頼が停止しています（'+(jobStatuses[job.status]||job.status)+'）。';
}
function group(task) { if(endedJob(task))return 3; if(isStalledReview(task))return 2; if(pending(task.id).length||['failed','blocked','interrupted'].includes(task.status))return 2; if(task.status==='queued')return 0; if(['running','awaiting_approval'].includes(task.status))return 1; return 3; }
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
// 2026-10-08 GitHub Copilot追加：使用するプロバイダを設定で選び、選んだものだけを表示する。
const providerLabels={codex:'Codex',claude:'Claude Code',copilot:'GitHub Copilot'};
const providerShortLabels={codex:'Codex',claude:'Claude',copilot:'Copilot'};
const copilotRoles=['planner','reviewer','builder'];
function enabledProviders(){return state?.config?.provider_settings?.enabled||['codex','claude'];}
function usageWindow(row){return row?.monthly_only?row?.monthly:row?.weekly_only?row?.weekly:row?.short;}
function quotaView(name) {
  const usage=state.usage?.[name],box=el('section',undefined,'quota');
  box.setAttribute('aria-label',providerLabels[name]+'の利用枠の残量');
  const models=[...new Set(Object.values(state.config.roles).map(id=>state.config.profiles[id]).filter(p=>p?.adapter===name).map(p=>p.model))];
  box.append(el('p','設定モデル: '+(models.join(' / ')||'未設定'),'quota-models'));
  const percent=w=>typeof w?.remaining_percent==='number'?`${w.remaining_percent}％`:'不明';
  const rows=usage?.rows?.length?usage.rows:[{label:'利用枠',short:null,weekly:null}];
  for(const row of rows){
    const item=el('div',undefined,'quota-row');
    const period=row.minutes?(row.minutes%60===0?`${row.minutes/60}時間枠`:`${row.minutes}分枠`):'';
    item.append(el('small',`${row.label}${period?' · '+period:''}`));
    const line=el('div',undefined,'quota-value');
    const credits=v=>typeof v==='number'?v.toLocaleString('ja-JP',{maximumFractionDigits:2}):'未設定';
    if(row.monthly_only)line.append(el('strong',`残り ${percent(row.monthly)}`),el('span',`（使用 ${credits(row.monthly?.used)} ／ 月間上限 ${credits(row.monthly?.limit)} クレジット）`));
    else if(row.weekly_only)line.append(el('span',`（週間残り ${percent(row.weekly)}）`));
    else line.append(el('strong',`残り ${percent(row.short)}`),el('span',`（週間残り ${percent(row.weekly)}）`));
    item.append(line);
    if(row.monthly_only&&row.monthly?.premium_requests)item.append(el('small',`今月のプレミアムリクエスト：${row.monthly.premium_requests}回`,'quota-note'));
    if(usage?.stale&&usage.updated_at)item.append(el('small','前回取得値・最新値は未確認','quota-warning'));
    const meterWindow=usageWindow(row);
    if(typeof meterWindow?.remaining_percent==='number'){const meter=el('meter');meter.min=0;meter.max=100;meter.low=20;meter.high=50;meter.optimum=100;meter.value=meterWindow.remaining_percent;meter.setAttribute('aria-label',row.label+'の残量');item.append(meter);}
    const resets=[];for(const [key,label] of [['short','短期'],['weekly','週間'],['monthly','月間']])if(row[key]?.resets_at)resets.push(`${label}リセット: ${new Date(row[key].resets_at*1000).toLocaleString('ja-JP')}`);
    if(resets.length)item.append(el('small',resets.join(' / '),'quota-reset'));
    box.append(item);
  }
  box.append(el('small',usage?.note||'利用枠を取得中…','quota-note'));
  if(usage?.updated_at)box.append(el('small',(name==='copilot'?'最終記録: ':'取得: ')+new Date(usage.updated_at*1000).toLocaleString('ja-JP'),'quota-note'));
  if(usage?.status==='unavailable'&&usage.next_update_at)box.append(el('small','次回取得: '+new Date(usage.next_update_at*1000).toLocaleTimeString('ja-JP'),'quota-note'));
  box.append(el('small',name==='copilot'?'采来での消費と、設定した月間上限から計算した値です。':'アカウントの利用枠です。会話のコンテキスト残量とは異なります。','quota-note'));
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
  const copilot=state.copilot_update||{},copilotNow=copilot.installed||'不明',copilotLatest=copilot.latest||'不明';
  badge('copilot-update-fold',copilot);
  $('copilot-update-status').textContent=copilot.state==='available'?`更新あり：${copilotNow} → ${copilotLatest}。${copilot.message}`:
    `現在 ${copilotNow}／公開版 ${copilotLatest}。${copilot.message||'更新情報を確認しています。'}`;
  $('copilot-update-checked').textContent=copilot.checked_at?'最終確認: '+new Date(copilot.checked_at*1000).toLocaleString('ja-JP'):'';
  $('copilot-update-check').disabled=['checking','updating'].includes(copilot.state);
  $('copilot-update-install').disabled=copilot.state!=='available';
  $('copilot-update-install').textContent=copilot.state==='updating'?'更新中…':`GitHub Copilot CLIを${copilot.state==='available'?copilotLatest+'へ':''}更新`;
  // Only the CLIs of the providers in use. A CLI that is updating stays visible until it finishes.
  for(const [name,id,update] of [['codex','codex-update-fold',info],['claude','claude-update-fold',claude],['copilot','copilot-update-fold',copilot]])
    $(id).hidden=!enabledProviders().includes(name)&&update.state!=='updating';
}
// 2026-10-03 UI改善：判断待ちを最上部の常設バナーに表示する（浮いた通知は廃止）。PC通知・音声通知は従来どおり新しい判断だけに出す。
function renderDecisionBanner(items){
  const banner=$('decision-banner'),list=$('decision-banner-list');
  banner.hidden=!items.length;if(!items.length){list.replaceChildren();return;}
  $('decision-banner-title').textContent=`あなたの判断が必要：${items.length}件`;
  list.replaceChildren(...items.map(item=>{
    const li=el('li'),what=el('div',undefined,'what'),job=state.jobs.find(j=>j.id===item.task.job_id);
    what.append(el('strong',item.label),el('span',(job?.title?job.title+' ／ ':'')+item.task.title));
    if(item.detail)what.append(el('small',item.detail));
    if(item.completion)what.append(el('small','工程終了は実機検証済みの保証ではありません。成果物と検証結果を確認してください。'));
    const open=el('button','内容を確認','primary');open.type='button';open.onclick=()=>openNotifiedTask(item.task.id);
    li.append(what,open);return li;
  }));
}
// 2026-10-03 UI改善：ヘッダーに利用枠の残量を常に表示する。取得できない値は「不明」とする。
function renderUsageChips(){
  const box=$('usage-chips');if(!box)return;
  box.replaceChildren(...enabledProviders().filter(name=>state.providers?.[name]).map(name=>{
    const usage=state.usage?.[name],row=usage?.rows?.[0],w=usageWindow(row);
    const value=typeof w?.remaining_percent==='number'?w.remaining_percent:null,label=providerShortLabels[name];
    const chip=el('button',undefined,'usage-chip');chip.type='button';
    const dot=el('span',undefined,'dot'+(value===null?' unknown':value<20?' warn':''));dot.setAttribute('aria-hidden','true');
    chip.append(dot,document.createTextNode(`${label} 残り${value===null?'不明':value+'％'}${usage?.stale?'（前回値）':''}`));
    chip.setAttribute('aria-label',`${label}の利用枠：残り${value===null?'不明':value+'パーセント'}。詳細を開く`);
    chip.onclick=openModelPanel;return chip;
  }));
}
function openModelPanel(){const panel=document.querySelector('.information-panel');if(!panel)return;panel.open=true;panel.scrollIntoView({behavior:'smooth',block:'start'});panel.querySelector('summary')?.focus();}
function render() {
  $('connection').textContent=state.paused?'新規着手を一時停止中':'● ローカル接続中';
  $('connection').classList.toggle('paused',!!state.paused);
  $('pause').textContent=state.paused?'新規着手を再開':'新規着手を一時停止';
  renderCliUpdate();
  const nextProviderVersion=JSON.stringify([state.providers,state.usage,state.config.roles,state.config.profiles,state.config.provider_settings,state.cli_update,state.claude_update,state.copilot_update]);
  if(nextProviderVersion!==providerVersion){
  providerVersion=nextProviderVersion;
  renderUsageChips();
  $('providers').replaceChildren();
  for(const [name,p] of Object.entries(state.providers).filter(([name])=>enabledProviders().includes(name))){
    const box=el('article',undefined,'provider'), text=el('div');box.append(el('div',{codex:'C',claude:'✳',copilot:'G'}[name],'symbol'));
    text.append(el('h2',providerLabels[name]),el('p',p.available?p.version:'接続の準備が必要'),el('small',p.note),quotaView(name));
    if(name==='copilot')text.append(el('small','月間上限・CLIの更新は「チーム設定」で行います。ログインは端末で copilot login を実行します。'));
    if(name==='codex'&&state.cli_update?.state==='available')text.append(button(`CLI更新あり：${state.cli_update.latest}。設定を開く`,()=>{$('settings-open').click();$('codex-update-fold').open=true;}));
    if(name==='claude'&&state.claude_update?.state==='available')text.append(button(`CLI更新あり：${state.claude_update.latest}。設定を開く`,()=>{$('settings-open').click();$('claude-update-fold').open=true;}));
    if(name==='copilot'&&state.copilot_update?.state==='available')text.append(button(`CLI更新あり：${state.copilot_update.latest}。設定を開く`,()=>{$('settings-open').click();$('copilot-update-fold').open=true;}));
    // button() は処理を action() で包む。ここでさらに action() で包むと、二重押し防止で中の処理が実行されない（2026-10-09 修正）。
    if(name==='codex')text.append(button('利用枠を再取得',async()=>{
      const result=await api('/api/codex-usage/refresh',{});
      notice(result.success?'Codexの利用枠を更新しました。':result.usage.note);
    }));
    if(name==='claude'){
      text.append(el('small','采来専用ログインを使用。通常のClaude Codeとは保存先を分けています。'));
      text.append(button('ログイン状態・残量を再確認',async()=>{await api('/api/claude-auth/refresh',{});}));
      if(p.authenticated!==true){
        text.append(button('サイクルでClaudeにログイン',async()=>{
          const login=await api('/api/claude-auth/login',{});
          if(login.state==='authenticated'){notice('ログイン済みです。');return;}
          notice('ログイン画面を準備しています。このPCのブラウザーに開いたら、ログインを完了してください。');
          watchClaudeLogin();
        }));
        text.append(el('p','ログインはサイクルが動いているPCのブラウザーで行います。通常は保存済み認証を使用します。認証切れの場合だけ再ログインしてください。','hint'));
      }
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
    const tasks=state.tasks.filter(t=>(!filter||t.job_id===filter)&&group(t)===index),column=el('section',undefined,'column'+(index===2&&tasks.length?' attention':''));
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
      const badge=el('span',endedJob(task)?(job.status==='cancelled'?'中止済みの依頼':jobStatuses[job.status]):pending(task.id).length?'確認・承認してください':isStalledReview(task)?'判断が必要（自動修正の上限）':statuses[task.status]||task.status,'badge'+(index===2?' approval':''));
      card.append(badge,el('strong',task.title));
      const wait=waitingReason(task);if(wait)card.append(el('p',wait,'summary'));
      if(task.next_action)card.append(el('p',task.next_action,'summary'));
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

let claudeLoginTimer=null,claudeLoginBrowserShown=null;
function watchClaudeLogin(){
 clearTimeout(claudeLoginTimer);
 claudeLoginTimer=setTimeout(async()=>{
  try{
   const result=await api('/api/claude-auth/login-status',{});
   // 采来がPCの既定ブラウザーでログイン画面を開いたか（URLは画面に出さない）。
   if(result.browser_opened!=null&&result.browser_opened!==claudeLoginBrowserShown){
    claudeLoginBrowserShown=result.browser_opened;
    notice(result.browser_opened?'このPCのブラウザーでClaudeのログイン画面を開きました。ログインを完了すると、ここに結果が出ます。':'ブラウザーを開けませんでした。このPCで既定のブラウザーが設定されているか確認してください。');
   }
   if(result.state==='running'){watchClaudeLogin();return;}
   claudeLoginBrowserShown=null;
   await refresh();
   notice(result.authenticated?'Claudeのログインが完了しました。停止した作業はカードから再試行してください。':'ログインを完了できませんでした。ブラウザーが開かない場合はPCの Login-ClaudeCode.ps1 を使い、ログイン状態を再確認してください。');
  }catch(e){notice(e.message);}
 },2000);
}
function section(title,text) { const s=el('section',undefined,'detail-section');s.append(el('h3',title),el('div',text,'text-block'));return s; }
const detailFolds=new Map();let renderedDetailTask=null;
function revealSection(node){
  for(let parent=node.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
}
function foldDetailSections(body,task){
  const previous=detailFolds.get(task.id)||{signature:null,open:{}};
  const requests=endedJob(task)?[]:pending(task.id);
  // 自動修正の上限では担当の状態は変わらず依頼だけが止まるため、依頼の状態と上限の判定も署名に含める。
  const stalled=isStalledReview(task),jobStatus=state.jobs.find(j=>j.id===task.job_id)?.status||'';
  const signature=JSON.stringify([task.status,task.attempt,endedJob(task),stalled,jobStatus,task.recovery_advice?.code||null,requests.map(a=>[a.id,a.status])]);
  const changed=previous.signature!==signature;
  const stopped=!endedJob(task)&&(stalled||['failed','blocked','interrupted'].includes(task.status));
  const scopeError=/未完了項目の引き継ぎには利用者の範囲指定/.test(task.summary||'');
  const completedReport=task.recovery_advice?.code==='task_completion';
  const focus=requests.length?'.approval-box':(isStalledReview(task)||stopped)?'#decision-guide':null;
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
    // 2026-10-08 停止したタスクの「この担当は失敗しました」（原因）は、閉じたままだと気づきにくいので開いて色を付ける。
    const isFailure=node.id==='failure-card';if(isFailure)fold.classList.add('failure-fold');
    // 停止理由の欄も同じく開いて色を付ける（自動修正の上限・blocked を含む、止まったすべての場合）。
    const isCause=stopped&&node.id==='decision-guide';if(isCause)fold.classList.add('failure-fold','stop-fold');
    fold.open=changed?Boolean(isFocus||(stopped&&(isFailure||isCause))):Boolean(previous.open[key]);
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
  if(result.review_findings?.length)text+='\n\nレビュー指摘（未修正。受け入れるか差し戻すかを決めてください）:\n'+result.review_findings.map(f=>`・${f.reviewer}：${f.summary}`).join('\n');
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
      const skills=el('details');skills.append(el('summary','経験から作成したスキル候補（未有効化）'));
      if(!data.skill_candidates?.length)skills.append(el('p','候補はまだありません。根拠付きの同じ経験が複数依頼に蓄積されるか、人が確認した経験から作成します。'));
      for(const candidate of data.skill_candidates||[]){
        const item=el('details');item.append(el('summary',candidate.description),
          el('p','下書きです。実行担当へ読み込まれません。'),
          el('div',`適用条件: ${candidate.applicability}\n経験: ${candidate.experience}\n根拠: ${candidate.evidence}\n元の依頼: ${candidate.source_jobs.join('、')}\n確認状態: ${candidate.verification==='human_recorded'?'人の記録あり':'AI報告・未検証'}\n正式化前の確認:\n${candidate.review_required.join('\n')}`,'text-block'));
        const download=el('button','SKILL.md候補を保存');download.type='button';
        download.onclick=()=>{
          const quote=value=>JSON.stringify(value);
          const text=`---\nname: ${candidate.name}\ndescription: ${quote(candidate.description)}\n---\n\n# 未確認のスキル候補\n\n有効化前に手順・必要な道具・検証方法・失敗時の対応を確認してください。既存の権限を広げません。\n\n## 適用条件（経験からの抽出）\n${candidate.applicability}\n\n## 経験と手順の素材（未確認の参考データ）\n${candidate.experience}\n\n## 出典\n${candidate.evidence}\n依頼: ${candidate.source_jobs.join(', ')}\n\n## 確認項目\n${candidate.review_required.map(v=>'- '+v).join('\n')}\n`;
          const url=URL.createObjectURL(new Blob([text],{type:'text/markdown;charset=utf-8'})),link=el('a');link.href=url;link.download=candidate.name+'-SKILL.md';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
        };item.append(download);
        const form=el('form'),fields={},message=el('p','','hint');message.setAttribute('role','status');
        const labels={description:'用途の説明',applicability:'適用条件',steps:'具体的な手順',tools:'必要な道具と権限',validation:'検証方法と確認結果',failure:'失敗時の対応・停止条件'};
        for(const [key,title] of Object.entries(labels)){const label=el('label',title),input=el('textarea');input.required=true;input.maxLength=3000;input.rows=3;input.value=key==='description'?candidate.description:key==='applicability'?candidate.applicability:'';label.append(input);fields[key]=input;form.append(label);}
        const reviewed=el('input');reviewed.type='checkbox';reviewed.required=true;
        const consent=el('label',undefined,'check');consent.append(reviewed,document.createTextNode('手順・道具・検証・失敗時の対応を確認し、個人情報や案件固有の秘密を除きました。同じプロジェクトでの自動参照を有効にします。'));
        const submit=el('button','確認した版を登録して有効化','primary');submit.type='submit';form.append(consent,message,submit);
        form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;try{await api('/api/skills/release',{job_id:jobId,candidate_id:candidate.id,reviewed:reviewed.checked,...Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value]))});await showHandoffHistory(jobId,container);}catch(error){message.textContent=error.message;}finally{submit.disabled=false;}};
        item.append(form);skills.append(item);
      }
      container.append(skills);
      const released=el('details');released.append(el('summary','正式化したスキルと過去の版'));
      for(const skill of data.released_skills||[]){const item=el('details');item.append(el('summary',`${skill.active?(skill.source_current?'利用中':'元の経験の再確認待ち'):'利用停止・旧版'}：${skill.description} · ${date(skill.created_at)}`),el('div',`適用条件: ${skill.applicability}\n手順: ${skill.steps}\n道具: ${skill.tools}\n検証: ${skill.validation}\n失敗時: ${skill.failure}`,'text-block'));
        const action=button(skill.active?'このスキルを利用停止':'この版を有効化',async()=>{await api('/api/skills/state',{job_id:jobId,version_id:skill.id,enabled:!skill.active});await showHandoffHistory(jobId,container);});item.append(action);released.append(item);}
      container.append(released);
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
const answerFiles=new Map();
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
    if(approval.payload.analysis){box.append(section('AIの意見：確認結果・違い・影響・推奨案',approval.payload.analysis));if(approval.payload.analysis_checks?.length)box.append(section('確認した根拠',approval.payload.analysis_checks.join('\n')));}
  }
  if(approval.payload.summary){const details=el('details');details.append(el('summary',approval.kind==='context_conflict'?'新旧の内容と根拠を読む':'作業計画と背景を読む'),el('div',approval.payload.summary,'text-block'));box.append(details);}
  const drafts=answerDrafts.get(approval.id)||{};answerDrafts.set(approval.id,drafts);
  // 2026-10-08 レビュー担当の質問では、担当が「何を確認したか」を並べ、利用者が項目ごとに承認し、足りない確認を指示できるようにする。
  const reviewTask=state.tasks.find(t=>t.id===approval.task_id),reviewChecks=reviewTask?.role==='reviewer'?(reviewTask.result?.checks||[]):[];
  let checklist=null;
  if(approval.kind==='question'&&reviewChecks.length){
    const saved=drafts.__checklist||(drafts.__checklist={approved:[],instruction:''});
    const box2=el('fieldset',undefined,'review-checklist');box2.append(el('legend','レビューで確認したこと（承認する項目にチェック）'));
    box2.append(el('p','「確認」の項目は、内容に納得できればチェックを入れてください。チェックしなかった項目は、担当に見直しを求めます。「未確認」「推奨」は担当が確認できなかったこと・提案です。','hint'));
    const kindOf=text=>/^未確認/.test(text)?'unchecked':/^推奨/.test(text)?'advice':/^確認/.test(text)?'checked':'other';
    const labels={checked:'確認済み',unchecked:'未確認',advice:'推奨',other:'その他'};
    const boxes=[];
    reviewChecks.forEach((text,index)=>{
      const kind=kindOf(text),row=el('div',undefined,'review-check review-check-'+kind),body=String(text).replace(/^(確認|未確認|推奨)[：:]\s*/,'');
      if(kind==='checked'){const input=el('input');input.type='checkbox';input.id=`review-check-${approval.id}-${index}`;input.checked=saved.approved.includes(index);
        input.onchange=()=>{saved.approved=boxes.filter(([,b])=>b.checked).map(([i])=>i);};boxes.push([index,input]);
        const label=el('label',undefined,'check'),words=el('span');label.htmlFor=input.id;words.append(el('strong',labels[kind]+'：'),document.createTextNode(body));label.append(input,words);row.append(label);}
      else row.append(el('strong',labels[kind]+'：'),document.createTextNode(body));
      box2.append(row);
    });
    if(boxes.length){const all=el('button','確認済みの項目をすべて承認','link-button');all.type='button';all.onclick=()=>{boxes.forEach(([,b])=>{b.checked=true;});saved.approved=boxes.map(([i])=>i);};box2.append(all);}
    const instructionLabel=el('label','足りない確認・追加の指示（任意）'),instruction=el('textarea');instruction.rows=3;instruction.maxLength=1000;
    instruction.placeholder='例：PDFは本文の個人情報だけ確認すれば十分。操作記録が無いのは資料作成でコマンドを使わないため。';instruction.value=saved.instruction;
    instruction.oninput=()=>{saved.instruction=instruction.value;};instructionLabel.append(instruction);box2.append(instructionLabel);
    box.append(box2);checklist=saved;
  }
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
  let fileDraft=null;
  if(approval.kind!=='tool'){
    fileDraft=answerFiles.get(approval.id);
    if(!fileDraft){const host=el('div');host.id='answer-files-'+approval.id;fileDraft={host,widget:new ImageAttachments(host)};answerFiles.set(approval.id,fileDraft);}
    const files=el('details');files.open=approval.payload.questions.some(q=>/ファイル|素材|画像|PDF|添付/.test(q.text));files.append(el('summary','回答にファイルを添付（ファイルを選択）'),fileDraft.host);form.append(files);
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
      const d=drafts[q.id],option=q.options.find(o=>o.id===d.option_id);let text=d.texts[d.option_id]||'';
      if(!text.trim()&&fileDraft?.widget.files.length&&/ファイル|素材|画像|PDF|添付/.test(q.text))text='添付ファイル: '+fileDraft.widget.files.map(e=>e.file.name).join('、');
      if(!option){message.textContent='未回答の質問があります。すべて選択してください。';return;}
      // レビューの確認結果・指示を書いた場合は、それを入力必須欄の回答として扱う（同じ内容を二度書かせない）。
      if(option.input_required&&!text.trim()&&checklist&&(checklist.approved.length||checklist.instruction.trim()))text='上の「レビューで確認したこと」の承認結果と指示を参照してください。';
      if(option.input_required&&!text.trim()){message.textContent='選択した項目の入力欄を記入してください。';return;}
      answers[q.id]={option_id:option.id,text};
    }
    submit.disabled=true;message.textContent='回答を送信しています…';
    try{const attachment_ids=fileDraft?await fileDraft.widget.upload(api):[];await api('/api/decide',{id:approval.id,allow:true,answers,attachment_ids,...(checklist?{review_checklist:{approved:checklist.approved,instruction:checklist.instruction}}:{})});answerDrafts.delete(approval.id);fileDraft?.widget.clear();answerFiles.delete(approval.id);await refresh();}
    catch(error){message.textContent=error.message;}
    finally{submit.disabled=false;}
  });
  return box;
}
const recoveryDrafts=new Map();
// A failed assignment: say so plainly, explain why, and offer alternatives (display only; actions reuse existing APIs).
function failureDiagnosis(task,job){
  const recovery=task.recovery||{},reported=recovery.reported_summary||'';
  const text=[task.summary||'',reported,JSON.stringify(task.result||{})].join('\n');
  const readOnly=['researcher','planner','reviewer'].includes(task.role)||task.agent_run?.sandbox==='read-only';
  const limit=stopReason(task);
  if(limit)return {cause:limit.title,why:limit.detail,kinds:['model','retry']};
  if(/(コマンド|実行)(の|を)?(実行)?(の)?道具(が|も)?(なく|ない|ありません)|実行の道具がなく|コマンドを実行できません|Bash.{0,12}(使えません|ありません|無い)/.test(text))
    return {cause:'この担当には、必要な道具（コマンドの実行）がありません',
      why:(readOnly?'この担当は読み取り専用の役割（'+(task.role==='researcher'?'調査':task.role)+'）で、':'この担当では、')+'コマンドを実行できません。割り当てられた作業にコマンドが必要だったため、何度再試行しても同じ結果になります。'+(job.document_source?'':'この依頼は開発の依頼として扱われています。資料を作る依頼なら、台帳の「相談」で種類を「資料作成」にすると、資料用の道具で進められます。'),
      kinds:['transfer','skip','redo','cancel']};
  if(recovery.code==='handoff_rejected')
    return {cause:'担当の「できない項目を残して先へ進む」報告を受け付けられませんでした',
      why:'未完了の項目を残して先へ進む範囲は、利用者が決める決まりです。担当が自分で範囲を決めて報告したため、采来は受け付けずに止めました。',
      kinds:['transfer','skip','retry','cancel']};
  // 2026-10-09 AIの報告の形の問題は、利用者が補足を書いても解決しない。書くことはないと示し、ボタン1つで再試行できるようにする。
  if(task.failure_code==='format'||/形式が不正|JSON形式|JSONオブジェクト|配列で返して|形式または大きさ|指定の形と違|項目がそろいません/.test(task.summary||''))
    return {cause:'担当（AI）の報告の形が、決まりに合いませんでした',
      why:'内容：'+(task.summary||'').slice(0,160)+'　AIが返した報告に、項目の欠けや形の違いがありました。作業の内容やあなたの判断の問題ではないので、書き足すことはありません。そのまま再試行すると多くの場合は解消します。繰り返す場合は担当・モデルを切り替えてください。',
      kinds:['rerun','model','cancel']};
  if(pythonFailure(task))return {cause:'Pythonを起動できませんでした',why:'担当の作業環境でPythonの起動に失敗しました。下の「停止理由と次の操作」で起動確認と回答案を確認してください。',kinds:['retry','model']};
  if(task.failure_code==='provider'||/^Claude Code:|AI接続|起動確認を受け取れ/.test(task.summary||''))
    return {cause:'AIとの接続、または担当の報告の形式で失敗しました',why:'内容：'+(task.summary||'').slice(0,240),kinds:['retry','model','cancel']};
  return {cause:'担当が決められた回数で工程を終えられませんでした',why:'内容：'+(task.summary||'（記録なし）').slice(0,240),kinds:['retry','model','transfer','cancel']};
}
function failureCard(task,job){
  const d=failureDiagnosis(task,job),box=el('section',undefined,'failure-card');box.id='failure-card';
  box.append(el('h3','この担当は失敗しました'+(task.attempt?'（試行 '+task.attempt+'回で停止）':'')));
  box.append(el('p','何が起きたか：'+d.cause,'failure-cause'),el('p',d.why));
  const reported=(task.recovery?.reported_summary||'').trim();
  if(reported)box.append(el('p','担当の最後の報告（要約・未検証）：'+reported.slice(0,260)+(reported.length>260?'…':''),'hint'));
  const jump=(selector,label)=>button(label,()=>{const target=document.querySelector(selector);if(target){target.scrollIntoView({behavior:'smooth',block:'center'});const field=target.querySelector?.('textarea')||target;field.focus?.();}});
  const targets=state.tasks.filter(t=>t.job_id===job.id&&t.after===task.id&&t.role==='builder'&&t.status==='queued');
  const options={
    transfer:targets.length?['できない作業を次の担当（'+targets[0].title.slice(0,24)+'）に移して再開する','下の「ボードから復旧する」で、移す作業を書いて保存します。次の担当がコマンドを使える場合に有効です。',jump('#task-recovery','移す作業を書く')]:null,
    skip:['その作業を未実施として残し、先へ進める','下の回答欄に残す項目が入ります。内容を確かめて「回答・補足を送って再試行」を押してください。未実施の項目は合格扱いにはなりません。',
      button('未実施として残す項目を書く',()=>{const input=$('retry-note'),allow=$('retry-handoff');if(!input||!allow)return;
        if(!input.value.trim())input.value='【未実施として残す項目】\n- （例：PDF本文の内容確認、見た目の確認）\n\n上の項目は未実施として残し、残りを完了として報告してよい。';
        allow.checked=true;revealSection(input);input.scrollIntoView({behavior:'smooth',block:'center'});input.focus();})],
    retry:['補足を書いて再試行する','原因を解消したうえで、下の回答欄に補足を書いて再試行します。同じ条件のままでは、同じ結果になります。',jump('#retry-note','回答欄へ')],
    rerun:['そのまま再試行する（書き足すことはありません）','同じ指示のまま、この担当をもう一度動かします。この担当がすでに行った変更はそのまま残ります。',
      button('そのまま再試行する',async()=>{
        if(!confirm('同じ指示のまま、この担当を再試行します。よろしいですか？'))return;
        await api('/api/retry',{id:task.id,checked_changes:true,note:'担当の報告の形が決まりに合わなかったため、同じ指示で再試行します。報告は指定の形で返してください。'});
        notice('再試行しました。担当が動き始めます。');},'primary')],
    redo:['依頼を中止して、出し直す',job.document_source?'依頼の内容を見直して出し直します。':'資料を作る依頼なら、台帳の「相談」で種類を「資料作成」にして出し直します。',jump('#cancel-job','中止ボタンへ')],
    model:['担当・モデルを切り替える','利用枠やモデルの不調が原因の場合に有効です。道具が足りない場合は解消しません。',button('担当・モデルを切り替える',()=>openModelSwitch(task.id))],
    cancel:['この依頼全体を中止する','作業済みの変更は自動では戻しません。',jump('#cancel-job','中止ボタンへ')]};
  const list=el('ol',undefined,'failure-options');
  // 2026-10-08 資料作成では、修正やその失敗で止まった依頼を、修正せずに受け入れ画面へ進められる（指摘は受け入れ画面に残る）。
  if(state.config?.document_review_relaxed && job.document_source&&['failed','blocked','interrupted'].includes(job.status)&&state.tasks.some(t=>t.job_id===job.id&&t.role==='reviewer'&&t.status==='succeeded')){
    const li=el('li');li.append(el('strong','修正せずに受け入れへ進める'),el('p','残っている修正・再レビューを取りやめ、受け入れ画面へ進めます。レビュー指摘は受け入れ画面に表示され、受け入れるか差し戻すかをそこで決められます。','hint'),
      button('受け入れ画面へ進める',async()=>{if(!confirm('修正せずに受け入れ画面へ進めます。残っている修正・再レビューは取りやめます。よろしいですか？'))return;
        try{await api('/api/jobs/accept-without-fix',{id:job.id,confirmed:true});await refresh();notice('受け入れ画面へ進めました。成果物とレビュー指摘を確認してください。');}catch(error){notice(error.message);}},'primary'));
    list.prepend(li);
  }
  for(const kind of d.kinds){const o=options[kind];if(!o)continue;const li=el('li');li.append(el('strong',o[0]),el('p',o[1],'hint'),o[2]);list.append(li);}
  box.append(el('h4','代わりの方法'),list);
  return box;
}
// Page images of PPTX/DOCX/PDF deliverables, rendered by the server when the job reaches acceptance.
function previewPanel(job){
  const box=el('section',undefined,'preview-panel');box.append(el('h4','成果物の見た目（自動で画像化）'));
  const area=el('div');area.append(el('p','読み込み中…','hint'));box.append(area);
  const load=async()=>{
    if(!box.isConnected&&area.dataset.started)return;area.dataset.started='1';
    let data;try{data=await api('/api/jobs/previews?id='+encodeURIComponent(job.id));}catch(error){area.replaceChildren(el('p',error.message,'hint'));return;}
    area.replaceChildren();
    if(!data.files.length){area.append(el('p','画像にできる成果物（PowerPoint・Word・PDF）は見つかりませんでした。','hint'));return;}
    for(const file of data.files){
      const head=el('p',file.file+(file.pages?'（'+file.pages+'ページ）':''),'preview-file');area.append(head);
      if(file.status==='done'){const grid=el('div',undefined,'preview-grid');
        for(let n=1;n<=file.pages;n++){const url='/api/previews/image?job='+job.id+'&key='+file.key+'&page='+n;const link=el('a');link.href=url;link.target='_blank';link.rel='noopener';
          const img=el('img');img.src=url;img.alt=file.file+' '+n+'ページ目';img.loading='lazy';link.append(img,el('span',String(n)));grid.append(link);}
        area.append(grid);}
      else if(file.status==='failed')area.append(el('p','画像にできませんでした：'+(file.error||''),'hint'));
      else area.append(el('p','画像を作成中です（Office の場合、1ファイル20秒ほど）。自動で表示を更新します。','hint'));
    }
    if(data.rendering||data.files.some(f=>f.status==='pending'))setTimeout(()=>{if(box.isConnected)load();},5000);
  };
  setTimeout(load,0);
  return box;
}
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
  const skillMode=job.skill_selection?.mode;
  if(job.evaluation?.mode)body.append(el('p','効果検証用の依頼：モード'+job.evaluation.mode+'（再利用の文脈だけを切り替え）'));
  if(skillMode!=='none'&&job.skill_selection?.names?.length)body.append(el('p','依頼時にプロジェクトへ追加したスキル（候補）: '+job.skill_selection.names.join('、')));
  else if(skillMode==='none')body.append(el('p','この依頼ではスキルを使いません（依頼時の選択）。'));
  if(!endedJob(task)&&group(task)===2){
    const guide=el('section',undefined,'detail-section');guide.id='decision-guide';
    guide.append(el('h3','停止理由・判断材料・次の操作'),el('p',stopCause(task,job),'failure-cause'),el('p',task.summary||task.result?.summary||'下の確認事項と担当の報告を確認してください。'));
    if(task.recovery_advice?.message)guide.append(el('p','統括の確認結果：'+task.recovery_advice.message));
    const choices=el('div',undefined,'actions');
    const jump=(label,target)=>choices.append(button(label,()=>{const dest=body.querySelector(target);if(!dest){notice('対象の操作欄がありません。担当の報告と履歴を確認してください。');return;}revealSection(dest);dest.scrollIntoView({block:'center'});const focus=dest.querySelector('button,textarea,input')||dest;focus.focus();}));
    if(isStalledReview(task))jump('範囲を指定して修正を依頼','#manual-review-repair');
    else if(pending(task.id).length)jump('判断材料を読んで回答','.approval-box');
    else {jump('原因と復旧方法を確認','#recovery-advice');jump('補足・差し戻しを入力','#retry-note');}
    jump('中止の確認へ','#cancel-job');
    choices.append(button('保留して閉じる',()=>{$('detail-dialog').close();notice('保留しました。依頼は停止したままで、判断待ちにも残ります。');}));
    guide.append(choices,el('p','操作を選ぶだけでは承認・再開しません。移動先で内容と変更済みの範囲を確認して送信します。','hint'));body.append(guide);
  }
  if(['queued','cancelled'].includes(task.status)&&job.status==='blocked'){
    const blocker=[...state.tasks].reverse().find(t=>t.job_id===job.id&&t.role==='reviewer'&&t.status==='succeeded'&&t.result?.status==='needs_changes');
    const action=el('div',undefined,'stop-reason-box');
    action.append(el('strong','先にレビュー指摘の修正が必要です'),el('p','このセキュリティレビューを直接開始するのではなく、未解決の指摘を修正し、通常レビュー後に自動で進めます。'));
    if(blocker)action.append(reviewResolutionHint(blocker,job));
    if(blocker)action.append(button('指摘を確認して修正を依頼',()=>{
      openTask(blocker.id);
      const panel=$('manual-review-repair');if(panel){revealSection(panel);panel.scrollIntoView({block:'center'});}
    }));
    body.append(action);
  }
  for(const artifact of job.artifacts||[]){
    const panel=el('section',undefined,'detail-section'),content=el('div',undefined,'text-block');
    panel.append(el('h3','この依頼の成果物'),button(artifact.label+'を読む',async()=>{
      try{const value=await api(`/api/job/artifact?job_id=${encodeURIComponent(job.id)}&path=${encodeURIComponent(artifact.path)}`);content.textContent=value.content;}
      catch(e){content.textContent=e.message;}
    }),content);body.append(panel);
  }
  if(job.status==='blocked'&&task.role==='reviewer'&&task.result?.status==='needs_changes'&&task.status==='succeeded'){
    const panel=el('section',undefined,'detail-section'),note=el('textarea');note.rows=4;note.setAttribute('aria-label','レビュー指摘の修正範囲');
    panel.id='manual-review-repair';
    note.id='manual-review-repair-note';
    const artifact=job.artifacts?.[0]?.path;
    const proposal=reviewRepairProposal(task,job);
    note.value=drafts[note.id]??proposal;
    const artifactPath=el('input');artifactPath.placeholder='成果物の相対パス（任意）：docs/plans/plan.md';artifactPath.value=job.artifacts?.[0]?.path||'';artifactPath.setAttribute('aria-label','確認する既存成果物の相対パス');
    note.placeholder='修正対象、許可する変更、変更しない範囲を入力してください。';
    const check=el('input'),checkLabel=el('label',undefined,'check');check.type='checkbox';checkLabel.append(check,document.createTextNode('最新成果物・変更済みの内容と、入力した修正範囲を確認しました。'));
    panel.append(el('h3','ボード内で指摘を修正する'),reviewResolutionHint(task,job),el('p','修正指示案は編集できます。送信するまで作業は開始しません。修正担当→通常レビュー→必要なセキュリティレビュー→成果確認へ進めます。自動修正の上限は変更しません。'),button('最新指摘から修正指示案を入れ直す',()=>{if(note.value!==proposal&&!confirm('編集中の指示を最新レビューから作った案に置き換えますか？'))return;note.value=proposal;note.dispatchEvent(new Event('input',{bubbles:true}));}),note,artifactPath,checkLabel,button('この範囲で修正を依頼',async()=>{
      if(!note.value.trim()){notice('修正範囲を入力してください。');return;}
      if(!check.checked){notice('最新成果物と修正範囲の確認にチェックを入れてください。');return;}
      if(check.checked){
        try{await api('/api/tasks/repair-review',{id:task.id,note:note.value,artifact_path:artifactPath.value.trim(),checked_changes:true});await refresh();}
        catch(e){notice(e.message);}
      }
    }));body.append(panel);
  }
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
  if(!endedJob(task)&&task.next_action)body.append(el('p','おすすめの次の操作：'+task.next_action,'notice'));
  if(!endedJob(task)&&task.previous_findings&&task.result?.status==='needs_changes')body.append(el('p','修正後も指摘が残っています。変更した箇所と根拠を確認してください。未変更のまま再試行せず、修正または未完了項目を残す引き継ぎを選んでください。','notice'));
  if(!endedJob(task)&&task.status==='failed')body.append(failureCard(task,job));
  if(!endedJob(task)&&group(task)===2)body.append(decisionHelp(task));
  if(!endedJob(task)&&['failed','blocked','interrupted'].includes(task.status))body.append(advicePanel(task));
  if(!endedJob(task)&&['failed','blocked','interrupted'].includes(task.status)&&['researcher','builder'].includes(task.role))body.append(recoveryPanel(task,job));
  if(job.artifact_urls?.length){const box=el('section',undefined,'detail-section');box.append(el('h3','ClaudeのArtifact（claude.ai・デスクトップから送信）'));
    for(const a of job.artifact_urls){const p=el('p');const link=el('a',a.url);link.href=a.url;link.target='_blank';link.rel='noopener noreferrer';p.append(link,` ・送信 ${a.publishes||0}回`);box.append(p);}
    box.append(el('p','共有するかどうかはclaude.ai上で利用者が判断してください。','hint'));body.append(box);}
  if(job.attachment_ids?.length){const images=el('section');images.append(el('h3','依頼の添付ファイル'),ImageAttachments.gallery(job.attachment_ids));body.append(images);}
  const instructionHealth=job.instruction_health;
  if(instructionHealth?.warnings?.length){const warning=el('div',undefined,'notice');warning.append(el('strong','指示ファイルの容量警告（依頼作成時点）'));for(const w of instructionHealth.warnings)warning.append(el('p',w.message));warning.append(el('p',instructionHealth.hint));body.append(warning);}
  body.append(section('今回の作業',task.instruction),section('対象と元の依頼',job.project+'\n\n'+job.goal));
  if(task.instruction_history?.length)body.append(section('過去の指示（履歴・現在の命令ではありません）',task.instruction_history.map(h=>`試行 ${h.attempt} · ${new Date(h.at*1000).toLocaleString()} · ${h.reason}\n${h.instruction}${h.submitted_note?'\n\n当時の追加内容:\n'+h.submitted_note:''}`).join('\n\n────────\n\n')));
  if(task.result)body.append(section('担当の報告（AIによる報告）',reportText(task.result)));
  if(job.status==='blocked'&&!state.tasks.some(t=>t.job_id===job.id&&['queued','running','awaiting_approval'].includes(t.status))){
    const form=el('form',undefined,'detail-section'),label=el('label','成果物への差し戻し理由・修正範囲'),input=el('textarea'),message=el('p','','hint');input.required=true;input.maxLength=4000;input.rows=4;label.append(input);
    const submit=el('button','この指摘で修正とレビューを再開','primary');submit.type='submit';message.setAttribute('role','status');form.append(el('h3','成果物を差し戻す'),el('p','元の許可範囲で修正担当→通常レビュー→セキュリティレビューへ進みます。未回答の質問や矛盾がある場合は先に解消してください。'),label,message,submit);body.append(form);
    form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;try{await api('/api/jobs/rework',{id:job.id,note:input.value});await refresh();}catch(error){message.textContent=error.message;}finally{submit.disabled=false;}};
  }
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
    const box=el('section',undefined,'approval-box');box.append(el('h3',a.kind==='plan'?'作業計画を承認':a.kind==='completion'?'成果を確認して受け入れる':'操作の承認'));
    if(a.kind==='tool'&&a.payload?.guard)box.append(el('p','送信の防御：'+a.payload.guard.label+'。'+a.payload.guard.note+' 送信先・送る内容・依頼に必要かを確認してから許可してください。','notice'));
    box.append(el('div',a.kind==='tool'?JSON.stringify(a.payload,null,2):reportText(a.payload),'text-block'));
    if(a.kind==='completion')box.append(previewPanel(job));
    const input=el('textarea');input.id='note-'+a.id;input.rows=3;input.placeholder='回答・判断の理由（任意）';input.setAttribute('aria-label','承認または拒否の補足');input.value=drafts[input.id]||'';
    box.append(el('p',a.kind==='tool'?'この操作1回だけに適用されます。期限: '+date(a.expires_at):a.kind==='completion'?'内容を確認して受け入れるか拒否してください。拒否する場合は理由・指摘を入力すると、その指摘で修正担当とレビューが自動で再開します（理由なしの拒否は停止）。':'内容を確認して承認または拒否してください。','hint'),input);
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
    // 2026-10-08 「未実施として残して先へ進める」を、回答欄の文章だけでなく利用者の範囲指定として記録できるようにする。
    const scope=el('input');scope.type='checkbox';scope.id='retry-handoff';const scopeLabel=el('label',undefined,'check');
    scopeLabel.append(scope,el('span','この欄に書いた項目は、未実施として残して先へ進めることを許可する（未実施の項目は合格扱いになりません）'));
    const canScope=['builder','researcher'].includes(task.role);
    s.append(el('h3','回答・補足を入力して再開'),label,input,...(canScope?[scopeLabel]:[]),el('p','入力は再開する担当へ渡します。システムエラーの場合は、原因を解消してから再開してください。','hint'),button('回答・補足を送って再試行',async()=>{
      if(scope.checked&&!input.value.trim()){notice('未実施として残す項目を入力欄に書いてください。');input.focus();return;}
      if(confirm(scope.checked?'書いた項目を未実施として残し、先へ進めることを許可して再試行します。よろしいですか？':'既に行われた編集や処理を確認しましたか？ 同じ依頼が再実行されます。'))await api('/api/retry',{id:task.id,note:input.value,checked_changes:true,handoff_allowed:scope.checked});}));body.append(s);
  }
  if(!['accepted','accepted_with_pending_checks','cancelled'].includes(job.status)){const cancel=button('この依頼全体を中止',async()=>{if(confirm('実行中・待機中の作業を中止します。既存の変更は自動では戻しません。'))await api('/api/cancel',{id:job.id});},'danger');cancel.id='cancel-job';body.append(cancel);}
  body.querySelectorAll('textarea').forEach(t=>{if(drafts[t.id])t.value=drafts[t.id];});
  foldDetailSections(body,task);
}
// 2026-10-08 カードを開くたびに折りたたみを初期状態（停止理由を開く）から始める。以前の開閉を引き継ぐと、停止後も閉じたままになっていた。
function openTask(id){selected=id;detailVersion='';detailFolds.delete(id);if(renderedDetailTask===id)renderedDetailTask=null;const t=state.tasks.find(t=>t.id===id);renderDetail(t,'');if(!$('detail-dialog').open)$('detail-dialog').showModal();}
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
  else if(isStalledReview(task)){text='自動修正の上限です。「ボード内で指摘を修正する」で最新指摘と成果物を確認し、許可する修正範囲を送信してください。';target='#manual-review-repair';}
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
  $('service-notifications').textContent=state?.notifications?.windows_enabled?'常駐PC通知：有効（停止する）':'常駐PC通知を有効にする（勤務時間内）';
  $('service-notification-status').textContent=state?.notifications?.error||'常駐通知はブラウザーを閉じても動作します。勤務時間外は次の勤務開始にまとめて通知します。Windowsへのログインが必要です。';
  const supported='Notification' in window&&window.isSecureContext;
  $('decision-notifications').textContent=!supported?'画面内で判断を通知':notificationSetting()&&Notification.permission==='granted'?'PC通知：有効（クリックで停止）':'PC通知を有効にする';
  $('decision-notifications').disabled=!supported;
  voiceButton();
}
function decisionItems(){
  return state.tasks.filter(t=>!endedJob(t)).flatMap(task=>{
    const approvals=pending(task.id);
    const completion=approvals.find(a=>a.kind==='completion');
    if(completion){
      const job=state.jobs.find(j=>j.id===task.job_id);
      const remaining=completion.payload.pending_items?.length;
      return [{task,key:'job-complete:'+completion.id,label:'依頼全体の作業工程が終了しました',
        detail:(job?.title||task.title)+'。'+(remaining?'未完了項目があります。成果物・検証結果・残作業を確認してください。':'成果物と検証結果を確認して受け入れてください。'),
        voice:'依頼全体の作業工程が終了しました。成果を確認してください。',completion:true}];
    }
    if(approvals.length)return [{task,key:approvals.map(a=>a.id).sort().join(':'),label:'判断・確認をお願いします'}];
    if(['blocked','failed','interrupted'].includes(task.status)){const reason=stopReason(task);return [{task,key:`${task.id}:${task.attempt}:${task.status}:${reason?.code||'generic'}`,label:reason?.title||(task.status==='blocked'?'判断が必要です':'作業が停止しています'),detail:reason?.detail,voice:reason?.title}];}
    return [];
  }).concat(state.jobs.filter(job=>!['cancelled','accepted','accepted_with_pending_checks'].includes(job.status)).flatMap(job=>{
    const review=stalledReview(job);
    return review?[{task:review,key:'repair-limit:'+review.id,label:'自動修正の上限に達し、判断が必要です',
      detail:'レビュー指摘が残っています。修正範囲を指定して再依頼するか、成果の扱いを判断してください。',voice:'自動修正の上限に達しました。判断が必要です。'}]:[];
  }));
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
  renderDecisionBanner(items);
  for(const [key,n] of decisionNotificationObjects)if(!active.has(key)){n.close();decisionNotificationObjects.delete(key);}
  const hours=state.config.business_hours||{utc_offset_minutes:540,start:'09:00',end:'18:00',workdays:[0,1,2,3,4],holidays:[]};
  const local=new Date(Date.now()+hours.utc_offset_minutes*60000),clock=local.toISOString().slice(11,16),day=(local.getUTCDay()+6)%7;
  if(!hours.workdays.includes(day)||hours.holidays.includes(local.toISOString().slice(0,10))||clock<hours.start||clock>=hours.end)return;
  let newDecisions=0,newVoice=null;
  for(const item of items){
    try{for(const key of JSON.parse(localStorage.getItem('agent-team-decision-seen')||'[]'))seenDecisions.add(key);}catch(error){}
    if(seenDecisions.has(item.key))continue;
    newDecisions++;
    if(item.voice&&!newVoice)newVoice=item.voice;
    seenDecisions.add(item.key);
    seenDecisions=new Set([...seenDecisions].slice(-300));
    try{localStorage.setItem('agent-team-decision-seen',JSON.stringify([...seenDecisions]));}catch(error){}
    if('Notification' in window&&notificationSetting()&&Notification.permission==='granted'&&!state.notifications?.windows_enabled){
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
$('service-notifications').addEventListener('click',()=>action(async()=>{
  const enabled=!state.notifications?.windows_enabled;
  await api('/api/notifications',{windows_enabled:enabled});
  notice(enabled?'勤務時間内の常駐PC通知を有効にしました。通知本文は件数のみです。':'常駐PC通知を停止しました。画面内の通知は続きます。');
}));
$('service-notification-test').addEventListener('click',()=>action(async()=>{
  notice('Windowsへテスト通知を送っています…');
  const result=await api('/api/notifications/test',{});notice(result.note);
}));
$('filter').addEventListener('change',render);
$('menu-models').addEventListener('click',openModelPanel);
$('menu-mobile').addEventListener('click',()=>{const m=$('mobile-access');if(m.hidden){notice('スマホ接続の情報はまだ読み込まれていません。少し待ってから再度開いてください。');return;}m.open=true;m.scrollIntoView({behavior:'smooth',block:'start'});});
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
let jobInstructionSequence=0;
async function checkJobInstructionSize(){
 const sequence=++jobInstructionSequence,project=$('project-select').value,box=$('job-instruction-health');
 box.replaceChildren(el('p','指示ファイルの容量を確認中…','hint'));
 if(!project){box.replaceChildren(el('p','対象プロジェクトを選ぶと指示ファイルの容量を確認します。','hint'));return;}
 try{const result=await api('/api/instruction-health?project='+encodeURIComponent(project));if(sequence!==jobInstructionSequence)return;box.replaceChildren();for(const w of result.warnings||[])box.append(el('p','指示ファイルの警告：'+w.message,'notice'));box.append(el('p',result.warnings?.length?result.hint:'確認した範囲にサイズ警告はありません。実際の読み込みは未確認です。','hint'));}
 catch(error){if(sequence===jobInstructionSequence)box.replaceChildren(el('p','指示ファイルの容量は未確認です。'+error.message,'notice'));}
}
$('project-select').addEventListener('change',checkJobInstructionSize);
$('new-job').addEventListener('click',()=>{if(!state)return;$('project-select').replaceChildren(...state.config.approved_roots.map(p=>new Option(p,p)));$('new-dialog').showModal();checkJobInstructionSize();jobSkills.load(api);});
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
  folderAction(()=>loadFolder('C:\\AI_Work'));
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
$('job-template')?.addEventListener('click',()=>{const goal=$('job-goal');if(goal.value.trim()&&!confirm('入力済みの内容を記入例で置き換えます。よろしいですか？'))return;goal.value='【目的】\n\n【材料】\n\n【完了の条件】\n\n【決まっていない点は標準で】ファイル名・表現の細部はおまかせ。迷ったら元の依頼を優先し、採用した標準を報告に書く。';goal.focus();});
// Requests for documents get the document tools only through the ledger consultation (kind: documentation).
const DOCUMENT_REQUEST=/PowerPoint|PPTX|パワポ|スライド|プレゼン|説明資料|報告書|Word|DOCX|Excel|XLSX|PDF|動画|MP4|チラシ|ポスター/i;
const syncJobKind=()=>{const docs=$('job-kind').value==='documentation';$('job-document').hidden=!docs;$('job-document-format').required=docs;};
$('job-kind').addEventListener('change',syncJobKind);
// 2026-10-08 依頼フォームの点検：「計画を開始する」で問題があれば、どの項目に不備があるかと解消方法をボタンの上に示す。
// ブラウザ標準の必須チェックは、閉じた欄の項目だと何も表示されないことがあるため使わず、ここでまとめて点検する。
const jobFields={
  title:{label:'依頼名',el:()=>$('job-form').elements.title},
  project:{label:'対象プロジェクト',el:()=>$('project-select')},
  format:{label:'成果物の形式',el:()=>$('job-document-format')},
  goal:{label:'達成したいこと・完了の条件',el:()=>$('job-goal')},
  skills:{label:'スキルをプロジェクトに追加して候補にする',el:()=>$('job-skills')},
  evaluation:{label:'効果検証（検証モード）',el:()=>$('job-evaluation-mode'),open:()=>{$('job-evaluation').open=true;}},
  attachments:{label:'参考ファイルを添付',el:()=>$('job-attachments')},
  consent:{label:'AIへの送信の確認',el:()=>$('job-form').elements.consent},
  other:{label:'送信・接続'}};
$('job-form').noValidate=true;
function checkJobForm(form,f,docs){
  const issues=[],add=(field,problem,fix)=>issues.push({field,problem,fix});
  if(!String(f.get('title')||'').trim())add('title','入力されていません。','依頼の内容が分かる短い名前を入力してください（例：単位変換モジュールの作成）。');
  if(!f.get('project'))add('project','選ばれていません。','一覧から選ぶか、「既存フォルダを選択」「＋ 新規フォルダを作成」で対象を登録してください。');
  if(docs&&!f.get('document_format'))add('format','選ばれていません。','作りたい資料の形式（PowerPoint・PDF・Markdownなど）を選んでください。');
  const goal=String(f.get('goal')||'');
  if(!goal.trim())add('goal','入力されていません。','やってほしいことと完了の条件を書いてください。「記入例を入れる」で書き方の型を入れられます。');
  else if(goal.length>16000)add('goal',`長すぎます（${goal.length.toLocaleString('ja-JP')}文字／上限16,000文字）。`,'要点に絞って短くしてください。詳しい資料は「参考ファイルを添付」で渡せます。');
  let skills=null;try{skills=jobSkills.value();}catch(error){add('skills',error.message,'追加するスキルのチェックを10件以内に減らしてください。');}
  if(f.get('evaluation_mode')){
    if(!f.has('evaluation_confirmed'))add('evaluation','「検証用の課題で、実業務のデータを含みません。」にチェックがありません。','検証をする場合はチェックを入れてください。普段の依頼なら、検証モードを「使わない（普段の依頼）」に戻してください。');
    if(skills&&(skills.skill_mode==='none'||skills.skill_ids.length))add('evaluation','検証モードと、スキルの追加（または「この依頼ではスキルを使わない」）は同時に使えません。','普段の依頼なら、検証モードを「使わない（普段の依頼）」に戻してください。検証をする場合は、スキルの欄のチェックをすべて外してください（スキルの有無は検証モードA〜Dで切り替わります）。');
  }
  if(!f.has('consent'))add('consent','チェックが入っていません。','内容を確認のうえ、「依頼内容・添付ファイルと必要な作業情報をAIに送信し、CLIの利用枠を使うことを確認しました。」にチェックを入れてください。');
  return issues;
}
function jobServerIssue(error){
  const text=String(error?.message||error||'');
  const rules=[
    [/AIへの送信と作業範囲の確認/,'consent','「AIに送信し、CLIの利用枠を使うことを確認しました。」にチェックを入れてください。'],
    [/検証モードは A〜D/,'evaluation','検証モードを選び直してください。普段の依頼なら「使わない（普段の依頼）」です。'],
    [/実業務のデータを含まない/,'evaluation','検証をする場合は確認のチェックを入れ、普段の依頼なら検証モードを「使わない（普段の依頼）」に戻してください。'],
    [/スキル追加と「スキルを使わない」/,'evaluation','普段の依頼なら検証モードを「使わない（普段の依頼）」に戻してください。検証をする場合は、スキルの欄のチェックをすべて外してください。'],
    [/未登録のプロジェクト/,'project','「既存フォルダを選択」で対象フォルダを選び直し、登録してから送信してください。'],
    [/対象フォルダが見つかりません/,'project','フォルダが移動・削除されていないか確認し、対象プロジェクトを選び直してください。'],
    [/アーカイブ・移動処理中|フォルダの復旧/,'project','プロジェクト台帳で処理の完了・フォルダの復旧を確認してから、もう一度送信してください。'],
    [/スキル/,'skills','スキル一覧で状態を確認し、チェックを付け直してください（10件まで）。'],
    [/添付/,'attachments','該当する添付を外すか、内容を見直して添付し直してください。'],
    [/成果物形式|制作ライブラリ|資料生成用Python/,'format','別の形式を選ぶか、管理者に制作環境の準備を依頼してください。'],
    [/が含まれています/,'goal','依頼名・内容から該当する箇所を削除するか伏せ字にしてから、もう一度送信してください。'],
    [/送信を取りやめました/,'goal','内容を見直してから、もう一度「計画を開始する」を押してください。'],
    [/依頼名と内容を入力/,'goal','依頼名と内容を入力してください（内容は16,000文字まで）。'],
    [/画面を再読み込み|403/,'other','采来が再起動された可能性があります。入力内容を控えてから、ページを再読み込みしてもう一度送信してください。'],
    [/Failed to fetch|NetworkError|接続/,'other','采来に接続できません。少し待ってからもう一度送信してください。続く場合は采来が起動しているか確認してください。']];
  const hit=rules.find(([pattern])=>pattern.test(text));
  return hit?{field:hit[1],problem:text,fix:hit[2]}:{field:'other',problem:text||'理由が分からないまま受付できませんでした。',fix:'入力内容を見直して、もう一度送信してください。解消しない場合は、この文面を管理者に伝えてください。'};
}
function clearJobIssues(){
  const box=$('job-form-message');box.hidden=true;box.replaceChildren();
  for(const field of Object.values(jobFields)){const target=field.el?.();if(target){target.removeAttribute('aria-invalid');target.classList.remove('field-error');}}
}
function showJobIssues(issues){
  clearJobIssues();const box=$('job-form-message');
  box.append(el('strong',issues.length>1?`依頼を開始できません。次の${issues.length}件を直してください。`:'依頼を開始できません。次の項目を直してください。'));
  const list=el('ol');
  for(const issue of issues){
    const field=jobFields[issue.field]||jobFields.other,target=field.el?.(),item=el('li');
    item.append(el('b',field.label+'：'),document.createTextNode(issue.problem),el('div','解消方法：'+issue.fix,'form-error-fix'));
    if(target){target.setAttribute('aria-invalid','true');target.classList.add('field-error');
      const jump=el('button','この項目へ移動','link-button');jump.type='button';
      jump.onclick=()=>{field.open?.();target.scrollIntoView({behavior:'smooth',block:'center'});(target.matches('input,select,textarea')?target:target.querySelector('input,select,textarea,button'))?.focus({preventScroll:true});};
      item.append(jump);}
    list.append(item);
  }
  box.append(list);box.hidden=false;box.scrollIntoView({block:'nearest'});
}
$('job-form').addEventListener('input',event=>{event.target.closest('.field-error')?.classList.remove('field-error');});
$('job-form').addEventListener('submit',e=>{e.preventDefault();const f=new FormData(e.target);const docs=f.get('kind')==='documentation';
clearJobIssues();const issues=checkJobForm(e.target,f,docs);if(issues.length){showJobIssues(issues);return;}
if(!docs&&DOCUMENT_REQUEST.test(String(f.get('title'))+' '+String(f.get('goal')))&&!confirm('資料（PowerPoint・Word・PDFなど）を作る依頼のようです。\n\n「依頼の種類」を「資料作成」にすると、図・表付きの資料を作る道具で進められます。開発・作業の依頼では、資料用の道具は使えません。\n\nこのまま開発・作業の依頼として出しますか？（キャンセルで入力に戻ります）'))return;
action(async()=>{try{await submitJob(f,docs,e.target);}catch(error){showJobIssues([jobServerIssue(error)]);throw error;}});});
async function submitJob(f,docs,form){const skills=jobSkills.value();const attachment_ids=await jobImages.upload(api);const created=await sendWithInputGuard(api,'/api/jobs',{title:f.get('title'),goal:f.get('goal'),project:f.get('project'),auto_execute:f.has('auto_execute'),consent:f.has('consent'),attachment_ids,...skills,...(docs?{kind:'documentation',document_format:f.get('document_format')}:{}),...(f.get('evaluation_mode')?{evaluation_mode:f.get('evaluation_mode'),evaluation_confirmed:f.has('evaluation_confirmed')}:{})});$('new-dialog').close();form.reset();syncJobKind();jobImages.clear();jobSkills.reset();notice('依頼を受け付けました。計画の作成を始めます。'+(created.prepared?.length?' '+created.prepared.join(' '):''));}
$('pause').addEventListener('click',()=>action(()=>api('/api/pause',{paused:!state.paused})));
$('stop').addEventListener('click',()=>action(async()=>{if(confirm('全ての実行中の依頼を中止し、新規着手を停止しますか？'))await api('/api/stop',{});}));
$('backup').addEventListener('click',()=>action(async()=>{const r=await api('/api/backup',{});notice('DBのバックアップを保存しました: data/'+r.file+'、data/'+r.handoff_file);}));
let modelCatalog={}, catalogLoading=false;
function modelSaveState(){
  $('models-save').disabled=catalogLoading||!checkedProviders().length||Object.keys(roles).some(role=>{
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
// 2026-10-08 チーム設定の注書き：Copilotの担当範囲と、その安全の仕組みを説明する（2026-10-09 実装担当に対応）。
function openCopilotLimits(){
  const dialog=el('dialog',undefined,'copilot-limits'),head=el('div',undefined,'dialog-title'),close=el('button','×');
  close.type='button';close.setAttribute('aria-label','説明を閉じる');close.onclick=()=>dialog.close();
  head.append(el('h2','GitHub Copilotの担当範囲と安全の仕組み'),close);
  const section=(title,...lines)=>{const box=el('section');box.append(el('h3',title));for(const line of lines)box.append(el('p',line));return box;};
  const list=(...items)=>{const ul=el('ul');for(const item of items)ul.append(el('li',item));return ul;};
  dialog.append(head,
    section('ひとことで言うと',
      '采来は、AIがファイルを書き換える・コマンドを実行する・Webを検索する前に、毎回その操作が安全かを確かめています。GitHub Copilot CLIには、この「実行前の確認」を采来に渡す仕組みがありません。そこで、Copilot自身のファイル操作・コマンド実行・Web接続はすべて禁止し、采来が用意した道具だけを使わせています。'),
    section('「計画」「レビュー」「相談」',
      '采来の読み取り専用の道具（プロジェクト内のファイル一覧・読み取り・検索・資料の読み取り）だけを使います。'),
    section('「実装」（資料作成の依頼）',
      '采来の資料作成用の道具（保存先の中だけ・資料形式だけ）を使います。コマンドは使えません。ClaudeのArtifact形式の資料は対象外です（Claudeに切り替えてください）。'),
    section('「実装」（開発の依頼）',
      '采来の道具（ファイルの書き込み・置き換え・コマンドの実行）を使います。どの操作も、実行の前に采来がClaude・Codexと同じ確認をし、許可したときだけ采来が実行します。'),
    list('対象プロジェクトの外には書き込ませない',
         'AGENTS.md・CLAUDE.md・.env・.git・セキュリティ方針などの設定や認証情報は変更させない',
         '既存ファイルを上書きする前に、元のファイルを退避する',
         '外部への送信・公開・導入のコマンドは、自動承認の設定でも利用者が確認する'),
    el('p','コマンドは Windows PowerShell で実行します。承認を待つあいだにCopilotが要求を取り消した場合は、その後に承認されても実行しません。'),
    section('「調査」を担当できない理由',
      '調査担当は、読み取り用のコマンド実行とWeb検索を使います。Web検索は、采来が検索語を検査してから送る仕組みで、Copilotの検索ではこの検査を通せません。そのため調査は、Codex・Claudeに任せています。'));
  document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();close.focus();
}
function checkedProviders(){return [...document.querySelectorAll('[name="provider-enabled"]:checked')].map(input=>input.value);}
function fillProviderOptions(role){
  const select=document.querySelector(`[name="${role}-adapter"]`),old=select.value;
  const names=checkedProviders().filter(name=>name!=='copilot'||copilotRoles.includes(role));
  select.replaceChildren(...names.map(name=>new Option(providerShortLabels[name],name)));
  if(!names.length)select.add(new Option('使用する担当がありません',''));
  select.disabled=!names.length;
  if(names.includes(old))select.value=old;
  return select.value!==old;
}
function providersChanged(){
  const credits=$('copilot-credits-label');if(credits)credits.hidden=!checkedProviders().includes('copilot');
  for(const role of Object.keys(roles))if(fillProviderOptions(role))fillModelOptions(role,'');
  loadModelCatalog();
}
async function loadModelCatalog(force=false){
  if(catalogLoading)return;
  const names=checkedProviders();
  catalogLoading=true;modelSaveState();$('models-reload').disabled=true;
  $('models-message').textContent=(names.map(name=>providerLabels[name]).join('・')||'選択中のプロバイダ')+'のモデル一覧を取得しています…';
  try{
    const results=await Promise.allSettled(names.map(adapter=>api('/api/models',{adapter,refresh:force})));
    names.forEach((name,i)=>{modelCatalog[name]=results[i].status==='fulfilled'?results[i].value:{models:[],note:'一覧を取得できませんでした。'};});
    $('models-message').textContent=names.length?names.map(name=>`${providerLabels[name]}: ${modelCatalog[name].models.length}件。${modelCatalog[name].models.length?'':modelCatalog[name].note}`).join(' '):'使用するプロバイダを1つ以上選択してください。';
  }finally{
    catalogLoading=false;$('models-reload').disabled=false;
    for(const role of Object.keys(roles)){const select=document.querySelector(`[name="${role}-model"]`);if(select)fillModelOptions(role,select.value);}
    modelSaveState();
    // A provider checked while the list was loading is fetched afterwards.
    if(checkedProviders().some(name=>!modelCatalog[name]))loadModelCatalog();
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
decisionSettings.append(decisionSave,decisionCheck,decisionMessage);
$('settings-open').addEventListener('click',()=>{
  const d=state?.config.decision||{provider:'ollama',model:'tev1:0.8b',shadow:true};decisionProvider.value=d.provider;decisionModel.value=d.model;decisionMessage.textContent='';decisionSummary.textContent='判断Provider：'+decisionProvider.selectedOptions[0].textContent;decisionSettings.open=false;
  if(!state)return;$('profile-inputs').replaceChildren(el('p',state.config.automatic_operations?'操作の承認：自動。仕様・方針の質問は利用者に確認します。':'操作の承認：個別確認（既知の読み取りは自動）。','hint'));
  $('profile-inputs').append(el('p','共通Agentを自動起動：計画・調査はcommon-explorer、実装はcommon-implementer、レビューはcommon-reviewer。セキュリティに関わる変更ではcommon-security-reviewerも順番に実行します。Agent名・セッションIDは各カードの詳細で確認できます。'));
  const providerBox=el('fieldset',undefined,'provider-choice');providerBox.append(el('legend','使用するプロバイダ'));
  for(const name of ['codex','claude','copilot']){
    const label=el('label',undefined,'check'),input=el('input');input.type='checkbox';input.name='provider-enabled';input.value=name;
    input.checked=enabledProviders().includes(name);input.addEventListener('change',providersChanged);
    const available=state.providers?.[name]?.available;
    label.append(input,el('span',providerLabels[name]+(available?'':'（CLI未検出）')));providerBox.append(label);
  }
  const creditsLabel=el('label','GitHub Copilotの月間の最大クレジット'),credits=el('input');creditsLabel.id='copilot-credits-label';
  credits.type='number';credits.name='copilot-credits';credits.min='1';credits.step='any';credits.inputMode='decimal';
  credits.value=state.config.provider_settings?.copilot_monthly_credits??'';credits.placeholder='例：1000';
  creditsLabel.append(credits);creditsLabel.hidden=!enabledProviders().includes('copilot');
  providerBox.append(creditsLabel,el('p','使用しないプロバイダは、担当の選択肢・利用枠表示・残量取得から外れます。GitHub Copilotは計画・実装・レビュー担当と相談で使えます（道具は采来の確認付きのものだけ。調査担当は不可）。残り％は采来から実行した分の消費で計算します。Copilotのモデル名の（約○クレジット/回）は、短い問いかけ1回の実測値です。実際の作業では、読み込む資料の量や回答の長さに応じて数倍〜数十倍になります。','hint'));
  $('profile-inputs').append(providerBox);
  for(const role of Object.keys(roles)){
    const p=state.config.profiles[state.config.roles[role]],row=el('div',undefined,'profile-row');row.append(el('span',roles[role]));
    const provider=el('select');provider.name=role+'-adapter';provider.setAttribute('aria-label',roles[role]+'の担当');provider.append(new Option(providerShortLabels[p.adapter],p.adapter));provider.value=p.adapter;
    const model=el('select');model.name=role+'-model';model.required=true;model.setAttribute('aria-label',roles[role]+'のモデル');model.append(new Option(p.model,p.model));model.disabled=true;
    const effort=el('select');effort.name=role+'-effort';effort.setAttribute('aria-label',roles[role]+'の推論設定');effort.append(new Option('low','low'),new Option('medium','medium'));effort.value=p.effort;
    provider.addEventListener('change',()=>fillModelOptions(role,''));
    model.addEventListener('change',()=>{fillEfforts(role);modelSaveState();});
    row.append(provider,model,effort);$('profile-inputs').append(row);fillProviderOptions(role);
  }
  const limitsNote=el('p',undefined,'hint copilot-limits-note'),limitsLink=el('button','GitHub Copilotの担当範囲と安全の仕組み（「調査」は選べません）','link-button');
  limitsLink.type='button';limitsLink.setAttribute('aria-haspopup','dialog');limitsLink.onclick=openCopilotLimits;
  limitsNote.append(document.createTextNode('※ '),limitsLink);$('profile-inputs').append(limitsNote);
  $('settings-dialog').showModal();loadModelCatalog();
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
// 2026-10-08 設定画面の並び：モデル選択 → 使用中のCLIの更新（Codex／Claude Code／GitHub Copilot）→ 判断Provider。
const copilotUpdateSection=el('section',undefined,'cli-update');
const copilotUpdateStatus=el('p','更新情報を確認しています。');copilotUpdateStatus.id='copilot-update-status';copilotUpdateStatus.setAttribute('role','status');copilotUpdateStatus.setAttribute('aria-live','polite');
const copilotUpdateChecked=el('p',undefined,'hint');copilotUpdateChecked.id='copilot-update-checked';
const copilotUpdateActions=el('div',undefined,'folder-actions');
const copilotUpdateCheck=el('button','今すぐ確認');copilotUpdateCheck.id='copilot-update-check';copilotUpdateCheck.type='button';
const copilotUpdateInstall=el('button','GitHub Copilot CLIを更新','primary');copilotUpdateInstall.id='copilot-update-install';copilotUpdateInstall.type='button';copilotUpdateInstall.disabled=true;
copilotUpdateActions.append(copilotUpdateCheck,copilotUpdateInstall);
copilotUpdateSection.append(copilotUpdateStatus,copilotUpdateChecked,copilotUpdateActions,
  el('p','起動時と24時間ごとにGitHubの公開版を確認します。更新はボタンを押した場合だけ、CLIの公式の更新コマンド（copilot update）で実行します。','hint'));
$('claude-update-fold').after(copilotUpdateSection);
foldCliUpdate(copilotUpdateSection,'GitHub Copilot CLIの更新','copilot-update-fold');
$('copilot-update-fold').after(decisionSettings);
$('models-reload').addEventListener('click',()=>loadModelCatalog(true));
$('cli-update-check').addEventListener('click',async()=>{try{await api('/api/cli-update/check',{});await refresh();}catch(e){notice(e.message);}});
$('cli-update-install').addEventListener('click',async()=>{
  const version=state?.cli_update?.latest;
  if(!version||!confirm(`Codex CLIを${version}へ更新します。実行中の作業がある場合は開始できません。更新中は新規着手を止め、完了後に元の状態へ戻します。続けますか？`))return;
  try{await api('/api/cli-update/install',{version});await refresh();}catch(e){notice(e.message);}
});
$('copilot-update-check').addEventListener('click',async()=>{try{await api('/api/copilot-update/check',{});await refresh();}catch(e){notice(e.message);}});
$('copilot-update-install').addEventListener('click',async()=>{
  const version=state?.copilot_update?.latest;
  if(!version||!confirm(`GitHub Copilot CLIを${version}へ更新します。実行中の作業がある場合は開始できません。続けますか？`))return;
  try{await api('/api/copilot-update/install',{version});await refresh();}catch(e){notice(e.message);}
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
  const credits=String(f.get('copilot-credits')||'').trim();
  data.provider_settings={enabled:checkedProviders(),copilot_monthly_credits:credits?Number(credits):null};
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
  function providerChoices(){const t=candidates.find(c=>c.id===target.value),old=provider.value;
    const names=enabledProviders().filter(name=>name!=='copilot'||copilotRoles.includes(t?.role));
    provider.replaceChildren(...names.map(name=>new Option(providerShortLabels[name],name)));if(names.includes(old))provider.value=old;}
  providerChoices();
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
  function targetChanged(){checked.checked=false;include.checked=false;autoReturn.checked=true;const t=current();providerChoices();
    const other=[...provider.options].map(o=>o.value).find(name=>name!==t?.profile.adapter);if(other)provider.value=other;load();}
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
