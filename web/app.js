'use strict';
let state = null, selected = null, detailVersion = '', busy = false, providerVersion = '';
const $ = id => document.getElementById(id);
const jobImages = new ImageAttachments(document.getElementById('job-attachments'));
const jobSkills = new SkillPicker(document.getElementById('job-skills'));
const roles = {planner:'計画',researcher:'調査',builder:'実装',reviewer:'レビュー'};
const statuses = {queued:'待機',running:'実行中',awaiting_approval:'承認待ち',succeeded:'担当作業終了',handed_off:'未完了項目を残して引き継ぎ済み',failed:'失敗',cancelled:'中止',interrupted:'中断',blocked:'判断が必要'};
const jobStatuses = {...statuses,planning:'計画中',awaiting_acceptance:'成果の確認待ち',accepted:'利用者が確認済み',accepted_with_pending_checks:'成果を受領・未完了項目あり'};
// P0 L2：復旧と保守の状態は、サーバーの p0_interruption と承認の category / intended_role だけで分ける。
// 画面側で障害の種類を推測しない。未知の値は「保守対応待ち」とし、成功や判断待ちとは扱わない。
const P0_PHASES={
  retrying:{key:'retrying',label:'復旧中',detail:'采来が自動で復旧を試みています。完了すると、止まった作業から再開します。あなたの操作は不要です。'},
  maintenance_required:{key:'maintenance_required',label:'保守対応待ち',detail:'作業の成果は保存されています。保守担当が対応します。あなたの操作は不要です。'},
  safety_blocked:{key:'safety_blocked',label:'安全上の確認待ち',detail:'安全のため作業を止めました。作業の成果は保存されています。権限のある担当者の確認が必要です。'}
};
function p0Phase(job){
  if(job?.p0_contract_version!==1)return null;
  const status=job.p0_interruption?.recovery_status;
  if(!status)return null;
  if(status==='retrying')return P0_PHASES.retrying;
  if(job.status!=='blocked')return null;
  return P0_PHASES[status]||P0_PHASES.maintenance_required;
}
// 利用者に判断を求める承認か。category の無い旧形式の承認は従来どおり扱う。保守・技術・改善の項目と、保守担当宛ての項目は含めない。
function userDecision(a){
  if(!a.category)return true;
  if(a.intended_role==='maintainer'||a.intended_role==='none')return false;
  return ['BUSINESS','ACCEPTANCE'].includes(a.category)||(a.category==='RISK'&&a.intended_role==='authorized_person');
}
function jobStatusLabel(job){const phase=p0Phase(job);if(phase)return phase.label;
  // 新形式の依頼が停止していて、利用者に判断を求める項目が無いとき、原因を推測せず「判断が必要」とは表示しない。
  if(job?.p0_contract_version===1&&job.status==='blocked'&&!(state?.approvals||[]).some(a=>a.job_id===job.id&&a.status==='pending'&&userDecision(a)))return '停止中（次の動作をご確認ください）';
  return job?.g2_gate_version===1&&job.g2_status==='pending'?'AIによる計画作成の開始の承認待ち':jobStatuses[job?.status]||job?.status||'不明';}
function el(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
function notice(text) { $('notice').textContent=text; dialogNotice(text); }
// W05.05.UI（F2）：modal のダイアログの間は #notice が裏に隠れ、外側は inert になる。開いているダイアログの中にも同じ文を出す。
function dialogNotice(text){
  const dialog=[...document.querySelectorAll('dialog[open]')].filter(d=>d.id).at(-1);
  if(!dialog)return;
  let box=dialog.querySelector(':scope > .dialog-notice');
  if(!box){
    box=el('p',undefined,'decision-help dialog-notice');box.setAttribute('role','status');box.setAttribute('aria-live','polite');box.hidden=true;
    const title=dialog.querySelector(':scope > .dialog-title');if(title)title.after(box);else dialog.prepend(box);
    dialog.addEventListener('close',()=>{box.textContent='';box.hidden=true;});
  }
  box.textContent=text||'';box.hidden=!text;
}
// P0 L1：標準画面（既定）と技術詳細の切り替え。選択はこのブラウザーにだけ保存し、読めなくても標準で動く。
(()=>{const toggle=$('view-toggle');if(!toggle)return;let technical=false;
  try{technical=localStorage.getItem('saikuru-view')==='technical';}catch(e){}
  const apply=()=>{document.body.dataset.view=technical?'technical':'standard';toggle.setAttribute('aria-pressed',String(technical));toggle.textContent=technical?'技術詳細を隠す':'技術詳細を表示';};
  toggle.addEventListener('click',()=>{technical=!technical;try{localStorage.setItem('saikuru-view',technical?'technical':'standard');}catch(e){}apply();});apply();})();
function waitingReason(task){
 if(task.status!=='queued')return '';
 const job=state.jobs.find(j=>j.id===task.job_id),prior=state.tasks.find(t=>t.id===task.after);
 const phase=p0Phase(job);if(phase)return phase.detail;
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
function date(t) { if(!(t>0))return '日時不明'; return new Date(t*1000).toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit',second:'2-digit'}); }
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
  if(data===undefined||retried)return apiCall(path,data,retried);
  showBusy('送信しています…');
  try{return await apiCall(path,data,retried);}finally{hideBusy();blockNext=true;}
}
async function apiCall(path, data, retried=false) {
  const response=await fetch(path,data===undefined?{credentials:'same-origin',cache:'no-store'}:{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Agent-Team-UI':'1'},body:JSON.stringify(data)});
  const result=await response.json();
  // These exact errors are returned before any operation is performed. Never retry
  // timeouts, network failures, or other responses that could duplicate a mutation.
  if(!retried&&response.status===403&&['画面を再読み込みしてください。','ブラウザからの認証済み操作が必要です。'].includes(result.error)){
    await recoverSession();return api(path,data,true);
  }
  if(!response.ok)throw new Error(workflowErrorText(result)||result.error||'通信に失敗しました。'); return result;
}
// W05.01 の固定の理由コードを、利用者向けの文に置き換える。未知のコードはサーバーの文言のまま出す（推測しない）。
const WORKFLOW_ERROR_LABELS={
  workflow_request_invalid:'送信の形式が一致しません。画面を再読み込みしてから操作してください。',
  workflow_feature_target_changed:'確認する内容が変わりました。再読み込みして、新しい内容で確認してください。',
  workflow_feature_confirmation_required:'確認の内容が一致しないか、期限が切れました。再読み込みしてください。',
  workflow_feature_observation_changed:'確認の途中で状態が変わりました。再読み込みしてください。',
  work_package_revision_changed:'機能の状態が更新されました。再読み込みしてください。',
  work_package_review_already_requested:'この機能の確認はすでに用意されています。再読み込みしてください。',
  workflow_writer_not_stopped:'作業中の担当があります。作業が終わってから操作してください。',
  workflow_pending_decision:'先に、ほかの確認・質問に答えてください。',
  workflow_conflict_review_required:'記録に食い違いがあります。先に食い違いの確認に答えてください。',
  workflow_feature_job_not_running:'この依頼は進行中ではないため、機能の確認はできません。',
  work_package_defect_note_required:'差し戻す理由を入力してください。',
  workflow_sensitive_input_blocked:'入力に送信できない情報が含まれている可能性があります。表現を変えて入力してください。',
  workflow_input_or_maintenance_check_required:'入力内容を確認してください。解決しない場合は保守担当の確認が必要です。',
  workflow_revision_confirmation_not_current:'計画の確認内容が古くなりました。再読み込みして、表示された計画で確認してください。',
  workflow_transfer_not_eligible:'再利用できない機能が選ばれました。画面を再読み込みして、表示された選択肢から選び直してください。',
  workflow_transfer_dependency_not_reused:'先に必要な機能を再利用しない場合は、この機能も再利用できません。再実行を選んでください。',
  workflow_transfer_decision_invalid:'再利用・再実行の選択が正しくありません。画面を再読み込みしてください。',
  workflow_transfer_evidence_changed:'以前の確認の内容が変わっていたため、再利用できませんでした。再読み込みして選び直してください。',
  workflow_control_target_changed:'確認の間に依頼の状態が変わったため、送りませんでした。新しい表示を確かめて、必要ならもう一度操作してください。',
  workflow_control_action_invalid:'この操作は使えません。画面を再読み込みしてください。',
  workflow_control_task_invalid:'対象の作業がこの依頼と一致しません。画面を再読み込みしてください。',
  workflow_control_task_not_current:'この作業は計画の変更で古くなったため、再試行できません。新しい作業の状態を確認してください。',
  workflow_control_confirmation_required:'確認が済んでいないため、送りませんでした。',
  workflow_control_job_not_active:'この依頼は終了しているため、この操作はできません。',
  workflow_local_io_check_required:'作業のフォルダを確認できませんでした。自動では再送しません。保守担当の確認が必要です。',
  workflow_user_action_required:'担当の作業中はこの操作を受け付けません。少し待ってから表示を確かめてください。',
  workflow_version_required:'この依頼の形式では、この操作を使えません。画面を再読み込みしてください。',
  workflow_feature_target_required:'新方式の依頼では、依頼全体ではなく機能ごとに差し戻します。機能の一覧の「この機能を差し戻す」を使ってください。',
  dependency_feature_review_required:'この機能の状態が変わったため、送りませんでした。新しい表示で、確認が必要な機能を確かめてください。',
  work_package_count_over_limit:'一度の計画で扱える作業パッケージは1〜6件です。依頼を段階に分け、今回は最初の段階だけを計画して承認してください。次の段階は、先行段階の成果物を入力にして、あらためて計画・承認します。',
  work_package_state_changed:'機能の状態が変わったため、送りませんでした。新しい表示を確かめてください。',
  work_package_not_current:'この機能は計画の変更で古くなったため、送りませんでした。新しい機能の一覧を確かめてください。'
};
// W05.05.UI：個別の文がない固定のコードは、種類ごとの文で「送っていない・変わっていない」ことを伝える（推測で詳しく書かない）。
const WORKFLOW_ERROR_FAMILIES=[
  [/^dependency_/,'先に必要な機能、またはその確認の内容が変わったため、送りませんでした。新しい表示を確かめてください。'],
  [/^work_package_/,'機能の状態または条件が一致しないため、送りませんでした。新しい表示を確かめてください。'],
  [/^workflow_/,'依頼の状態または確認の内容が一致しないため、送りませんでした。新しい表示を確かめてください。']];
function workflowErrorText(result){
  // 旧来の経路（/api/decide など）は、固定のコードを error に入れて返すことがある。英字のコードをそのまま見せない。
  const raw=typeof result?.code==='string'?result.code:(typeof result?.error==='string'&&/^(?:workflow|work_package|dependency)_[a-z_]+$/.test(result.error)?result.error:'');
  const code=raw;if(!code)return '';
  return WORKFLOW_ERROR_LABELS[code]||(WORKFLOW_ERROR_FAMILIES.find(([re])=>re.test(code))||[])[1]||'';
}
// W05.03：新方式の依頼の中止・再試行。操作を始めたときに現在の対象（版）を読み、利用者の明示の確認の後に1回だけ送る。
// 対象が古くなっていれば送り直さない（新しい表示で確認し直す）。対象は権限ではなく、保守の確認は保守担当だけが行う。
// 旧方式の依頼は従来の経路と文言のまま。
const CONTROL_ONCE_NOTE='\n\n（いま表示している状態に対して1回だけ送ります。その間に状態が変わっていた場合は、送らずにお知らせします。）';
function isWorkflowJob(job){return job?.workflow_contract_version===1;}
function needsMaintenance(task,job){
  const stop=job?.p0_interruption||{};
  return !!(task.recovery_check_required||task.recovery_confirmation
    ||(stop.recovery_status==='maintenance_required'&&(!stop.interrupted_task_id||stop.interrupted_task_id===task.id))
    ||(isWorkflowJob(job)&&job.workflow?.wait_owner==='maintainer'));
}
async function workflowTarget(job,kind,taskId){
  const view=await api('/api/workflow/controls?id='+encodeURIComponent(job.id));
  if(view.contract_version!==1)throw new Error('操作の形式が一致しません。画面を再読み込みしてください。');
  if(kind==='cancel_job')return view.cancel_target;
  const entry=(view.tasks||[]).find(t=>t.task_id===taskId);
  if(!entry)throw new Error('対象の作業が見つかりません。画面を再読み込みしてください。');
  return entry.retry_target;
}
function retryAcceptedText(result){
  if(result.decision_required)return '再試行を受け付けました。始める前にあなたの確認が必要です。確認の欄で内容を見て判断してください。';
  if(result.queued)return '再試行を受け付けました。順番が来ると担当が動き始めます（まだ始まっていません）。';
  return '再試行を受け付けました（状態：'+(statuses[result.task_status]||result.task_status)+'）。';
}
// question が null の場合は、呼出し元の確認（チェック欄など）を明示の確認とみなす。戻り値：送ったかどうか。
async function requestRetry(task,job,{note='',handoff,question,legacyNotice}){
  if(!isWorkflowJob(job)){
    if(question&&!confirm(question))return false;
    const body={id:task.id,checked_changes:true,note};if(handoff!==undefined)body.handoff_allowed=handoff;
    await api('/api/retry',body);if(legacyNotice)notice(legacyNotice);return true;
  }
  if(needsMaintenance(task,job)){notice('この作業の確認と再開は保守担当が行います。あなたの画面からは再試行できません。待たずにやめる場合は、依頼全体を中止して新しい依頼として出し直せます。');return false;}
  const target=await workflowTarget(job,'retry_task',task.id);
  if(question&&!confirm(question+CONTROL_ONCE_NOTE))return false;
  const result=await api('/api/workflow/control',{action:'retry_task',job_id:job.id,task_id:task.id,target,
    checked_changes:true,note:note||'',handoff_allowed:handoff===true});
  notice(retryAcceptedText(result));return true;
}
async function requestCancel(job){
  const question='実行中・待機中の作業を中止します。既存の変更は自動では戻しません。';
  if(!isWorkflowJob(job)){if(confirm(question))await api('/api/cancel',{id:job.id});return;}
  const target=await workflowTarget(job,'cancel_job');
  if(!confirm(question+'中止は成果の受領ではありません。'+CONTROL_ONCE_NOTE))return;
  await api('/api/workflow/control',{action:'cancel_job',job_id:job.id,target,confirmed:true});
  notice('依頼を中止しました。作業済みの変更は戻していません。');
}
// 保守の確認の後も、この作業の再試行には保守の認証が要る（サーバーの判定）。利用者の画面に再試行が出ると約束しない。
function maintenanceWaitBox(task){
  const confirmed=!!task?.recovery_confirmation&&!task?.recovery_check_required;
  const box=el('section',undefined,'decision-help');box.id='maintenance-wait';box.setAttribute('role','status');
  box.append(el('h3',confirmed?'保守担当の再開を待っています':'保守担当の確認を待っています'),
    el('p',confirmed?'保守担当が、担当の停止と現在の変更を確認しました。この作業の再開（再試行）も保守担当が行います。作業の成果は保存されています。'
      :'作業が途中で止まりました。保守担当が、担当の停止と現在の変更を確認するまで再開しません。確認の後の再開も保守担当が行います。作業の成果は保存されています。'),
    el('p','あなたの画面からは、この作業を再試行できません。待つ場合、あなたの操作は不要です。待たずにやめる場合は、下の「この依頼全体を中止」を使い、必要なら新しい依頼として出し直してください（中止しても、作業済みの変更は戻りません）。','hint'));
  return box;
}
async function action(fn) { if(busy)return; busy=true; try{await fn();await refresh();}catch(e){notice(e.message);}finally{busy=false;} }
function button(text, fn, cls) { const b=el('button',text,cls);b.type='button';b.addEventListener('click',()=>action(fn));return b; }
function pending(id) { return state.approvals.filter(a=>a.task_id===id&&a.status==='pending'); }
function pendingUser(id){return pending(id).filter(userDecision);}
// 承認待ちがあるが、利用者に判断を求めるものではない（保守・技術・改善）。利用者の判断欄には出さない。
function maintenanceHeld(task){return pending(task.id).length>0&&pendingUser(task.id).length===0;}
// W05.03：新方式の依頼で保守の確認待ちの作業は、利用者の判断待ちに数えない（保守の判断と利用者の確認を分ける）。
function workflowMaintenanceWait(task){const job=state.jobs.find(j=>j.id===task.job_id);return isWorkflowJob(job)&&['failed','blocked','interrupted'].includes(task.status)&&!pendingUser(task.id).length&&needsMaintenance(task,job);}
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
  return '原因：依頼が停止しています（'+jobStatusLabel(job)+'）。';
}
function group(task) { if(endedJob(task))return 3; const phase=p0Phase(state.jobs.find(j=>j.id===task.job_id));if(phase&&!pendingUser(task.id).length)return phase.key==='retrying'?1:0; if(maintenanceHeld(task)||workflowMaintenanceWait(task))return 0; if(isStalledReview(task))return 2; if(pending(task.id).length||['failed','blocked','interrupted'].includes(task.status))return 2; if(task.status==='queued')return 0; if(['running','awaiting_approval'].includes(task.status))return 1; return 3; }
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
    open.setAttribute('aria-label','内容を確認：'+(job?.title?job.title+'、':'')+item.task.title);open.dataset.focusKey='banner:'+item.task.id;
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
// P0 L3：依頼の進み具合。表示するのは p0_progress / goal_contract / deliverable_access の項目だけで、完成や権限は画面で判断しない。
// 起動・操作の確認は、サーバーが確認済みを示す値を契約で定めるまで、常に「未検証」と表示する。
const COMPLETION_LABELS={in_progress:'作業中（完成ではありません）',awaiting_user_acceptance:'あなたの確認待ち（受領前です）',accepted:'受領済み'};
function progressList(title,items,mark,cls){
  const box=el('div',undefined,'progress-group'+(cls?' '+cls:''));box.append(el('h4',`${title}（${items.length}件）`));
  if(!items.length){box.append(el('p','なし','hint'));return box;}
  const list=el('ul');for(const item of items)list.append(el('li',`${mark} ${item}`));box.append(list);return box;
}
// 操作の承認（kind==='tool'）を、利用者が判断できる形で出す。説明はコマンドの文字列から機械的に読み取った事実だけ。
// AIによる説明ではなく、安全の保証でもない。読み取れないものは「読み取れません」と書き、コマンドの全文を折りたたみに残す。
const GENERIC_APPROVAL_SUMMARY='AI利用または操作の範囲を確認してください。';
const BACKSLASH=String.fromCharCode(92);
function normalizePath(path){return String(path||'').split(BACKSLASH).join('/').replace(/\/+$/,'').toLowerCase();}
function insideApprovedRoots(path){
  const target=normalizePath(path);
  return !!target&&(state.config?.approved_roots||[]).map(normalizePath).some(root=>root&&(target===root||target.startsWith(root+'/')));
}
function commandUrls(text){return [...String(text).matchAll(/https?:\/\/[^\s"'<>)\]]+/gi)].map(m=>m[0]);}
function urlHost(url){return (url.match(/^https?:\/\/([^\/?#:]+|\[[^\]]+\])/i)||[])[1]||'';}
function isLoopbackHost(host){return ['127.0.0.1','localhost','[::1]'].includes(String(host).toLowerCase());}
function describeCommand(command,background){
  // 先頭の「作業フォルダへ移動」は、何をするかの判定から外す（場所は別の項目で出す）。
  const text=String(command||'').replace(/^\s*cd\s+("[^"]*"|'[^']*'|\S+)\s*(?:&&|;)\s*/i,'');
  let match=text.match(/^\s*python[0-9.]*\s+(?:-[A-Za-z]+\s+)*-m\s+([A-Za-z0-9_.]+)/i);
  if(match){
    if(match[1]==='unittest')return 'テストを実行します（python -m unittest）。';
    if(match[1]==='pip')return 'Pythonのパッケージを操作します（python -m pip）。外部から取得する場合があります。';
    return `プログラム「${match[1]}」を起動します（python -m ${match[1]}）。`+(background?'起動したまま、次の作業に進みます。':'終わるまで待ちます。');
  }
  if(/(urllib|requests\.|httpx|aiohttp|http\.client|^\s*curl\b|Invoke-WebRequest|Invoke-RestMethod)/i.test(text)){
    const hosts=[...new Set(commandUrls(text).map(urlHost))];
    if(hosts.length&&hosts.every(isLoopbackHost))return `コマンド文字列に自分のPC宛てのURL（${hosts.join('、')}）が記載されています。実際の接続先や処理の影響は、この表示だけでは確認できません。`;
    if(hosts.length)return `コマンド文字列に外部URL（${hosts.join('、')}）が記載されています。実際の通信内容は、この表示だけでは確認できません。`;
    return 'コマンド文字列に通信関連の記載があります。接続先や実際の処理は、この表示だけでは確認できません。';
  }
  match=text.match(/^\s*git\s+(status|diff|log|show|branch)\b/i);
  if(match)return `Gitの状態を確認します（git ${match[1]}）。`;
  match=text.match(/^\s*(?:python[0-9.]*|py|node)\s+(?:-[A-Za-z]+\s+)*("[^"]+"|'[^']+'|[^\s-][^\s]*)/i);
  if(match)return `スクリプト ${match[1]} を実行します。`;
  return null;
}
const RISKY_WORDS=/(?:^|[\s;&|(])(?:rm|rmdir|del|erase|rd|mv|move|Remove-Item|Move-Item|Set-Content|Out-File|truncate)(?=[\s;&|)]|$)|shutil\.rmtree|os\.remove|os\.unlink|\bgit\s+(?:reset|clean|push|checkout\s+--)/i;
function toolApprovalView(a){
  const payload=a.payload||{},details=payload.details||{},command=details.command,wrap=el('div',undefined,'tool-approval');
  const rows=[];
  const bash=payload.operation==='Bash'&&typeof command==='string';
  const said=bash?describeCommand(command,details.run_in_background===true):null;
  rows.push(['何をするか',said||(bash?'内容を読み取れません。下の「技術詳細」でコマンドの全文を確認してください。':`「${payload.operation||'不明な操作'}」の実行を求めています。`)]);
  const target=details.file_path||details.path||details.notebook_path;
  if(target)rows.push(['対象のファイル',String(target)+(insideApprovedRoots(target)?'（作業範囲の中）':'（作業範囲の外：要確認）')]);
  if(payload.cwd)rows.push(['実行する場所',String(payload.cwd)+(insideApprovedRoots(payload.cwd)?'（作業範囲の中）':'（作業範囲の外：要確認）')]);
  if(bash){
    const hosts=[...new Set(commandUrls(command).map(urlHost))];
    rows.push(['文字列に記載された通信先',hosts.length?(hosts.every(isLoopbackHost)?'自分のPC内の宛先が記載されています（実際の通信先は実行時の挙動で異なる場合があります）':'外部宛先が記載されています：'+hosts.filter(h=>!isLoopbackHost(h)).join('、')):
      payload.guard?.category==='network'?'通信を伴う可能性があります（通信先はコマンドの文字列から読み取れません）':'コマンドの文字列に通信先の記載はありません（実行時通信の有無は別途確認が必要です）']);
    const risky=command.match(RISKY_WORDS);
    rows.push(['削除・上書き',risky?`削除・上書きを示す語が含まれています：${risky[0].trim()}（内容を確認してください）`:'削除・上書きを示す語は、コマンドの文字列に見つかりません']);
  }
  rows.push(['許可すると','この操作が1回だけ実行されます。次に同じ操作が必要になれば、もう一度確認します。']);
  rows.push(['拒否すると','この操作は実行されません。担当は別の方法を試すか、停止する場合があります。']);
  const list=el('dl',undefined,'tool-facts');
  for(const [label,value] of rows)list.append(el('dt',label),el('dd',value));
  wrap.append(list);
  wrap.append(el('p','この説明は、コマンドの文字列から機械的に読み取ったものです。AIによる説明でも、安全の保証でもありません。','hint'));
  const raw=el('details',undefined,'tool-raw');raw.append(el('summary','技術詳細：コマンドの全文と元のデータ'));
  if(bash)raw.append(el('pre',command,'text-block'));
  raw.append(el('pre',JSON.stringify(payload,null,2),'text-block'));
  wrap.append(raw);
  return wrap;
}
function openCompletion(job){
  const approval=state.approvals.find(a=>a.job_id===job.id&&a.kind==='completion'&&a.status==='pending'&&userDecision(a));
  if(!approval){notice('確認の準備がまだできていません。作業の工程が終わると、ここから確認できます。');return;}
  openTask(approval.task_id);
}
function openCancel(job){
  const tasks=state.tasks.filter(t=>t.job_id===job.id).sort((a,b)=>(b.updated_at||0)-(a.updated_at||0));
  if(!tasks.length){notice('中止できる作業が見つかりません。');return;}
  openTask(tasks[0].id);
  setTimeout(()=>{const target=$('detail-body').querySelector('#cancel-job');if(target)revealSection(target);},300);
}
const PROGRESS_ACTIONS={
  review_completion:['成果を確認する',openCompletion],
  record_completion_evidence:['確認結果を記録する',openCompletion],
  cancel_job:['この依頼を中止する…',openCancel]
};
// W05.02：新方式の依頼の機能一覧。サーバーの投影（job.workflow）の値だけを使い、状態・誰の番か・権限を画面で推測しない。
// 機能の表示は W01.06 の 2.3 の順に判定する。内部の呼び名（WP・WBS番号・版の hash）は技術詳細でだけ出す。
const WAIT_OWNER_LABELS={
  user:'あなたの確認を待っています。',saikuru:'采来が進めています。あなたの操作は不要です。',
  maintainer:'保守担当の確認を待っています。作業の成果は保存されています。あなたの操作は不要です。',
  authorized_person:'権限のある担当者の確認を待っています。',dependency:'先に必要な機能の確認を待っています。',
  none:'待っている操作はありません。'
};
const WORKFLOW_STOP_LABELS={
  workflow_recovery_confirmation_required:'再起動などで作業が止まりました。保守担当が作業の停止を確認するまで、再開しません。',
  workflow_maintenance_required:'作業の環境の確認が必要です。保守担当が対応します。',
  workflow_safety_blocked:'安全のため作業を止めました。権限のある担当者の確認が必要です。',
  workflow_blocked:'依頼が止まっています。',workflow_failed:'作業が失敗して止まっています。',workflow_interrupted:'作業が中断しています。',
  workflow_state_unknown:'依頼の状態を確認できません。保守担当が確認します。',
  workflow_state_verification_required:'記録を照合できないため、完成とは扱いません。保守担当が確認します。',
  workflow_authentication:'AIの認証の確認が必要です。',workflow_provider_limit:'AIの利用上限に達しました。',
  workflow_timeout:'作業の時間の上限で止まりました。',workflow_sandbox_setup:'作業の実行環境の準備で止まりました。',
  workflow_safety:'安全上の確認が必要です。',workflow_cancelled:'作業を中断しました。',workflow_execution:'作業の結果の確認が必要です。'
};
// 画面は定期的に描き直すため、入力中の文とチェックは描き直しをまたいで保持する（送信・成功で消す）。
const featureDrafts=new Map();
function draftField(node,key){node.value=featureDrafts.get(key)??'';node.addEventListener('input',()=>featureDrafts.set(key,node.value));node.dataset.focusKey=key;return node;}
function workflowPackages(job){return (job.workflow?.progress?.packages)||[];}
function featureApproval(wp){
  return (state.approvals||[]).find(a=>a.kind==='feature_review'&&a.status==='pending'
    &&a.payload?.feature_target?.work_package_id===wp.id&&a.actionable===true)||null;
}
function wpState(wp,job){
  const progress=workflowPackages(job),item=progress.find(p=>p.id===wp.id)||{};
  const waiting=(wp.dependencies||[]).filter(d=>!progress.find(p=>p.id===d.id)?.feature_approved);
  const ex=wp.execution_state,rv=wp.review_status;
  if(ex==='cancelled')return {key:'cancelled',label:'中止',owner:'なし'};
  if(ex==='blocked')return {key:'blocked',label:rv==='changes_requested'?'差し戻し後に停止中':'停止中',owner:'理由による',note:'止まった理由は、上の依頼の状態を確認してください。'};
  if(ex==='running')return rv==='changes_requested'?{key:'rework',label:'差し戻しの修正中（あなたの指摘を反映しています）',owner:'采来'}:{key:'running',label:'作業中',owner:'采来'};
  if(['planned','ready'].includes(ex)){
    if(waiting.length)return {key:'dependency',label:`依存待ち：『${waiting.map(d=>d.title).join('』『')}』の確認後に始まります`,owner:'先に必要な機能',waiting};
    return rv==='changes_requested'?{key:'rework',label:'差し戻し：修正の開始待ち',owner:'采来'}:{key:'queued',label:'開始待ち',owner:'采来'};
  }
  if(ex==='succeeded'){
    if(rv==='pending'&&featureApproval(wp))return {key:'review',label:'あなたの確認待ち',owner:'あなた'};
    if(rv==='approved'&&item.feature_approved)return {key:'approved',label:'確認済み',owner:'なし'};
    if(rv==='approved')return {key:'preparing',label:'確認の記録を照合できません（確認済みとは扱いません）',owner:'采来'};
    const label=rv==='invalidated'?'確認の準備中（前回の確認後に関連する内容が変わったため、確認を出し直します）'
      :rv==='changes_requested'?'差し戻し済み（修正後に確認を出し直します）':'確認の準備中';
    return {key:'preparing',label,owner:'采来'};
  }
  return {key:'unknown',label:'状態を確認中',owner:'保守担当'};
}
function shortVersion(sha){return /^[0-9a-f]{12,}$/.test(String(sha||''))?String(sha).slice(0,12):'不明';}
function versionLine(path,sha){
  const line=el('li');line.append(el('code',path||'（場所不明）'),document.createTextNode('　'),el('span','版 '+shortVersion(sha),'version-tag'));
  const full=el('small','　'+(sha||''),'tech-only');line.append(full);return line;
}
// 機能の確認（本人）。表示した版の digest をそのまま確認に使い、画面で版を作り直さない。依頼全体の受領とは別。
function featureDecisionForm(approval,job){
  const target=approval.payload?.feature_target||{},digest=approval.payload?.feature_target_digest;
  const wp=(job.workflow?.work_packages||[]).find(w=>w.id===target.work_package_id)||{};
  const form=el('section',undefined,'feature-decision');form.dataset.userDecision='1';
  form.append(el('h4',`機能の確認：${wp.title||'（名前不明）'}`));
  form.append(el('p','この確認は、この機能の動作・見た目・完了の条件の確認です。依頼全体の正式受領ではありません。','notice'));
  const criteria=target.criteria||{};
  if(criteria.done_when?.length)form.append(progressList('この機能の完了の条件',criteria.done_when,'○','todo'));
  if(criteria.goal_links?.length)form.append(progressList('対応する目標・受入条件',criteria.goal_links,'・',''));
  const pendingChecks=Array.isArray(target.pending_checks)?target.pending_checks.map(p=>p.check).filter(Boolean):[];
  if(pendingChecks.length){form.append(progressList('AIのレビューが確かめられなかった点（あなたが確かめてください）',pendingChecks,'!','unmet'));
    form.append(el('p','下の「表示された版の内容を確認しました」は、この点を確かめたことも含みます。確かめられない場合は差し戻してください。','hint'));}
  const artifacts=Array.isArray(target.artifacts)?target.artifacts:[];
  const files=el('div',undefined,'progress-group');files.append(el('h4',`確認する成果物の版（${artifacts.length}件）`));
  const list=el('ul');for(const row of artifacts)list.append(versionLine(row.path,row.sha256));files.append(list);form.append(files);
  const tests=Object.keys(target.test_snapshot||{});
  if(tests.length){const t=el('div',undefined,'progress-group tech-only');t.append(el('h4',`試験ファイルの版（${tests.length}件）`));const tl=el('ul');for(const p of tests)tl.append(versionLine(p,target.test_snapshot[p]));t.append(tl);form.append(t);}
  form.append(el('p','起動・操作の確認は、ご自身で行ってください。画面に表示した版と違う内容になった場合、この確認は使えなくなります。期限：'+date(approval.expires_at),'hint'));
  const note=draftField(el('textarea'),'feature-note:'+approval.id);note.rows=3;note.maxLength=4000;
  note.placeholder='差し戻す場合は、気づいた点を書いてください（承認する場合は任意）';note.setAttribute('aria-label',`${wp.title||'機能'}の確認の補足`);
  const check=el('input');check.type='checkbox';check.checked=featureDrafts.get('feature-check:'+approval.id)===true;
  check.addEventListener('change',()=>featureDrafts.set('feature-check:'+approval.id,check.checked));check.dataset.focusKey='feature-check:'+approval.id;const checkLabel=el('label','表示された版の内容を確認しました','check');checkLabel.prepend(check);
  form.append(note,checkLabel);
  const actions=el('div',undefined,'actions');
  const send=allow=>api('/api/workflow/feature-review/decide',{approval_id:approval.id,allow,note:note.value,action:'decide_feature_review',
    confirmation:{action:'feature_review_confirmation',target_digest:digest,decision:allow?'approved':'rejected'}});
  const reject=button(`差し戻す（${wp.title||'この機能'}）`,async()=>{
    if(!note.value.trim()){notice('差し戻す理由を入力してください。');note.focus();return;}
    await send(false);featureDrafts.delete('feature-note:'+approval.id);nextFocusKey='wp:'+wp.id;notice('差し戻しました。修正と、影響する機能の再確認を采来が進めます。');
  },'danger');
  const approve=button(`この版を確認済みにする（${wp.title||'この機能'}）`,async()=>{
    if(!check.checked){notice('「表示された版の内容を確認しました」にチェックを入れてください。');check.focus();return;}
    await send(true);featureDrafts.delete('feature-note:'+approval.id);featureDrafts.delete('feature-check:'+approval.id);nextFocusKey='wp:'+wp.id;notice('この機能を確認済みにしました。依頼全体の受領は、すべての機能の確認の後に別に行います。');
  },'primary');
  reject.dataset.focusKey='feature-reject:'+approval.id;approve.dataset.focusKey='feature-approve:'+approval.id;
  if(!digest){actions.append(el('p','確認する版を特定できません。再読み込みしてください。','form-error'));}
  else actions.append(reject,approve);
  form.append(actions);return form;
}
// 確認済みの機能を差し戻す。依頼全体ではなく、この機能だけ。影響する機能の範囲は采来が判定し、無関係な確認は残る。
function featureReworkForm(wp,job){
  const box=el('details',undefined,'feature-rework'),summary=el('summary',`この機能を差し戻す…（${wp.title}）`);
  summary.dataset.focusKey='feature-rework-open:'+wp.id;box.append(summary);
  box.open=openJobRows.has('rework|'+wp.id);box.addEventListener('toggle',()=>{if(box.open)openJobRows.add('rework|'+wp.id);else openJobRows.delete('rework|'+wp.id);});
  box.append(el('p','確認済みの機能に問題が見つかった場合に使います。この機能の修正と、影響する機能の再確認だけが行われます。関係のない機能の確認は残ります。','hint'));
  const note=draftField(el('textarea'),'feature-rework:'+wp.id);note.rows=3;note.maxLength=4000;note.placeholder='見つかった問題（必須）';note.setAttribute('aria-label',`${wp.title}を差し戻す理由`);
  const send=button('差し戻す',async()=>{
    if(!note.value.trim()){notice('差し戻す理由を入力してください。');note.focus();return;}
    await api('/api/workflow/feature-rework',{job_id:job.id,work_package_id:wp.id,work_package_revision:wp.state_revision,note:note.value,
      action:'rework_job',confirmation:{action:'request_feature_rework',target_digest:wp.feature_target_digest}});
    featureDrafts.delete('feature-rework:'+wp.id);nextFocusKey='wp:'+wp.id;notice('差し戻しました。修正と、影響する機能の再確認を采来が進めます。');
  },'danger');send.dataset.focusKey='feature-rework-send:'+wp.id;box.append(note,send);
  return box;
}
// 待機理由の種類ごとの説明と次に起きること。種類と担当はサーバーの投影（dispatch.*.details）の値で、画面では推測しない。
const WAIT_KIND_LABELS={
  stopped:'依頼が実行中ではないため、次の作業を始めません。',capacity:'別の作業が同じ場所を使っているため、終わるまで待っています。',
  decision:'確認の返事を待っているため、次の作業を始めません。',dependency:'先に必要な作業や機能の確認が済むのを待っています。',
  retry:'失敗した作業の再試行までの待ち時間です。',maintenance:'再起動などで止まった作業の確認を待っています。',
  consistency:'計画と記録が一致しないため、始めません。',unknown:'待っている理由を判別できません。安全のため始めません。'
};
const WAIT_NEXT_LABELS={
  user:'次に動く人：あなた（表示された確認、または「確認を始める」の操作を行ってください）',system:'次に動く人：采来（あなたの操作は不要です。条件がそろうと自動で進みます）',
  maintainer:'次に動く人：保守担当（確認が済むまで再開しません）',
  authorized_person:'次に動く人：権限のある担当者（表示された確認を行ってください）',
  saikuru:'次に動く人：采来（あなたの操作は不要です）',dependency:'次に動く人：采来（先に必要な機能の確認が済むのを待っています）',
  none:'待っている操作はありません。'
};
function waitReasonList(wf,packages){
  const titles=new Map();for(const wp of packages)for(const id of wp.task_ids||[])titles.set(id,wp.title);
  const rows=[];
  for(const row of wf.dispatch?.waiting||[])for(const d of row.details||[])rows.push({target:titles.get(row.task_id)||'次の作業',d});
  for(const d of wf.dispatch?.reason_details||[])if(d.code!=='no_ready_task'||!rows.length)rows.push({target:'依頼全体',d});
  if(!rows.length||wf.dispatch?.status==='ready')return null;
  const seen=new Set(),list=el('ul',undefined,'wait-reasons');
  for(const {target,d} of rows){
    const key=target+'|'+d.code;if(seen.has(key))continue;seen.add(key);
    const li=el('li',`${target}：${WAIT_KIND_LABELS[d.kind]||WAIT_KIND_LABELS.unknown}　${WAIT_NEXT_LABELS[d.owner]||WAIT_NEXT_LABELS.maintainer}`);
    li.append(el('small','　'+d.code,'tech-only'));list.append(li);
  }
  const box=el('div',undefined,'progress-group');box.append(el('h4','今は始めない理由と、次に起きること'),list);return box;
}
// 計画の上限（1〜6件）と、7件以上の依頼の進め方。件数が上限に達したときに案内する。切り捨てや自動分割はしない。
function packageLimitNote(wf,count){
  const lim=wf.context?.package_limit;if(!lim||(count<lim.limit&&!wf.context?.plan_over_limit))return null;
  const box=el('div',undefined,'progress-group package-limit');box.append(el('p',lim.reason,'hint'),progressList('7件以上の依頼の進め方',lim.next,'›','unmet'));return box;
}
// 引継ぎ：現在の要求・計画の版、未完了の機能、次に動く人。会話の全文は含めない。
function handoffNote(wf){
  const c=wf.context||{};if(!c.plan_revision)return null;
  const open=(c.unfinished_packages||[]).map(p=>p.title);
  const verified=c.basis==='approved_records';
  const box=el('div',undefined,'progress-group handoff');box.append(el('h4','引継ぎの要点'));
  const plan=verified?`承認済みの計画の版 ${c.plan_revision}`:`計画の版 ${c.plan_revision}（記録を照合できていないため、承認済みとは扱いません）`;
  box.append(el('p',`要求の版 ${c.requirement_revision}／${plan}。確認が済んでいない機能：${open.length?open.join('、'):'なし'}。`,'hint'));
  const inputs=(c.input_versions||[]).filter(v=>(v.artifacts||[]).length);
  if(inputs.length){
    const list=el('ul',undefined,'handoff-inputs');
    for(const v of inputs)for(const a of v.artifacts)list.append(el('li',`${v.wbs_id}：${a.path}（版 ${String(a.sha256||'').slice(0,12)}）`));
    box.append(el('p','引き渡す成果物（承認済みの計画に記録された入力）','hint'),list);
  }
  box.append(el('p',WAIT_NEXT_LABELS[c.next_owner]||'次に動く人を確認できません。','hint'));return box;
}
function workflowPanel(job){
  const wf=job.workflow||{},progress=wf.progress||{},packages=wf.work_packages||[];
  const panel=el('section',undefined,'workflow-panel');panel.append(el('h4',`機能の一覧（${packages.length}件）`));
  panel.append(el('p',WAIT_OWNER_LABELS[wf.wait_owner]||'誰の番かを確認できません。','wait-owner'));
  const waits=waitReasonList(wf,packages);if(waits)panel.append(waits);
  const stops=(wf.stop_codes||[]).map(c=>WORKFLOW_STOP_LABELS[c]).filter(Boolean);
  if(stops.length)panel.append(progressList('止まっている理由',stops,'!','unmet'));
  if(wf.uncertain_operation)panel.append(el('p','結果が分からない操作があります。安全のため、保守担当が確認するまで同じ操作を繰り返しません。','notice'));
  const counts=progress.counts||{},uat=progress.uat||{},n=counts.packages||packages.length;
  const tally=el('ul',undefined,'workflow-tally');
  tally.append(el('li',`技術の確認が終わった機能：${counts.technical_complete||0} / ${n}`),
    el('li',`あなたが確認済みの機能：${counts.feature_approved||0} / ${n}`),
    el('li',`受入条件の確認：${uat.confirmed||0} / ${uat.required||0}`),
    el('li',`依頼全体の正式受領：${progress.formal_receipt?'済み':'まだ'}`));
  panel.append(tally,el('p','作業が終わった数は、受入の確認が済んだ数ではありません。','hint'));
  if(progress.state==='inconclusive')panel.append(el('p','記録を照合できないため、進み具合を判断できません。完成とは扱いません。','notice'));
  if(wf.feature_confirmation_count>0)panel.append(el('p',`あなたの確認を待っている機能：${wf.feature_confirmation_count}件（1件ずつ確認します）`,'hint'));
  const list=el('ol',undefined,'wp-list');
  for(const wp of packages){
    const st=wpState(wp,job),item=el('li',undefined,'wp-item');item.dataset.state=st.key;item.dataset.focusKey='wp:'+wp.id;item.tabIndex=-1;
    const head=el('p',undefined,'wp-head');head.append(el('strong',wp.title),document.createTextNode('　'),el('span',st.label,'badge wp-state'));
    head.append(el('small','　'+(wp.wbs_id||''),'tech-only'));item.append(head);
    item.append(el('p',`次に操作する人：${st.owner}`,'hint'));
    if(wp.done_when?.length)item.append(el('p','完了の条件：'+wp.done_when.join('／'),'hint'));
    if(st.key==='approved'&&wp.feature_target_digest)item.append(el('p','確認した版 '+shortVersion(wp.feature_target_digest),'hint tech-only'));
    const actions=el('div',undefined,'actions');
    if((wp.available_actions||[]).includes('request_feature_review')){
      const b=button(`確認を始める（${wp.title}）`,async()=>{
        await api('/api/workflow/feature-review/prepare',{job_id:job.id,work_package_id:wp.id,work_package_revision:wp.state_revision,action:'request_feature_review'});
        notice('確認する内容を用意しました。まだ確認済みにはなっていません。内容を見て判断してください。');
      },'secondary');b.dataset.focusKey='wp-prepare:'+wp.id;actions.append(b);
      item.append(el('p','技術の確認が終わりました。「確認を始める」を押すと、確認する成果物の版を用意します（まだ承認にはなりません）。','hint'));
    }
    const approval=st.key==='review'?featureApproval(wp):null;
    if(approval)item.append(featureDecisionForm(approval,job));
    if(actions.children.length)item.append(actions);
    if((wp.available_actions||[]).includes('rework_job'))item.append(featureReworkForm(wp,job));
    list.append(item);
  }
  if(!packages.length)panel.append(el('p','機能の一覧はまだありません（計画の承認後に表示されます）。','hint'));
  else panel.append(list);
  for(const extra of [packageLimitNote(wf,packages.length),handoffNote(wf)])if(extra)panel.append(extra);
  return panel;
}
// 影響を受けない機能ごとに「以前の確認を再利用する」か「再実行する」かを選ぶ。選ばない機能は停止したままになる。
// 再利用できるのは、成果物・入力・確認の記録が現在も一致する機能だけ。選択は承認の送信と一緒にだけ送られる。
const transferChoices=new Map();
function transferChoiceView(a,rev,name){
  const group=el('div',undefined,'progress-group transfer-choices'),picked=transferChoices.get(a.id)||{};transferChoices.set(a.id,picked);
  const offered=new Set((rev.transfer_candidates||[]).map(c=>c.wbs_id)),stopped=rev.transfer_blocked||[];
  if(!offered.size&&!stopped.length)return group;
  group.append(el('h4','影響を受けない機能の扱い'));
  group.append(el('p','選ばない機能は停止したままになり、作業は進みません。再利用を選べるのは、成果物・入力・確認の記録が今も一致している機能だけです。いつでも拒否できます。','hint'));
  const reasons={dependency_artifact_changed:'成果物のファイルが変わっています',shared_file_with_changed_package:'変更される機能と同じファイルを使っています',
    dependency_not_reusable:'先に必要な機能を再利用できません',dependency_feature_review_required:'機能の確認が済んでいません',
    dependency_feature_review_invalid:'以前の確認が有効ではありません',evidence_not_current:'以前の確認が現在の内容と一致しません'};
  const rows=[...(rev.transfer_candidates||[]).map(c=>({id:c.wbs_id,why:null})),...stopped.map(b=>({id:b.wbs_id,why:b.reasons}))];
  for(const row of rows){
    const line=el('p',undefined,'transfer-row'),select=el('select'),label=el('label',name(row.id)+'：');
    select.setAttribute('aria-label',`${name(row.id)}の扱い`);
    const options=[['','選ばない（停止したまま）'],...(row.why?[]:[['reuse','以前の確認を再利用する']]),['rerun','再実行する']];
    for(const [value,text] of options){const o=el('option',text);o.value=value;select.append(o);}
    select.value=picked[row.id]||'';
    select.addEventListener('change',()=>{if(select.value)picked[row.id]=select.value;else delete picked[row.id];});
    label.append(select);line.append(label);
    if(row.why)line.append(el('small','　再利用できない理由：'+row.why.map(r=>reasons[r]||r).join('、'),'hint'));
    group.append(line);
  }
  return group;
}
// 新方式の計画の確認（承認前）。機能の分け方・完了の条件・使う既存の成果物・作業場所・変更の要点を表示する。
// 承認では、表示した分析の digest をそのまま送る（workflow_confirmation）。非開発者に内部の設計判断を求めない。
function planRevisionView(a){
  const box=el('div',undefined,'plan-revision'),plan=a.payload||{},rev=a.workflow_revision||{};
  const titles=Object.fromEntries((plan.work_packages||[]).map(w=>[w.wbs_id,w.title]));
  const name=id=>titles[id]||id;
  box.append(el('h4',`機能の分け方（${(plan.work_packages||[]).length}件）`));
  box.append(el('p','AIが成果物を D1・D2…、受入条件を A1・A2… の番号で指定した場合も、下では番号を成果物・受入条件の本文に置き換えて表示しています。存在しない番号や言い換えは、AIへの差し戻しになり、ここには表示されません。','hint'));
  const list=el('ol',undefined,'wp-list');
  for(const w of plan.work_packages||[]){
    const item=el('li',undefined,'wp-item'),head=el('p',undefined,'wp-head');
    head.append(el('strong',w.title),el('small','　'+w.wbs_id,'tech-only'));item.append(head);
    if(w.done_when?.length)item.append(el('p','完了の条件：'+w.done_when.join('／'),'hint'));
    if(w.goal_links?.length)item.append(el('p','対応する目標・受入条件：'+w.goal_links.join('／'),'hint'));
    item.append(el('p',w.depends_on?.length?'先に必要な機能：'+w.depends_on.map(name).join('、'):'先に必要な機能：なし','hint'));
    const inputs=w.input_artifacts||[];
    if(inputs.length){const g=el('ul');for(const r of inputs)g.append(versionLine(r.path,r.sha256));item.append(el('p','使う既存の成果物：','hint'),g);}
    list.append(item);
  }
  box.append(list);
  const scope=plan.checkpoint_paths||[];
  if(scope.length){const s=el('details');s.append(el('summary',`変更してよいファイル（${scope.length}件）`));const sl=el('ul');for(const p of scope)sl.append(el('li',p));s.append(sl);box.append(s);}
  if(rev.requirements_changed)box.append(el('p','要求の内容が変わります：'+Object.keys(rev.scope_changes||{}).map(k=>({primary_goal:'主な目標',deliverables:'成果物',acceptance_criteria:'受入条件',non_goals:'対象外',constraints:'制約'})[k]||k).join('、'),'notice'));
  if(rev.base_versions?.plan_revision>0){
    const affected=(rev.affected_work_packages||[]).map(name),again=(rev.reconfirmation_required||[]).map(name);
    box.append(progressList('変更の影響を受ける機能',affected,'!','unmet'));
    box.append(progressList('新しい計画で確認し直す機能（既定では、影響がなくても以前の確認は引き継ぎません）',again,'○','todo'));
    box.append(transferChoiceView(a,rev,name));
  }
  const ws=rev.workspace_boundary||{};
  const where=el('div',undefined,'progress-group');where.append(el('h4','作業する場所'));
  if(ws.status==='ready'){
    where.append(el('p',`作業用のコピーで作業します。元のプロジェクトは直接変更しません。編集中のファイル：${(ws.dirty_changes||[]).length}件`,'hint'));
    where.append(el('p',`作業用のコピー：${ws.execution_root}（${ws.execution_branch}）／統合先：${ws.integration_root}（${ws.integration_branch}）`,'hint tech-only'));
    where.append(el('p','統合先の未保存の変更は確認していません。作業用のコピーの作成手順の記録は確認できません。','hint'));
  }else{
    where.append(el('p','作業する場所を確認できません。この計画を承認しても作業は始まりません。','form-error'));
    where.append(el('small',String(ws.reason||''),'tech-only'));
  }
  box.append(where);
  return box;
}
// P0 L3：登録済みの成果物（テキスト）を、読み取り専用で表示する。内容は信頼できない入力として textContent だけで表示し、実行・描画しない。
// 起動・操作の確認は引き続き「未検証」。表示した内容は、開いた時点のもの（再描画をまたいで保持し、閉じて開き直すと最新になる）。
const openedArtifacts=new Map();
function deliverableRow(job,item){
  const row=el('div',undefined,'deliverable-row'),key=job.id+'|'+item.path;
  const line=el('p');line.append(el('code',item.path||'（場所不明）'),document.createTextNode('　'),el('span','起動・操作は未検証','badge unverified'));
  if(item.launch_status&&item.launch_status!=='unverified')line.append(el('small',`（サーバーの値：${item.launch_status}）`,'tech-only'));
  row.append(line);
  if(!item.path)return row;
  const shown=openedArtifacts.get(key);
  const label=shown?(shown.error?'再読み込み':'内容を閉じる'):'内容を読む';
  const readButton=button(label,async()=>{
    if(shown&&!shown.error){openedArtifacts.delete(key);return;}
    try{
      const result=await api('/api/job/p0-artifact?job_id='+encodeURIComponent(job.id)+'&path='+encodeURIComponent(item.path));
      openedArtifacts.set(key,{content:String(result.content??''),truncated:result.truncated===true});
    }catch(error){openedArtifacts.set(key,{error:'内容を読み込めませんでした：'+error.message});}
  },'secondary');
  readButton.setAttribute('aria-label',`${label}（${job.title}／${item.path}）`);readButton.dataset.focusKey='artifact:'+key;row.append(readButton);
  if(shown?.error)row.append(el('p',shown.error,'form-error'));
  else if(shown){
    const text=el('pre',shown.content,'artifact-text');text.tabIndex=0;text.setAttribute('aria-label',`${item.path} の内容（読み取り専用の表示）`);
    row.append(text,el('p','表示した時点の内容です。更新された場合は、閉じて開き直してください。'+(shown.truncated?'内容が長いため、一部のみ表示しています。':''),'hint'));
  }
  return row;
}
// 依頼の一覧は1件1行（名前・状態・進捗）。詳細は開いたときだけ出す。開閉は再描画をまたいで保持する。
const openJobRows=new Set();
function jobCounts(job){
  const progress=job.p0_progress||{};
  if(Array.isArray(progress.completed)||Array.isArray(progress.remaining)){
    const done=(progress.completed||[]).length,total=done+(progress.current?1:0)+(progress.remaining||[]).length;
    if(total>0)return {done,total};
  }
  const tasks=(state.tasks||[]).filter(t=>t.job_id===job.id&&t.status!=='cancelled');
  return tasks.length?{done:tasks.filter(t=>t.status==='succeeded').length,total:tasks.length}:null;
}
function needsUserDecision(job){return (state.approvals||[]).some(a=>a.job_id===job.id&&a.status==='pending'&&userDecision(a));}
function deliverableGroup(job,access){
  const group=el('details',undefined,'job-deliverables');
  group.append(el('summary',`成果物（${access.length}件）`));
  group.open=openJobRows.has(job.id+'|deliverables');
  group.addEventListener('toggle',()=>{const key=job.id+'|deliverables';if(group.open)openJobRows.add(key);else openJobRows.delete(key);});
  if(!access.length){group.append(el('p','登録された成果物はまだありません。','hint'));return group;}
  group.append(el('p','起動・操作の確認は、まだどれも行われていません。内容を読めても、動作確認の代わりにはなりません。起動用のURLも未提供です。','hint'));
  for(const item of access)group.append(deliverableRow(job,item));
  return group;
}
function progressCard(job){
  const progress=job.p0_progress||{},goal=job.goal_contract||{},counts=jobCounts(job);
  const row=el('details',undefined,'job-row');row.dataset.jobId=job.id;
  // 色は状態ごとに分ける。色だけに頼らず、状態の文字も必ず出す。
  row.dataset.state=needsUserDecision(job)?'decision':p0Phase(job)?.key||job.status||'unknown';
  const head=el('summary',undefined,'job-row-head');head.dataset.focusKey='jobrow:'+job.id;
  const decision=needsUserDecision(job);
  head.append(el('h3',job.title,'job-row-title'),el('span',decision?'あなたの判断が必要':jobStatusLabel(job),'badge'+(decision?' approval':'')));
  if(counts){
    const meter=el('span',undefined,'job-row-progress');
    // 従来形式の依頼は、作業の数だけから数える。全作業が終わっていても、依頼が止まっていれば完了ではない
    // （レビューの指摘が残る、判断待ちなど）。「全部完了」に見える割合は出さず、事実の文だけにする。
    const allWorksDone=job.p0_contract_version!==1&&counts.done===counts.total;
    if(allWorksDone){
      meter.append(el('span',`作業${counts.total}件が終了（依頼は未完了）`,'job-row-count'));
    }else{
      meter.append(el('span',`${counts.total}工程中 ${counts.done}件完了`,'job-row-count'));
      const bar=el('progress');bar.max=counts.total;bar.value=counts.done;bar.setAttribute('aria-label',`${job.title}：${counts.total}工程中${counts.done}件完了`);meter.append(bar);
    }
    head.append(meter);
  }
  row.append(head);
  const body=el('div',undefined,'job-row-body');
  body.append(el('p',`作るもの：${goal.primary_goal||job.goal||'未設定'}`,'summary'));
  if(job.p0_contract_version!==1){
    // 従来形式の依頼：完成の契約や受入条件は持たない。担当の作業の状態だけを、事実として並べる。
    const works=(state.tasks||[]).filter(t=>t.job_id===job.id&&t.status!=='cancelled');
    const finished=works.filter(t=>t.status==='succeeded').map(t=>t.title);
    const handedOff=works.filter(t=>t.status==='handed_off').map(t=>`${t.title}（${statuses[t.status]||t.status}）`);
    const rest=works.filter(t=>!['succeeded','handed_off'].includes(t.status)).map(t=>`${t.title}（${statuses[t.status]||t.status}）`);
    const lists=el('div',undefined,'progress-lists');
    lists.append(progressList('完了した作業',finished,'✓','done'),progressList('残りの作業',rest,'○','todo'),progressList('未完了で引き継いだ作業',handedOff,'↗','todo'));
    body.append(lists,el('p','従来形式の依頼です。完成の判定や成果物の確認は、各作業の画面で行います。','hint'));
    row.append(body);
    row.open=openJobRows.has(job.id);
    row.addEventListener('toggle',()=>{if(row.open)openJobRows.add(job.id);else openJobRows.delete(job.id);});
    return row;
  }
  if(goal.deliverables?.length)body.append(el('p',`成果物（予定）：${goal.deliverables.join('、')}`,'hint'));
  body.append(el('p',`現在の工程：${progress.current||'確認中'}`));
  if(progress.next_action)body.append(el('p',`次の動作：${progress.next_action}`));
  const lists=el('div',undefined,'progress-lists');
  lists.append(progressList('完成済み',progress.completed||[],'✓','done'),progressList('残り',progress.remaining||[],'○','todo'));
  body.append(lists);
  const unmet=progress.unmet_acceptance_criteria||[];
  body.append(progressList('まだ確認できていない受入条件',unmet,'○',unmet.length?'unmet':''));
  const status=COMPLETION_LABELS[progress.completion_status]||'状態を確認中（完成とは扱いません）';
  body.append(el('p',`完成の状況：${status}`,'completion-status'));
  if(job.workflow_contract_version===1)body.append(job.workflow?workflowPanel(job):el('p','機能の一覧を読み込めません。再読み込みしてください（完成とは扱いません）。','notice'));
  body.append(deliverableGroup(job,Array.isArray(progress.deliverable_access)?progress.deliverable_access:[]));
  const actions=el('div',undefined,'actions');
  for(const name of Array.isArray(progress.available_actions)?progress.available_actions:[]){
    const entry=PROGRESS_ACTIONS[name];if(entry){const b=button(entry[0],()=>entry[1](job),name==='cancel_job'?'danger':'primary');b.setAttribute('aria-label',`${entry[0]}（${job.title}）`);b.dataset.focusKey='progress:'+job.id+':'+name;actions.append(b);}
  }
  if(actions.children.length)body.append(actions);
  row.append(body);
  row.open=openJobRows.has(job.id);
  row.addEventListener('toggle',()=>{if(row.open)openJobRows.add(job.id);else openJobRows.delete(job.id);});
  return row;
}
function renderJobProgress(){
  const section=$('job-progress'),list=$('job-progress-list');if(!section||!list)return;
  const jobs=state.jobs.filter(j=>!['cancelled','accepted','accepted_with_pending_checks'].includes(j.status));
  section.hidden=!jobs.length;list.replaceChildren(...jobs.map(progressCard));
}
function render() {
  $('connection').textContent=state.paused?'新規着手を一時停止中':'● ローカル接続中';
  $('connection').classList.toggle('paused',!!state.paused);
  $('pause').textContent=state.paused?'新規着手を再開':'新規着手を一時停止';
  renderCliUpdate();
  renderJobProgress();
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
  $('history-summary').textContent=`直近${state.history?.days||7}日と未終了の依頼を表示。過去の終了案件は${state.history?.hidden_jobs||0}件です。`;
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
      const job=state.jobs.find(j=>j.id===task.job_id),card=button('',()=>openTask(task.id),'card role-'+task.role+(index===2?' attention':''));card.dataset.focusKey='card:'+task.id;
      const badge=el('span',endedJob(task)?(job.status==='cancelled'?'中止済みの依頼':jobStatusLabel(job)):p0Phase(job)&&!pendingUser(task.id).length?p0Phase(job).label:maintenanceHeld(task)||workflowMaintenanceWait(task)?'保守対応待ち':pending(task.id).length?'確認・承認してください':isStalledReview(task)?'判断が必要（自動修正の上限）':statuses[task.status]||task.status,'badge'+(index===2?' approval':''));
      const head=el('div',undefined,'card-head');head.append(badge);if(job?.title)head.append(el('span',job.title,'job-title'));card.append(head,el('strong',task.title));
      const warnings=(job?.warning_issues||[]).filter(i=>i.task_id===task.id&&i.status==='open');
      if(warnings.length){const note=el('div',undefined,'progress-warning');note.append(el('strong',`⚠ 警告 ${warnings.length}件・作業は継続`));for(const issue of warnings)note.append(el('p',issue.summary));card.append(note);}
      const wait=waitingReason(task);if(wait)card.append(el('p',wait,'summary'));
      if(task.next_action)card.append(el('p',task.next_action,'summary'));
      const reason=stopReason(task);
      if(reason)card.append(el('p',reason.title,'summary'));
      else if(task.summary)card.append(el('p',task.summary,'summary'));
      const meta=el('div',undefined,'meta'),chip=el('span',roles[task.role],'role-chip');chip.append(el('span',` · ${task.profile.adapter}`,'tech-only'));meta.append(chip);card.append(meta);
      if(task.common_agents?.length){const run=task.agent_run,labels={starting:'起動中',running:'実行中',completed:'実行完了',failed:'失敗',cancelled:'中止',held:'送る前に保留（秘密情報の確認）'};
        card.append(el('p','共通Agent: '+task.common_agents.join('、')+' · '+(run?labels[run.status]||run.status:task.agent_name?'起動待ち':'旧方式の役割指示'),'hint tech-only'));}
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
  const panel=node.closest?.('.detail-tab-panel');if(panel)activateDetailTab(panel.closest('#detail-body')||document,panel.dataset.tab);
  for(let parent=node.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
}
// 2026-10-19 （利用者の要望）縦に長い詳細を、ジャンルごとのタブに分ける。区画の中身は作り終えたあとで振り分けるだけで、
// 各区画の処理（回答・承認・再開など）は変えない。失敗・停止のあるタブは赤、確認待ち・注意のあるタブは黄にする。
const DETAIL_TABS=[['action','対応'],['result','結果'],['request','依頼・指示'],['run','実行記録'],['usage','使用量'],['handoff','引き継ぎ']];
const detailTabs=new Map();
function detailTabOf(node){
  const title=(node.querySelector?.(':scope > summary')?.textContent||node.querySelector?.(':scope > h3')?.textContent||node.textContent||'').trim().slice(0,60);
  if(/CLIトークン|入力サイズ|参考情報の画像化/.test(title))return 'usage';
  if(/Agentの実行情報|過去のAgent実行|判断レイヤー|共通Skill|一時モデル|担当する共通Agent|実行時に共通定義|旧方式/.test(title))return 'run';
  if(/引き継ぎ情報を登録|引き継ぎDB/.test(title))return 'handoff';
  if(/今回の作業|対象と元の依頼|過去の指示|一次分類|添付ファイル|指示ファイルの容量|効果検証用|スキル/.test(title))return 'request';
  if(/担当の報告|^状況|成果物$|この依頼の成果物|Artifact|判断の履歴|引き継ぎ範囲|この依頼は終了/.test(title))return 'result';
  return 'action';
}
function activateDetailTab(body,key){
  const bar=body.querySelector?.('.detail-tabs');if(!bar)return;
  for(const tab of bar.querySelectorAll('[role=tab]')){const on=tab.dataset.tab===key;tab.setAttribute('aria-selected',String(on));tab.tabIndex=on?0:-1;}
  for(const panel of body.querySelectorAll('.detail-tab-panel'))panel.hidden=panel.dataset.tab!==key;
  if(renderedDetailTask)detailTabs.set(renderedDetailTask,key);
}
function tabifyDetail(body,task){
  const nodes=[...body.children],header=[];
  // The first lines (担当・モデル・試行、状態) stay above the tabs.
  while(nodes.length&&nodes[0].tagName==='P'&&header.length<2)header.push(nodes.shift());
  const panels=new Map(DETAIL_TABS.map(([key])=>{const panel=el('div',undefined,'detail-tab-panel');panel.dataset.tab=key;panel.setAttribute('role','tabpanel');return [key,panel];}));
  for(const node of nodes)panels.get(detailTabOf(node)).append(node);
  const bar=el('div',undefined,'detail-tabs');bar.setAttribute('role','tablist');bar.setAttribute('aria-label','詳細の表示の切り替え');
  const used=DETAIL_TABS.filter(([key])=>panels.get(key).children.length);
  let alertKey=null,attentionKey=null;
  for(const [key,label] of used){
    const panel=panels.get(key),tab=el('button',label,'detail-tab');tab.type='button';tab.dataset.tab=key;tab.setAttribute('role','tab');
    const tabId='detail-tab-'+key;tab.id=tabId;panel.id='detail-panel-'+key;panel.setAttribute('aria-labelledby',tabId);tab.setAttribute('aria-controls',panel.id);
    const shownNodes=[...panel.children].filter(node=>document.body.dataset.view==='technical'||!node.classList.contains('tech-only'));
    const has=selector=>shownNodes.some(node=>node.matches(selector)||node.querySelector(selector));
    const alert=has('.failure-fold,.stop-fold,#failure-card,.failure-card');
    const attention=!alert&&has('.approval-box,.stop-reason-box,.notice,#decision-guide');
    if(alert){tab.classList.add('tab-alert');tab.title='失敗・停止があります';alertKey??=key;}
    else if(attention){tab.classList.add('tab-attention');tab.title='確認待ち・注意があります';attentionKey??=key;}
    tab.addEventListener('click',()=>activateDetailTab(body,key));
    tab.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();const tabs=[...bar.querySelectorAll('[role=tab]')].filter(t=>document.body.dataset.view==='technical'||!t.classList.contains('tech-only'));
      const next=tabs[(tabs.indexOf(tab)+(event.key==='ArrowRight'?1:tabs.length-1))%tabs.length];next.focus();activateDetailTab(body,next.dataset.tab);});
    if([...panel.children].every(node=>node.classList.contains('tech-only')))tab.classList.add('tech-only');
    bar.append(tab);
  }
  body.replaceChildren(...header,bar,...used.map(([key])=>panels.get(key)));
  const visible=used.filter(([key])=>document.body.dataset.view==='technical'||!bar.querySelector('[data-tab="'+key+'"]').classList.contains('tech-only'));
  const keys=visible.map(([key])=>key),saved=detailTabs.get(task.id);
  if(!keys.includes(alertKey))alertKey=null;if(!keys.includes(attentionKey))attentionKey=null;
  activateDetailTab(body,keys.includes(saved)?saved:(alertKey||attentionKey||(keys.includes('result')?'result':keys[0])));
}
$('view-toggle')?.addEventListener('click',()=>{
  if(document.body.dataset.view==='technical')return;
  const body=$('detail-body'),bar=body.querySelector('.detail-tabs');if(!bar)return;
  if(bar.querySelector('[aria-selected="true"]')?.classList.contains('tech-only')){
    const first=[...bar.querySelectorAll('[role=tab]')].find(tab=>!tab.classList.contains('tech-only'));
    if(first)activateDetailTab(body,first.dataset.tab);
  }
});
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
    // W05.03：保守の確認待ちの案内は、利用者が操作を探さなくて済むよう開いて出す。
    const isMaintenanceWait=stopped&&node.id==='maintenance-wait';
    fold.open=changed?Boolean(isFocus||(stopped&&(isFailure||isCause||isMaintenanceWait))):Boolean(previous.open[key]);
    if(isFocus)focusFold=fold;
    fold.addEventListener('toggle',()=>{const saved=detailFolds.get(task.id);if(saved&&fold.isConnected)saved.open[key]=fold.open;});
  }
  detailFolds.set(task.id,{signature,open:Object.fromEntries([...body.querySelectorAll(':scope > details[data-fold-key]')].map(d=>[d.dataset.foldKey,d.open]))});
  if(changed&&focusFold)requestAnimationFrame(()=>{if(focusFold.isConnected)focusFold.scrollIntoView({block:'start'});});
}
function reportText(result){
  let text=result.summary||'';
  if(result.stage_checks?.length)text+='\n\n各担当の検証報告（未実施の項目を含みます。合格の保証ではありません）:\n'+result.stage_checks.map(item=>`${item.title}（担当報告: ${item.reported_status||'不明'}）\n${(item.checks||[]).join('\n')}${item.pending_checks?.length?'\n担当時点で未確認と報告された項目:\n'+item.pending_checks.map(c=>'・'+c).join('\n'):''}`).join('\n\n');
  if(result.tasks)text+='\n\n'+result.tasks.map((t,i)=>`${i+1}. ${t.title}（${roles[t.role]||t.role}）\n${t.instruction}`).join('\n\n');
  if(result.code_test_plan){const p=result.code_test_plan;text+='\n\nコードテスト計画: '+(p.required?'実施する':'対象外（'+(p.reason||'理由未記載')+'）');if(p.required)text+='\n既存テスト: '+(p.existing_tests?.length?p.existing_tests.join('、'):'なし')+'\n確認する条件:\n'+(p.cases||[]).map(c=>'・'+c).join('\n')+'\n実行予定: '+p.command+'\n※計画のコマンドは自動承認されません。実行時に既存の安全確認を通します。';}
  if(result.test_files?.length)text+='\n\n今回使用したテストファイル: '+result.test_files.join('、')+(result.test_change_reason?'\nテスト変更の理由: '+result.test_change_reason:'');
  if(result.security_review_required===true)text+='\n\n通常レビューの後、common-security-reviewerを独立セッションで起動します。';
  if(result.checks?.length)text+='\n\n報告された検証:\n'+result.checks.map(c=>'・'+c).join('\n');
  if(result.pending_checks?.length)text+='\n\n未確認の必須検証（依頼全体の達成は未確認）:\n'+result.pending_checks.map(c=>'・'+c).join('\n');
  if(result.question)text+='\n\n確認したいこと:\n'+result.question;
  if(result.note)text+='\n\n'+result.note;
  if(result.review_findings?.length)text+='\n\nレビュー指摘（未修正。受け入れるか差し戻すかを決めてください）:\n'+result.review_findings.map(f=>`・${f.reviewer}：${f.summary}`).join('\n');
  if(result.pending_items?.length)text+='\n\n未完了として残す項目（合格・全体完了ではありません）:\n'+result.pending_items.map(item=>`${item.title}\n${item.note}\n${item.checks.join('\n')}`).join('\n\n');
  if(result.resolved_checks?.length)text+='\n\n利用者が後から確認した検証（AIの再検証ではありません）:\n'+result.resolved_checks.map(item=>`${item.title}\n${item.check}\n根拠: ${item.evidence}\n確認日時: ${date(item.confirmed_at)}`).join('\n\n');
  return text;
}
function completionEvidenceForm(approval){
  const choices=[];
  (approval.payload.pending_items||[]).forEach((item,itemIndex)=>(item.checks||[]).forEach((check,checkIndex)=>choices.push({itemIndex,checkIndex,label:`${item.title}：${check}`})));
  if(!choices.length)return null;
  const form=el('form',undefined,'detail-section'),select=el('select'),evidence=el('textarea'),confirmed=el('input'),message=el('p','','hint');
  choices.forEach((choice,index)=>{const option=el('option',choice.label);option.value=String(index);select.append(option);});
  evidence.required=true;evidence.maxLength=2000;evidence.rows=3;evidence.id='evidence-'+approval.id;
  evidence.placeholder='実施日時、対象、方法、結果、確認できる場所など。秘密情報は入力しないでください。';
  confirmed.type='checkbox';confirmed.required=true;confirmed.id='evidence-confirm-'+approval.id;
  const targetLabel=el('label','根拠を追加する未確認項目');targetLabel.append(select);
  const evidenceLabel=el('label','人が確認した検証根拠');evidenceLabel.append(evidence);
  const confirmLabel=el('label',undefined,'check');confirmLabel.append(confirmed,document.createTextNode('この検証を実施し、結果と根拠を確認しました。'));
  const submit=el('button','根拠を記録して受領判断を更新','primary');submit.type='submit';message.setAttribute('role','status');
  form.append(el('h4','後から確認した検証を記録'),el('p','項目ごとに記録します。記録後も成果は自動で通常受領にならず、更新された内容を改めて受領します。AIの再検証ではありません。','hint'),targetLabel,evidenceLabel,confirmLabel,message,submit);
  form.addEventListener('submit',async event=>{event.preventDefault();if(submit.disabled)return;submit.disabled=true;
    const choice=choices[Number(select.value)];
    try{await api('/api/completion/evidence',{approval_id:approval.id,item_index:choice.itemIndex,check_index:choice.checkIndex,evidence:evidence.value,confirmed:confirmed.checked});await refresh();}
    catch(error){message.textContent=error.message;}finally{submit.disabled=false;}});
  return form;
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
    try{const attachment_ids=fileDraft?await fileDraft.widget.upload(api):[];await api('/api/decide',{id:approval.id,allow:true,answers,attachment_ids,...(checklist?{review_checklist:{approved:checklist.approved,instruction:checklist.instruction}}:{})});answerDrafts.delete(approval.id);fileDraft?.widget.clear();answerFiles.delete(approval.id);$('detail-dialog').close();await refresh();}
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
  if(/外部送信ガード/.test(task.summary||''))return {cause:'AIへ渡す内容の送信前検査で停止しました',why:'作業内容の追加判断ではありません。検出対象と送信許可範囲を確認し、誤検出または入力の問題を解消してから再開します。担当・モデルの切替や作業の移管だけでは解消しません。内容：'+(task.summary||'').slice(0,240),kinds:[]};
  return {cause:'担当が決められた回数で工程を終えられませんでした',why:'内容：'+(task.summary||'（記録なし）').slice(0,240),kinds:['retry','model','transfer','cancel']};
}
function failureCard(task,job){
  if(isWorkflowJob(job)&&needsMaintenance(task,job))return maintenanceWaitBox(task);
  if(task.failure_code==='codex_sandbox'||task.failure_code==='claude_auth'){
    const box=el('section',undefined,'failure-card');box.id='failure-card';
    box.append(el('h3','実行環境の障害で一時停止しています'),
      el('p','依頼を中止する必要はありません。作成済みの成果物・完了した工程・承認履歴を保持し、停止した工程から再開できます。'),
      el('p',task.failure_code==='codex_sandbox'?'Codexの実行環境を起動できませんでした。この工程のAI実行は始まっていません。':'Claudeの認証を確認できませんでした。認証情報を回答欄へ入力しないでください。'),
      el('p','実行環境の復旧が必要です。仕様の補足や後続工程への移管では解消しません。'));
    box.append(button('成果を保持して閉じる',()=>{$('detail-dialog').close();}),
      button('環境復旧後、この工程から再開',async()=>{
        try{if(await requestRetry(task,job,{note:'実行環境の復旧後、既存成果と完了済み工程を保持して、この工程のみ再開します。',
          question:'実行環境が復旧した後に使用してください。完了済み工程と成果を保持し、この工程だけを再試行します。再開しますか？',
          legacyNotice:'停止した工程から再開しました。'}))await refresh();}catch(error){notice(error.message);}
      }));
    const detail=el('details');detail.append(el('summary','技術的な停止理由'),el('p',task.summary||'詳細未取得'));box.append(detail);return box;
  }
  const d=failureDiagnosis(task,job),box=el('section',undefined,'failure-card');box.id='failure-card';
  box.append(el('h3',/外部送信ガード/.test(task.summary||'')?'送信前検査で停止しています':'この担当は失敗しました'+(task.attempt?'（試行 '+task.attempt+'回で停止）':'')));
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
        await requestRetry(task,job,{note:'担当の報告の形が決まりに合わなかったため、同じ指示で再試行します。報告は指定の形で返してください。',
          question:'同じ指示のまま、この担当を再試行します。よろしいですか？',legacyNotice:'再試行しました。担当が動き始めます。'});},'primary')],
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
  if(list.children.length)box.append(el('h4','代わりの方法'),list);
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
      try{if(await requestRetry(task,job,{note:'ボードで登録済みの工程間移管を確認しました。最新の移管範囲に従って再開してください。',question:null}))await refresh();
        else restart.disabled=!confirmInput.checked;}
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
let archiveOffset=null,archiveQuery='',archiveGeneration=0,archiveDetailGeneration=0;
async function searchArchive(more=false){
 const generation=++archiveGeneration;
 if(!more){archiveQuery=$('archive-query').value.trim();archiveOffset=0;$('archive-results').replaceChildren();$('archive-detail').replaceChildren();$('archive-more').hidden=true;archiveDetailGeneration++;}
 $('archive-message').textContent='検索しています…';$('archive-more').disabled=true;
 try{
  const data=await api('/api/jobs/archive?q='+encodeURIComponent(archiveQuery)+'&offset='+(archiveOffset||0));
  if(generation!==archiveGeneration)return;
  archiveOffset=data.next_offset;
  for(const job of data.jobs){const row=el('p');row.append(button(`${job.title} — ${jobStatusLabel(job)}`,()=>showArchive(job.id)));$('archive-results').append(row);}
  $('archive-message').textContent=data.total?`${data.total}件中${$('archive-results').childElementCount}件を表示しています。`:'該当する終了案件はありません。';
  $('archive-more').hidden=archiveOffset===null;
 }catch(e){if(generation===archiveGeneration)$('archive-message').textContent='検索できませんでした。「検索」で再試行してください。 '+e.message;}
 finally{if(generation===archiveGeneration)$('archive-more').disabled=false;}
}
async function showArchive(id){
 const generation=++archiveDetailGeneration,area=$('archive-detail');area.replaceChildren(el('p','詳細を読み込んでいます…'));
 try{
  const data=await api('/api/jobs/archive?id='+encodeURIComponent(id));if(generation!==archiveDetailGeneration)return;
  area.replaceChildren(el('h3',data.job.title),el('p',jobStatusLabel(data.job)),el('p',data.job.goal),button('この依頼の評価記録',()=>openEffort(id)));
  for(const task of data.tasks){const box=el('details');box.append(el('summary',`${task.title} — ${statuses[task.status]||task.status}`),el('pre',task.instruction||''),el('pre',typeof task.result==='string'?task.result:JSON.stringify(task.result||{},null,2)));area.append(box);}
  const decisions=el('details');decisions.append(el('summary',`判断履歴 ${data.approvals.length}件`));
  for(const a of data.approvals)decisions.append(el('p',`${a.kind} / ${a.status}${a.note?' / '+a.note:''}`));
  area.append(decisions);area.focus();
 }catch(e){if(generation===archiveDetailGeneration)area.replaceChildren(el('p','詳細を取得できませんでした。案件を選び直してください。 '+e.message));}
}
let effortJob=null,effortRevision=0,effortGeneration=0;
const qualityStates={passed:'確認済み',failed:'不合格',inconclusive:'未確認',invalidated:'無効（確認後に変更されました）'};
const qualityChecks={'CQA-03':'検証コマンド','CQA-06':'テストファイル','DQA-01':'成果物'};
async function loadQuality(id,generation){
 // SQAH Phase 3: read-only. It decides nothing; acceptance stays with the reviews and the person.
 const box=el('div');box.append(el('h3','品質確認（読み取り専用）'),el('p','読み込んでいます…'));$('effort-summary').append(box);
 try{const q=await api('/api/quality/job?id='+encodeURIComponent(id));if(generation!==effortGeneration)return;
  const rows=[el('h3','品質確認（読み取り専用）'),el('p',`全体: ${qualityStates[q.overall]||q.overall}（確認済みでも受領の判断は変えません）`)];
  rows.push(el('p','【采来が確認した事実】'));
  if(!q.facts.length)rows.push(el('p','確認できた事実はありません。'));
  for(const f of q.facts)rows.push(el('p',`${qualityChecks[f.id]||f.id}: ${qualityStates[f.state]||f.state} — ${f.detail}`));
  const reviews=q.findings.reviews.map(r=>`${r.agent} ${r.status||'未報告'}`).join(' / ')||'なし';
  rows.push(el('p','【AIレビューの所見（証拠には数えません）】'),el('p',reviews));
  rows.push(el('p','【未確認】'),el('p',`受領判断に残る未確認項目 ${q.unresolved.pending_items}件`));
  rows.push(el('p','【承認】'),el('p',q.approval.accepted?'通常受領済み':q.approval.conditional?'条件付き受領':`受領判断: ${q.approval.status||'なし'}`));
  rows.push(el('p','この表示は記録を読み直したもので、ファイルや状態は変更しません。検証コマンドの指定がない案件や導入前の案件は「未確認」と表示されます。'));
  box.replaceChildren(...rows);
 }catch(e){if(generation===effortGeneration)box.replaceChildren(el('h3','品質確認（読み取り専用）'),el('p','取得できませんでした。'+e.message));}
}
async function openEffort(id){
 const generation=++effortGeneration;effortJob=id;$('effort-form').hidden=true;$('effort-summary').replaceChildren();$('effort-message').textContent='記録を読み込んでいます…';
 if(!$('effort-dialog').open)$('effort-dialog').showModal();
 try{const data=await api('/api/jobs/evaluation?id='+encodeURIComponent(id));if(generation!==effortGeneration)return;
  $('effort-title').textContent='評価記録: '+data.title;
  effortRevision=data.revision;$('effort-minutes').value=data.record?.minutes??'';$('effort-observer').value=data.record?.observer||'real_user';$('effort-confirmed').checked=false;
  $('effort-summary').replaceChildren(el('p',`受領状態: ${jobStatuses[data.status]||data.status}`),el('p',`承認 ${data.approved_decisions}件 / 拒否 ${data.denied_decisions}件 / 判断待ち ${data.pending_decisions}件`),el('p',`利用者の差し戻し ${data.user_rework_count}回 / 要修正のレビュー報告 ${data.review_needs_changes_reports}件 / タスク再試行 ${data.task_retry_count}回`),el('p','過去のレビュー報告の保存範囲内で集計。受領状態だけでは実利用者本人の確認を証明しません。'));loadQuality(id,generation);
  $('effort-message').textContent=data.record?`保存済み: ${data.record.minutes}分（${data.record.observer==='real_user'?'本人記録':'代理記録'}）。訂正時は累計を入力してください。`:'実作業時間は未測定です。';$('effort-form').hidden=false;
 }catch(e){if(generation===effortGeneration)$('effort-message').textContent='記録を取得できませんでした。再読み込みしてください。 '+e.message;}
}
$('archive-open').addEventListener('click',()=>{$('archive-dialog').showModal();searchArchive();});
$('archive-form').addEventListener('submit',e=>{e.preventDefault();searchArchive();});
$('archive-more').addEventListener('click',()=>searchArchive(true));
$('effort-open').addEventListener('click',()=>{const id=$('filter').value;if(!id){notice('作業ボードの「表示する依頼」で対象を選んでください。');$('filter').focus();return;}openEffort(id);});
$('effort-reload').addEventListener('click',()=>{if(effortJob)openEffort(effortJob);});
$('effort-form').addEventListener('submit',async e=>{
 e.preventDefault();const submit=e.submitter;submit.disabled=true;const id=effortJob;
 try{await api('/api/jobs/evaluation',{job_id:id,revision:effortRevision,minutes:Number($('effort-minutes').value),observer:$('effort-observer').value,confirmed:$('effort-confirmed').checked});if(id===effortJob)await openEffort(id);}
 catch(error){$('effort-message').textContent='保存できませんでした。 '+error.message;}
 finally{submit.disabled=false;}
});
const taskDetails=new Map(),taskDetailLoading=new Set();
function mergeTaskDetails(){for(const t of state.tasks){const d=t.slim&&taskDetails.get(t.id);if(d)Object.assign(t,d,{slim:false});}}
async function loadTaskDetail(task){if(taskDetailLoading.has(task.id))return;taskDetailLoading.add(task.id);try{taskDetails.set(task.id,await api('/api/tasks/detail?id='+encodeURIComponent(task.id)));mergeTaskDetails();const t=state.tasks.find(x=>x.id===task.id);if(t&&selected===t.id&&$('detail-dialog').open)renderDetail(t,detailVersion);}catch(e){notice(e.message);}finally{taskDetailLoading.delete(task.id);}}
function renderDetail(task,version) {
  const dialogHadFocus=$('detail-dialog').open&&$('detail-dialog').contains(document.activeElement)&&document.activeElement!==$('detail-dialog');
  const focusedId=dialogHadFocus?document.activeElement.id:'';
  const checkedBoxes={};$('detail-body').querySelectorAll('input[type=checkbox][id]').forEach(c=>checkedBoxes[c.id]=c.checked);
  if(task.slim){loadTaskDetail(task);$('detail-title').textContent=task.title;$('detail-body').replaceChildren(el('p','詳細を読み込み中…'));renderedDetailTask=null;return;}
  if(renderedDetailTask){const saved=detailFolds.get(renderedDetailTask);if(saved)$('detail-body').querySelectorAll('details[data-fold-key]').forEach(d=>saved.open[d.dataset.foldKey]=d.open);}
  renderedDetailTask=task.id;
  const drafts={};$('detail-body').querySelectorAll('textarea').forEach(t=>drafts[t.id]=t.value);
  detailVersion=version;const job=state.jobs.find(j=>j.id===task.job_id);$('detail-title').textContent=task.title;
  const body=$('detail-body');body.replaceChildren(el('p',`${roles[task.role]} / ${task.profile.adapter} / ${task.profile.model} / ${task.profile.effort} · 試行 ${task.attempt}`,'tech-only'),el('p',`状態: ${statuses[task.status]||task.status} · 依頼の状態: ${jobStatusLabel(job)}`));
  if(job.g2_gate_version===1)body.append(section('AIによる計画作成の開始',`状態: ${job.g2_status==='pending'?'未承認・停止中':job.g2_status==='approved'?'承認済み・開始待ち':job.g2_status==='executing'?'承認済みの計画生成を実行中':job.g2_status==='consumed'?'承認済みの1回を使用済み':job.g2_status==='denied'?'拒否済み':'検証不能'}\nこの承認で許可するのは、AIによる計画作成の1回だけです。計画ができた後は、別に開発計画の確認・承認が必要です。`));
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
    else if(pendingUser(task.id).length)jump('判断材料を読んで回答','.approval-box');
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
  if(task.common_agents?.length)body.append(el('p','担当する共通Agent: '+task.common_agents.join('、'),'hint tech-only'));
  const intake=state.jobs.find(j=>j.id===task.job_id)?.intake_classification;
  if(intake)body.append(section('依頼の一次分類',`${intake.label} / ${intake.provider}\n理由: ${intake.reason}\n分類は作業範囲や権限の承認ではありません。`));
  if(task.decision_record){const d=task.decision_record;body.append(section('判断レイヤーの記録',`動作: ${d.shadow?'シャドー（既存判断を維持）':'適用'}\nProvider: ${d.provider}\n状態: ${d.status}\n理由コード: ${d.reason_code}\n既存判断: ${d.baseline}\n候補: ${d.decision}\n採用: ${d.effective}\n差異: ${d.differs?'あり':'なし'}\n時間: ${d.latency_ms} ms`+(d.usage?'\n'+(d.provider==='ollama'?'ローカル':'API')+'トークン: '+JSON.stringify(d.usage):'\nトークン: 未取得')+(d.confidence!==undefined?'\n集中度（正解確率ではありません）: '+d.confidence:'')+(d.probabilities?'\n候補確率: '+JSON.stringify(d.probabilities):'')));}
  if(task.agent_run){const run=task.agent_run,labels={starting:'起動中',running:'実行中',completed:'実行完了',failed:'失敗',cancelled:'中止',held:'送る前に保留（秘密情報の確認）'};
    body.append(section('Agentの実行情報',`Agent: ${run.name}\n実行方式: ${run.launch_method||'起動確認待ち'}\n状態: ${labels[run.status]||run.status}\nセッションID: ${run.session_id||'未取得'}\n試行: ${run.attempt}\n権限: ${run.sandbox}\n定義SHA256: ${run.definition_hash}`));
    if(run.usage&&Object.keys(run.usage).length)body.append(section('今回のCLIトークン記録',Object.entries(run.usage).map(([key,value])=>`${key}: ${value}`).join('\n')+'\nCLIが返した値です。アカウント利用枠の残量とは異なります。'));
    if(run.context_size)body.append(section('入力サイズの記録',`依頼本文: ${run.context_size.user_prompt_chars}文字\nセッション指示: ${run.context_size.native_instructions_chars}文字\nSkill案内: ${run.context_size.skill_catalog_chars}文字\n文字数はトークン数の代用指標です。`));
    if(run.context_optimization){const o=run.context_optimization,modes={shadow:'記録のみ（入力は変えない）',auto:'自動'},
      reasons={image_candidate:'画像が有利',below_threshold:'文字数が閾値未満',contains_execution_details:'コード・コマンド・パスを含む',policy_blocked:'秘密情報らしき文字列を含む',large_reference_needs_selection:'長すぎる（抽出が先）',no_extractable_part:'画像にできる文章の部分が無い',reference_too_large:'大きすぎる（6万字超）',unsupported_or_unknown:'画像を送れない担当',render_or_readability_fail:'規定の枚数に収まらない',uncertain_cost:'実測の見積もりが無い',insufficient_savings:'削減が基準未満',retry_uses_text:'再試行はテキスト',reference_not_located:'置き換え位置を特定できない',optimizer_error:'判定の失敗'};
      body.append(section('参考情報の画像化',`動作: ${modes[o.optimization_mode]||o.optimization_mode}\n送信方式: ${o.selected_transport==='image'?`画像 ${o.image_pages}枚`:'テキスト'}\n`+(o.references||[]).map(r=>`${r.field||'参考情報'}: ${r.reference_chars??'-'}文字`+(r.partial?`（うち${r.extracted_chars}文字を抜き出し）`:'')+` · ${reasons[r.decision_reason]||r.decision_reason}`+(r.estimated_saving_ratio!=null?` · 推定削減 ${Math.round(r.estimated_saving_ratio*100)}%（実測にもとづく推定）`:'')).join('\n')+'\n本文・画像は記録しません。'));}
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
  // 新方式で保守の確認待ちの作業には、利用者向けの復旧・再試行の操作を出さない（保守の判断と利用者の確認を分ける）。
  const maintenanceWait=isWorkflowJob(job)&&needsMaintenance(task,job);
  if(!endedJob(task)&&!maintenanceWait&&['failed','blocked','interrupted'].includes(task.status))body.append(advicePanel(task));
  if(!endedJob(task)&&!maintenanceWait&&['failed','blocked','interrupted'].includes(task.status)&&['researcher','builder'].includes(task.role))body.append(recoveryPanel(task,job));
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
  if(job.status==='accepted_with_pending_checks'){
    const latest=state.approvals.filter(a=>a.job_id===job.id&&a.kind==='completion').at(-1);
    if(latest?.task_id===task.id&&latest.status==='approved'){
      const evidenceForm=completionEvidenceForm(latest);if(evidenceForm)body.append(evidenceForm);
    }
  }
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
  for(const a of endedJob(task)?[]:pendingUser(task.id)){
    const summary=a.business_summary&&!(a.kind==='tool'&&a.business_summary===GENERIC_APPROVAL_SUMMARY)?el('p',a.business_summary,'business-summary'):null;
    // 利用者の判断欄の目印。折りたたみの枠に包まれた後でも、復旧中の表示が判断欄を隠さないために使う。
    if(summary)summary.dataset.userDecision='1';
    if(a.kind==='feature_review'){
      // 機能の確認は専用の操作だけで行う（汎用の承認では受け付けない）。操作できるかはサーバーの actionable に従う。
      if(a.actionable===true)body.append(featureDecisionForm(a,job));
      else{const held=el('section',undefined,'approval-box');held.append(el('h3','機能の確認'),el('p','いまは確認できません（作業中・期限切れ・依頼の状態の変化など）。再読み込みしても変わらない場合は、依頼の状態を確認してください。','hint'));body.append(held);}
      continue;
    }
    if(a.payload.questions?.length){if(summary)body.append(summary);const form=questionForm(a);form.dataset.userDecision='1';body.append(form);continue;}
    const box=el('section',undefined,'approval-box');box.dataset.userDecision='1';box.append(el('h3',a.kind==='ai_start'?'AIによる計画作成の開始を承認':a.kind==='plan'?'開発計画の確認・承認':a.kind==='completion'?'依頼全体の正式受領':'操作の承認'));
    if(summary)box.append(summary);
    if(a.kind==='ai_start')box.append(el('p','ジョブ登録は完了しています。ここで許可するのは表示されたProviderによる計画生成1回です。計画ができても後続作業は始まらず、別途、開発計画の確認・承認が必要です。モデル利用量が発生します。','notice'));
    if(a.kind==='tool'&&a.payload?.guard)box.append(el('p',(['delete','dynamic'].includes(a.payload.guard.category)?'安全のための確認：':'送信の防御：')+a.payload.guard.label+'。'+a.payload.guard.note+(['delete','dynamic'].includes(a.payload.guard.category)?' 対象と範囲が依頼に必要かを確認してから許可してください。':' 送信先・送る内容・依頼に必要かを確認してから許可してください。'),'notice'));
    if(a.kind==='completion'){
      box.append(el('p','レビュー結果の確認と依頼全体の正式受領は別操作です。レビュー確認だけでは受領状態を変更しません。','notice'));
      box.append(el('p','対象依頼：'+job.title,'hint'));
    }
    if(a.kind==='tool')box.append(toolApprovalView(a));else box.append(el('div',reportText(a.payload),'text-block'));
    if(a.kind==='plan'&&a.workflow_revision)box.append(planRevisionView(a));
    if(a.kind==='completion'){
      box.append(previewPanel(job));
      const evidenceForm=completionEvidenceForm(a);if(evidenceForm)box.append(evidenceForm);
    }
    const input=el('textarea');input.id='note-'+a.id;input.rows=3;input.placeholder='回答・判断の理由（任意）';input.setAttribute('aria-label','承認または拒否の補足');input.value=drafts[input.id]||'';
    box.append(el('p',a.kind==='tool'?'この操作1回だけに適用されます。期限: '+date(a.expires_at):a.kind==='completion'?'差し戻す場合は理由を入力してください。正式受領する場合は、まずレビュー結果の確認を記録してください。':'内容を確認して承認または拒否してください。','hint'),input);
    const actions=el('div',undefined,'actions');
    const hasPending=a.payload.pending_items?.length||a.payload.review_findings?.length;
    actions.append(button(a.kind==='completion'?'差し戻す':'拒否する',()=>api('/api/decide',{id:a.id,allow:false,note:input.value}),'danger'));
    if(a.kind==='completion'){
      if(!a.review_acknowledgement){
        actions.append(button('レビュー結果を確認（受領待ちを維持）',()=>api('/api/jobs/review-confirmation',
          {approval_id:a.id,confirmed:true}),'primary'));
      }else{
        box.append(el('p','レビュー結果の確認済み（受領待ちは継続） '+date(a.review_acknowledgement.decided_at),'notice'));
        box.append(el('p','再起動後に確認証跡が失効した場合は、対象とレビュー結果が変わっていないことを照合して新しい確認要求を発行できます。対象に差異があれば再テスト・再レビューが必要です。','hint'));
        actions.append(button('再起動後のレビュー確認をやり直す',()=>api('/api/jobs/reconfirm-review',
          {approval_id:a.id,confirmed:true}),'secondary'));
        const confirmation=el('input');confirmation.autocomplete='off';confirmation.maxLength=20;
        confirmation.setAttribute('aria-label','正式受領の確認');confirmation.placeholder='正式受領と入力';
        const label=el('label','正式受領する場合は「正式受領」と入力');label.append(confirmation);box.append(label);
        const accept=button(hasPending?'未完了項目を明示して条件付き正式受領':'成果物を正式受領',async()=>{
          if(confirmation.value.trim()!=='正式受領'){notice('正式受領する場合は確認欄に「正式受領」と入力してください。');return;}
          await api('/api/jobs/formal-acceptance',{approval_id:a.id,confirmation:confirmation.value.trim(),note:input.value});
          notice('正式受領しました。この依頼は終了しました。');
        },'primary');actions.append(accept);
      }
    }else{
      // 新方式の計画は、表示した分析の digest を確認として送る。表示していない計画では承認しない。
      const confirmation=a.kind==='plan'&&a.workflow_revision_digest?{workflow_confirmation:a.workflow_revision_digest}:{};
      actions.append(button(a.kind==='ai_start'?'AIによる計画作成を1回開始する':'承認する',async()=>{
        const chosen=transferChoices.get(a.id)||{};
        await api('/api/decide',{id:a.id,allow:true,note:input.value,...confirmation,...(Object.keys(chosen).length?{transfer_decisions:chosen}:{})});
        transferChoices.delete(a.id);
        // W05.05.UI（F4）：承認の結果を文で伝える。開始・完了を約束しない。
        if(a.kind==='plan')notice('計画を承認しました。承認した計画で作業を進めます。機能ごとの確認は、各機能の作業の後に行います。');
        else if(a.kind==='ai_start')notice('AIによる計画作成の開始を承認しました（1回だけ）。計画ができたら、改めて確認をお願いします。');
      },'primary'));
    }
    box.append(actions);body.append(box);
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
  if(!endedJob(task)&&isWorkflowJob(job)&&needsMaintenance(task,job)&&['failed','interrupted','blocked'].includes(task.status)){
    if(!body.querySelector('#maintenance-wait'))body.append(maintenanceWaitBox(task));
  }else if(!endedJob(task)&&['failed','interrupted','blocked','cancelled'].includes(task.status)&&!pending(task.id).some(a=>a.payload.questions?.length)){
    const s=el('section',undefined,'detail-section'),input=el('textarea');input.id='retry-note';input.rows=4;input.placeholder='ここに回答・追加の作業指示、または解決した条件を入力してください。';input.value=drafts[input.id]||'';input.setAttribute('aria-label','回答・再開の補足');
    const label=el('label','回答・補足の入力欄');label.htmlFor=input.id;
    // 2026-10-08 「未実施として残して先へ進める」を、回答欄の文章だけでなく利用者の範囲指定として記録できるようにする。
    const scope=el('input');scope.type='checkbox';scope.id='retry-handoff';const scopeLabel=el('label',undefined,'check');
    scopeLabel.append(scope,el('span','この欄に書いた項目は、未実施として残して先へ進めることを許可する（未実施の項目は合格扱いになりません）'));
    const canScope=['builder','researcher'].includes(task.role);
    s.append(el('h3','回答・補足を入力して再開'),label,input,...(canScope?[scopeLabel]:[]),el('p','入力は再開する担当へ渡します。システムエラーの場合は、原因を解消してから再開してください。','hint'),button('回答・補足を送って再試行',async()=>{
      if(scope.checked&&!input.value.trim()){notice('未実施として残す項目を入力欄に書いてください。');input.focus();return;}
      await requestRetry(task,job,{note:input.value,handoff:scope.checked,
        question:scope.checked?'書いた項目を未実施として残し、先へ進めることを許可して再試行します。よろしいですか？':'既に行われた編集や処理を確認しましたか？ 同じ依頼が再実行されます。'});}));body.append(s);
  }
  if(!['accepted','accepted_with_pending_checks','cancelled'].includes(job.status)){const cancel=button('この依頼全体を中止',()=>requestCancel(job),'danger');cancel.id='cancel-job';body.append(cancel);}
  body.querySelectorAll('textarea').forEach(t=>{if(drafts[t.id])t.value=drafts[t.id];});
  body.querySelectorAll('input[type=checkbox][id]').forEach(c=>{if(checkedBoxes[c.id])c.checked=true;});
  foldDetailSections(body,task);
  // 新形式の依頼では、引き継ぎDBの編集欄と履歴は技術詳細に置く（旧形式の表示は変えない）。
  // 新形式の依頼では、担当・モデルの切り替えは一般画面に出さない（技術詳細で従来どおり使える）。
  if(job.p0_contract_version===1)for(const button of body.querySelectorAll('button'))if(button.textContent.trim()==='担当・モデルを切り替える')button.classList.add('tech-only');
  if(job.p0_contract_version===1)for(const node of body.children){const head=node.textContent||'';if(head.startsWith('依頼の一次分類')||head.startsWith('引き継ぎ情報を登録・訂正')||head.startsWith('引き継ぎDBの履歴')||head.startsWith('停止理由と次の操作')||head.startsWith('ボードから復旧する')||head.startsWith('成果物を差し戻す'))node.classList.add('tech-only');}
  const phase=p0Phase(job);
  if(phase){
    // 復旧中・保守対応待ち・安全上の確認待ちでは、技術的な修理の欄を標準で隠す（技術詳細では従来どおり表示）。中止と成果物は残す。
    const hasUserDecision=pendingUser(task.id).length>0;
    for(const node of [...body.children]){
      if(node.id==='cancel-job'||node.querySelector?.('#cancel-job')||node.querySelector?.('h3')?.textContent==='この依頼の成果物'||
         (hasUserDecision&&(node.matches?.('[data-user-decision]')||node.querySelector?.('[data-user-decision]'))))continue;
      node.classList.add('tech-only');
    }
    const detail=hasUserDecision?'操作の確認待ちがあります。下の承認欄をご確認ください。':phase.detail;
    const status=section('いまの状況',phase.label+'\n'+detail+(job.p0_progress?.next_action?'\n次の動作：'+job.p0_progress.next_action:''));
    status.classList.add('p0-status');status.setAttribute('role','status');body.prepend(status);
  }
  tabifyDetail(body,task);
  // 描き直しで、操作中のボタンが差し替わりフォーカスを失ったときは、承認の枠の見出し（無ければ詳細の題名）へ移す。
  if(dialogHadFocus&&(!$('detail-dialog').contains(document.activeElement)||document.activeElement===$('detail-dialog'))){
    const same=focusedId&&document.getElementById(focusedId);
    const target=same&&body.contains(same)?same:((approvalSummary(body)||body.querySelector('.approval-box h3'))||$('detail-title'));
    if(target!==same)target.tabIndex=-1;
    revealSection(target);target.focus();
  }
}
// 2026-10-08 カードを開くたびに折りたたみを初期状態（停止理由を開く）から始める。以前の開閉を引き継ぐと、停止後も閉じたままになっていた。
// 定期的な描き直しで、フォーカスのあるボタンが差し替わっても、同じ印（data-focus-key）のボタンへフォーカスを戻す。
function currentFocusKey(){return document.activeElement?.dataset?.focusKey||null;}
let focusPending=false;   // 詳細を閉じた直後は、描き直しでフォーカスを失っても、戻り先（進み具合の見出し）へ戻す
// 操作の結果で押したボタンが消える場合（機能の確認・差し戻し）は、結果の行へフォーカスを移す（W05.05.UI、F3）。
let nextFocusKey=null;
function restoreFocus(key){
  // 処理中の表示（id のない modal）がフォーカスを持つ間は何もしない。閉じた直後に hideBusy から呼び直す。
  // 閉じた直後は、閉じた処理中の表示がまだフォーカスを持つため、BODY と同じに扱う。
  const inBusy=typeof busyDialog!=='undefined'&&!!busyDialog&&busyDialog.contains(document.activeElement);
  if(inBusy&&busyDialog.open)return;
  const next=nextFocusKey;nextFocusKey=null;   // 操作の直後の1回だけ使う（後の描き直しで急に移さない）
  if(!inBusy&&document.activeElement&&document.activeElement!==document.body)return;
  // modal のダイアログの間は、外側（inert）の要素へは移さない。
  const modal=[...document.querySelectorAll('dialog[open]')].filter(d=>d.id).at(-1)||null;
  const find=k=>k?[...document.querySelectorAll('[data-focus-key]')].find(e=>e.dataset.focusKey===k&&e.offsetParent!==null&&(!modal||modal.contains(e))):null;
  // 押したボタンが結果で消えた場合（前の鍵が見つからない）も、結果の行の鍵を使う。結果の鍵は1回で使い切る。
  let target=find(key);
  if(!target&&next)target=find(next);
  if(!target&&modal){target=modal.querySelector(':scope > .dialog-notice:not([hidden])')||modal.querySelector(':scope > .dialog-title h2');if(target)target.tabIndex=-1;}
  if(!target&&focusPending){const progress=$('job-progress');target=progress&&!progress.hidden?$('job-progress-title'):$('board');}
  if(target){target.focus({preventScroll:true});focusPending=false;}
}
let focusReturn=null;
function openTask(id){focusReturn=document.activeElement?.dataset?.focusKey||null;selected=id;detailVersion='';detailFolds.delete(id);detailTabs.delete(id);if(renderedDetailTask===id)renderedDetailTask=null;const t=state.tasks.find(t=>t.id===id);renderDetail(t,'');if(!$('detail-dialog').open)$('detail-dialog').showModal();
  // 利用者の判断を待つ作業を開いたときは、最初のフォーカスを承認欄に置く（閉じるボタンから十数回Tabを押さずに済む）。
  if(pendingUser(id).length)focusApproval();}
// 承認欄の見出し（折りたたみの要約）。折りたたんだ後は h3 が要約に移るため、要約を探す。
function approvalSummary(body){
  const box=body.querySelector('.approval-box,[data-user-decision]');
  return box?.closest('details.detail-fold')?.querySelector(':scope > summary')||null;
}
function focusApproval(){
  const body=$('detail-body'),box=body.querySelector('.approval-box,[data-user-decision]');if(!box)return false;
  const target=approvalSummary(body)||box.querySelector('textarea,button')||box;
  if(target===box)box.tabIndex=-1;
  revealSection(target);target.focus();return true;
}
// 詳細を閉じたら、開いたボタンの位置へフォーカスを戻す（画面の描き直しで要素が差し替わっても、印（data-focus-key）で探す）。
// 見つからない（判断が済んで項目が消えた）ときは、進み具合の見出し、無ければ作業ボードへ戻す。
$('detail-dialog').addEventListener('close',()=>{focusPending=true;setTimeout(()=>{focusPending=false;},4000);setTimeout(()=>{
  if(document.activeElement&&document.activeElement!==document.body)return;
  const keyed=focusReturn&&[...document.querySelectorAll('[data-focus-key]')].find(e=>e.dataset.focusKey===focusReturn&&e.offsetParent!==null);
  const progress=$('job-progress'),fallback=progress&&!progress.hidden?$('job-progress-title'):$('board');
  const target=keyed||fallback;if(target){target.focus();if(keyed)focusPending=false;}
},0);});
// 取得・送信を待つあいだは、操作を受け付けない「処理中」の表示を出す（定期更新は表示しない）。
let busyCount=0,busyDialog=null,busyTimer=null,refreshing=null,blockNext=true;
function showBusy(text){
  if(!busyDialog){
    busyDialog=document.createElement('dialog');busyDialog.setAttribute('aria-live','polite');
    busyDialog.style.cssText='width:min(380px,90vw);text-align:center;padding:26px';
    busyDialog.innerHTML='<p style="margin:0;font-weight:700;color:inherit" class="busy-text"></p><p style="margin:10px 0 0;font-size:13px">しばらくお待ちください。完了するまで操作できません。<span class="busy-sec"></span></p>';
    busyDialog.addEventListener('cancel',e=>e.preventDefault());document.body.append(busyDialog);
  }
  busyDialog.querySelector('.busy-text').textContent=text;
  if(busyCount++===0){
    const started=Date.now(),sec=busyDialog.querySelector('.busy-sec');sec.textContent='';
    busyTimer=setInterval(()=>{sec.textContent=' （'+Math.floor((Date.now()-started)/1000)+'秒）';},1000);
    if(!busyDialog.open)busyDialog.showModal();
  }
}
// 「処理中」の表示が閉じた後に、詳細の中でフォーカスを失っていたら、直前にフォーカスのあった要素（無ければ承認の枠の見出し）へ戻す。
let lastDetailFocus='';
$('detail-dialog').addEventListener('focusin',event=>{lastDetailFocus=event.target.id||'';});
function restoreDetailFocus(){
  const dialog=$('detail-dialog');if(!dialog.open)return;
  const active=document.activeElement;if(active&&active!==document.body&&active!==dialog&&dialog.contains(active))return;
  const body=$('detail-body'),same=lastDetailFocus&&document.getElementById(lastDetailFocus);
  const target=same&&body.contains(same)?same:((approvalSummary(body)||body.querySelector('.approval-box h3'))||$('detail-title'));
  if(target!==same)target.tabIndex=-1;
  revealSection(target);target.focus();
}
function hideBusy(){if(busyCount>0&&--busyCount===0){clearInterval(busyTimer);if(busyDialog.open)busyDialog.close();setTimeout(restoreDetailFocus,0);setTimeout(()=>restoreFocus(null),0);}}
function refresh(){
  if(refreshing){
    if(!blockNext)return refreshing;
    blockNext=false;showBusy('最新の状態を取得しています…');return refreshing.finally(hideBusy);
  }
  const block=blockNext;blockNext=false;if(block)showBusy('最新の状態を取得しています…');
  refreshing=loadState().finally(()=>{refreshing=null;if(block)hideBusy();});
  return refreshing;
}
async function loadState(){try{state=await api('/api/state');mergeTaskDetails();const focusKey=currentFocusKey();render();const routeBox=$('job-routing-eval');if(routeBox){routeBox.hidden=state.config?.routing_evaluation?.enabled!==true;if(routeBox.hidden)$('job-route').value='full';}notifyDecisions();restoreFocus(focusKey);}catch(e){$('connection').textContent='接続できません';notice('接続が切れました。サーバーの状態を確認し、画面を再読み込みしてください。 '+e.message);}}
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
    const approvals=pendingUser(task.id);
    const completion=approvals.find(a=>a.kind==='completion');
    if(completion){
      const job=state.jobs.find(j=>j.id===task.job_id);
      const remaining=completion.payload.pending_items?.length;
      return [{task,key:'job-complete:'+completion.id,label:'依頼全体の作業工程が終了しました',
        detail:(job?.title||task.title)+'。'+(remaining?'未完了項目があります。成果物・検証結果・残作業を確認してください。':'成果物と検証結果を確認して受け入れてください。'),
        voice:'依頼全体の作業工程が終了しました。成果を確認してください。',completion:true}];
    }
    if(approvals.length)return [{task,key:approvals.map(a=>a.id).sort().join(':'),label:'判断・確認をお願いします'}];
    if(!p0Phase(state.jobs.find(j=>j.id===task.job_id))&&!workflowMaintenanceWait(task)&&['blocked','failed','interrupted'].includes(task.status)){const reason=stopReason(task);return [{task,key:`${task.id}:${task.attempt}:${task.status}:${reason?.code||'generic'}`,label:reason?.title||(task.status==='blocked'?'判断が必要です':'作業が停止しています'),detail:reason?.detail,voice:reason?.title}];}
    return [];
  }).concat(state.jobs.filter(job=>!['cancelled','accepted','accepted_with_pending_checks'].includes(job.status)).flatMap(job=>{
    const review=stalledReview(job);
    return review&&!p0Phase(job)?[{task:review,key:'repair-limit:'+review.id,label:'自動修正の上限に達し、判断が必要です',
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
$('menu-context-optimization').addEventListener('click',()=>{location.href='/context-optimization';});
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
$('new-job').addEventListener('click',()=>{if(!state)return;
  $('project-select').replaceChildren(...state.config.approved_roots.map(p=>new Option(p,p)));
  const auto=$('new-dialog').querySelector('input[name="auto_execute"]'),autoLabel=auto?.closest('label');
  const autoText=autoLabel&&[...autoLabel.childNodes].find(n=>n.nodeType===Node.TEXT_NODE);
  if(autoText)autoText.nodeValue=' 開発計画の承認後の作業を自動で進める設定（計画生成・計画承認の確認は省略されません）';
  if(autoLabel?.nextElementSibling)autoLabel.nextElementSibling.textContent='依頼の受付ではAIを起動しません。AIによる計画作成の開始を承認し、計画ができたら開発計画を確認・承認してください。';
  const consent=$('new-dialog').querySelector('input[name="consent"]'),consentLabel=consent?.closest('label');
  const consentText=consentLabel&&[...consentLabel.childNodes].find(n=>n.nodeType===Node.TEXT_NODE);
  if(consentText)consentText.nodeValue=' 依頼内容・添付ファイルを隔離ジョブ管理DBへ登録することを確認しました。登録だけではAIへ送信されません。';
  const submit=$('new-dialog').querySelector('form button[type="submit"]');if(submit)submit.textContent='依頼を登録して、計画作成の開始の承認を待つ';
  $('new-dialog').showModal();checkJobInstructionSize();jobSkills.load(api);
});
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
  folderAction(()=>loadFolder('C:\\AI_WorkSpace'));
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
const syncJobKind=()=>{const docs=$('job-kind').value==='documentation';$('job-document').hidden=!docs;$('job-document-format').required=docs;$('job-validation').hidden=docs;$('job-workflow-box').hidden=$('job-workflow-hint').hidden=docs;};
$('job-kind').addEventListener('change',syncJobKind);
const routingBox=document.createElement('details');routingBox.id='job-routing-eval';routingBox.hidden=true;
routingBox.innerHTML='<summary>分離環境で経路を比較する</summary><p class="hint">架空の単一Pythonファイルだけが対象です。通常サービスでは使えません。</p><label>実行経路<select name="route" id="job-route"><option value="full">通常経路</option><option value="light_eval">軽い経路（計画AIのみ省略）</option></select></label><label>変更してよいPythonファイル<input name="light_scope_file" id="job-light-scope" placeholder="例：calculator.py"></label><label>独立した合格条件<textarea name="light_acceptance" id="job-light-acceptance" rows="3" placeholder="例：既存の単体テストをすべて通し、負数の合計も正しく返す"></textarea></label><p class="hint">軽い経路には自動実行、検証モードA、検証用データの確認が必要です。添付・資料作成は使えません。</p>';
$('job-attachments').before(routingBox);
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
  route:{label:'軽い経路の評価',el:()=>$('job-route'),open:()=>{routingBox.open=true;}},
  consent:{label:'確認のチェック',el:()=>$('job-form').elements.consent},
  other:{label:'送信・接続'}};
$('job-form').noValidate=true;
// 同意のチェック欄の文言は画面の状態（隔離ジョブ管理の確認など）で書き換わる。エラー文は、実際の文言を引用する。
function consentLabelText(){return ($('job-form').elements.consent?.closest('label')?.textContent||'').trim()||'依頼内容・添付ファイルと必要な作業情報をAIに送信し、CLIの利用枠を使うことを確認しました。';}
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
  if(f.get('route')==='light_eval'){
    if(docs||!f.has('auto_execute')||f.get('evaluation_mode')!=='A'||!f.has('evaluation_confirmed'))add('route','軽い経路の開始条件を満たしていません。','開発・作業、自動実行、検証モードA、検証用データの確認を選んでください。');
    if(!/^[^\\/]+\.py$/i.test(String(f.get('light_scope_file')||''))||!String(f.get('light_acceptance')||'').trim())add('route','対象ファイルか合格条件が不足しています。','単一のPythonファイル名と具体的な合格条件を入力してください。');
  }
  if(!f.has('consent'))add('consent','チェックが入っていません。','内容を確認のうえ、「'+consentLabelText()+'」にチェックを入れてください。');
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
async function submitJob(f,docs,form){const skills=jobSkills.value();const attachment_ids=await jobImages.upload(api);const route=f.get('route')||'full';const created=await sendWithInputGuard(api,'/api/jobs',{title:f.get('title'),goal:f.get('goal'),project:f.get('project'),auto_execute:f.has('auto_execute'),consent:f.has('consent'),attachment_ids,validation_commands:docs?[]:String(f.get('validation_commands')||'').split(/\r?\n/).map(x=>x.trim()).filter(Boolean),...skills,...(docs?{kind:'documentation',document_format:f.get('document_format')}:f.get('workflow')==='wbs'?{workflow:'wbs'}:{}),...(f.get('evaluation_mode')?{evaluation_mode:f.get('evaluation_mode'),evaluation_confirmed:f.has('evaluation_confirmed')}:{}),...(f.get('token_reduction')==='compare'?{token_reduction_compare:true,evaluation_confirmed:f.has('evaluation_confirmed')}:f.get('token_reduction')?{token_reduction:f.get('token_reduction')}:{}) ,...(route==='light_eval'?{route,light_scope_file:f.get('light_scope_file'),light_acceptance:f.get('light_acceptance')}: {})});$('new-dialog').close();form.reset();syncJobKind();jobImages.clear();jobSkills.reset();notice((created.comparison?'トークン削減の比較のため、計画作成の開始の承認待ちの依頼を2件登録しました。':'依頼を登録しました。AIによる計画作成は、開始の承認待ちで停止中です。')+(created.prepared?.length&&document.body.dataset.view==='technical'?' '+created.prepared.join(' '):''));}
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
  // 2026-10-19 Claude ⇔ GitHub Copilot の自動の切り替えのオン・オフ（既定はオン）。
  const failoverBox=el('fieldset',undefined,'provider-choice');failoverBox.append(el('legend','自動の切り替え'));
  const failoverLabel=el('label',undefined,'check'),failover=el('input');failover.type='checkbox';failover.name='auto-failover';failover.id='auto-failover';
  failover.checked=state.config.auto_failover!==false;
  failoverLabel.append(failover,el('span','Claude と GitHub Copilot を自動で切り替える（ログイン切れ・利用上限・枠切れのとき）'));
  failoverBox.append(failoverLabel,el('p','担当が止まったとき、または使うサービスの枠を使い切っていると分かっているときに、もう一方のサービスで続けます（始める前の切り替えは通知します）。自動で切り替えるのは、データ分類が公開・社内の依頼だけです。オフにすると切り替えずに待機または停止します。中止や手動の切り替えを判断したいときはオフにしてください。','hint'));
  $('profile-inputs').append(failoverBox);
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
  data.auto_failover=f.has('auto-failover');
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

$('job-token-reduction').addEventListener('change',function(){if(this.value==='compare'){const auto=$('job-form').elements.namedItem('auto_execute');if(auto)auto.checked=false;}});
