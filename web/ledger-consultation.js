'use strict';
let consultation=null, consultationTimer=null, consultationEpoch=0;
let consultationDraftRevision=null;
let consultationModels=[], consultationModelsLoading=false, consultationModelEpoch=0;
let consultationSending=false, consultationSubmitting=false, consultationRegistering=false, consultationActivityTimer=null, consultationStartedAt=null, consultationPendingMode=null;
const consultDialog=document.getElementById('consultation-dialog');
const consultStatus=text=>{document.getElementById('consultation-status').textContent=text;document.getElementById('consultation-action-status').textContent=text;document.getElementById('consultation-submit-status').textContent=text;};

function consultationSubmissionControls(){
 if(!consultation)return;
 const blocked=consultation.busy||consultationSending||consultationSubmitting||consultationRegistering;
 const prompt=$('consultation-prompt').value.trim();
 const docs=$('consultation-kind').value==='documentation';
 $('consultation-request-scope').value=docs?'documentation':'development';
 $('consultation-document-scope').hidden=!docs;
 $('consultation-submit').disabled=blocked||!consultation.plan_prompt||!prompt||(!docs&&!consultation.job_allowed)||(docs&&(!$('consultation-document-output').value.trim()||!$('consultation-document-confirm').checked))||Boolean(consultation.job_id)||consultation.project_changed||!$('consultation-confirm').checked;
 $('consultation-register').hidden=docs||consultation.job_allowed||Boolean(consultation.job_id)||consultation.project_changed;
 $('consultation-register-consent').parentElement.hidden=$('consultation-register').hidden;
 $('consultation-register-path').hidden=$('consultation-register').hidden;
 $('consultation-register').disabled=blocked||!$('consultation-register-consent').checked;
 if(!consultation.job_id)$('consultation-job-note').textContent=docs?'資料作成は元プロジェクトの登録不要です。別の保存先と資料作成の範囲を確認してください。':consultation.job_allowed?'計画内容を確認すると開始できます。':'改善・改修には対象登録が必要です。本体の資料を作る場合は、上の依頼範囲を「資料作成のみ」に切り替えてください。';
 $('consultation-submit').textContent=consultationSubmitting?'計画を依頼しています…':'この内容で計画を開始';
}
function consultationActivity(){
 const busy=consultationSending||consultation?.busy;
 if(busy&&consultationStartedAt===null)consultationStartedAt=Date.now();
 if(!busy)consultationStartedAt=null;
 $('consultation-activity').hidden=!busy;
 consultDialog.setAttribute('aria-busy',String(Boolean(busy||consultationSubmitting||consultationRegistering)));
 if(busy){
  const mode=consultationPendingMode||consultation.last_mode;
  $('consultation-activity-text').textContent=`実行中：${mode==='plan'?'計画プロンプトを作成':'相談内容を検討'} ／ ${Math.floor((Date.now()-consultationStartedAt)/1000)}秒経過。${consultationSending?'送信を受け付けています。':consultation.progress||'回答を待っています。'} 完了までお待ちください。`;
 }
 $('consultation-send').textContent=busy&&(consultationPendingMode||consultation.last_mode)!=='plan'?'相談を検討中…':'相談を送る';
 $('consultation-plan').textContent=busy&&(consultationPendingMode||consultation.last_mode)==='plan'?'計画プロンプトを作成中…':'計画プロンプトを作る';
 clearInterval(consultationActivityTimer);
 if(busy&&consultDialog.open)consultationActivityTimer=setInterval(consultationActivity,1000);
 consultationSubmissionControls();
}

function selectedConsultationProfile(){
 return {adapter:$('consultation-adapter').value,model:$('consultation-model-select').value,effort:$('consultation-effort').value};
}
function consultationModelControls(){
 if(!consultation)return;
 const selected=$('consultation-model-select').selectedOptions[0];
 const usable=!consultationModelsLoading&&selected&&!selected.disabled&&Boolean($('consultation-effort').value);
 const blocked=consultation.busy||consultationSending||consultationSubmitting||consultationRegistering||Boolean(consultation.job_id);
 $('consultation-send').disabled=blocked||!usable;
 $('consultation-plan').disabled=blocked||!usable||!consultation.messages.length;
 $('consultation-retry').disabled=blocked||!usable;
 for(const id of ['consultation-adapter','consultation-model-select','consultation-effort','consultation-model-refresh'])$(id).disabled=blocked;
}
function consultationEfforts(preferred){
 const row=consultationModels.find(m=>m.id===$('consultation-model-select').value);
 const select=$('consultation-effort');select.replaceChildren();
 for(const value of row?.efforts||[]){const option=node('option',value);option.value=value;select.append(option);}
 if((row?.efforts||[]).includes(preferred))select.value=preferred;
 else if((row?.efforts||[]).includes('medium'))select.value='medium';
 const note=row?.quota?.state==='unknown'?'残量が不明です。実行時に利用枠・権限で停止する可能性があります。':row?.quota?.label||'候補を選択してください。';
 $('consultation-model-note').textContent=note;
 consultationModelControls();
}
async function loadConsultationModels(force=false){
 const epoch=consultationEpoch,request=++consultationModelEpoch,adapter=$('consultation-adapter').value;
 const previous=$('consultation-model-select').value;
 consultationModelsLoading=true;consultationModels=[];
 $('consultation-model-select').replaceChildren(node('option','モデル候補を取得しています…'));
 $('consultation-model-select').firstChild.disabled=true;
 $('consultation-effort').replaceChildren();consultationModelControls();
 $('consultation-model-note').textContent='CLIのモデル候補と取得済みの利用枠を確認しています…';
 try{
  const catalog=await api(`/api/ledger/consultation/models?adapter=${adapter}&refresh=${force?'1':'0'}`);
  if(epoch!==consultationEpoch||request!==consultationModelEpoch)return;
  consultationModels=catalog.models;
  const select=$('consultation-model-select');select.replaceChildren();
  for(const row of catalog.models){
   const option=node('option',`${row.label} — ${row.id} ／ ${row.quota.label}`);
   option.value=row.id;option.disabled=['exhausted','unavailable'].includes(row.quota.state);select.append(option);
  }
  const wanted=catalog.models.find(m=>m.id===previous&&!['exhausted','unavailable'].includes(m.quota.state))||
   catalog.models.find(m=>adapter===consultation.profile.adapter&&m.id===consultation.profile.model&&!['exhausted','unavailable'].includes(m.quota.state))||
   catalog.models.find(m=>m.quota.state==='available')||catalog.models.find(m=>m.quota.state==='unknown');
  if(wanted)select.value=wanted.id;
  else{const option=node('option','利用可能な候補がありません');option.value='';option.disabled=true;select.prepend(option);select.value='';}
  consultationModelsLoading=false;consultationEfforts(consultation.profile.effort);
  if(!catalog.models.length)$('consultation-model-note').textContent=catalog.note;
 }catch(e){
  if(epoch!==consultationEpoch||request!==consultationModelEpoch)return;
  consultationModelsLoading=false;consultationModelControls();$('consultation-model-note').textContent=e.message;
 }
}

function renderConsultation(s){
 consultation=s;
 const profile=s.profile;
 $('consultation-title').textContent=s.project.name+'：改修・指摘事項の相談';
 $('consultation-model').textContent=`${s.calls.length?'最後に使用した担当':'相談の初期設定'}：${profile.adapter} / ${profile.model} / ${profile.effort}`;
 $('consultation-context').value=JSON.stringify(s.project,null,2);
 const research=$('consultation-research');research.replaceChildren();
 const investigation=s.investigation||{};
 research.append(node('p',s.busy?'構成・資料・関連コードを調査して相談を整理しています。':
  s.evidence?.length?'調査したファイルの根拠を確認できます。':'コードの調査根拠はまだありません。'));
 for(const e of s.evidence||[]){
  research.append(node('p',`${e.id}：${e.path}:${e.start_line}（取得範囲 ${e.line_count}行以内） ／ ${new Date(e.checked_at*1000).toLocaleString()}${e.reused?' ／ 変更なし・前回の抜粋を再利用':''}`));
 }
 for(const error of investigation.errors||[])research.append(node('p',error));
 const history=$('consultation-history');history.replaceChildren();
 if(!s.messages.length)history.append(node('p','直したいこと、困っていること、レビューでの指摘を入力してください。'));
 for(const m of s.messages){
  const block=node('section',undefined,'consultation-message '+m.role);
  const kind={improvement:'改善相談',bug:'不具合・指摘',user_voice:'ユーザーの声',documentation:'資料作成'}[m.kind];
  block.append(node('h3',m.role==='user'?'あなた'+(kind?'：'+kind:''):'計画担当'),node('p',m.text));history.append(block);
 }
 $('consultation-usage').textContent=`モデル呼出 ${s.calls.length}/20回。相談はCLIの利用枠を消費します。直近の取得済み利用量：${s.calls.length?JSON.stringify(s.calls.at(-1).usage):'未取得'}`;
 $('consultation-send').disabled=s.busy||Boolean(s.job_id);
 $('consultation-plan').disabled=s.busy||!s.messages.length||Boolean(s.job_id);
 $('consultation-new').disabled=s.busy||consultationSending||consultationSubmitting||consultationRegistering;
 $('consultation-cancel').disabled=!s.busy||s.status==='cancelling';
 $('consultation-retry').hidden=!['failed','cancelled','interrupted'].includes(s.status);
 $('consultation-retry').disabled=s.busy;
 if(consultationDraftRevision!==s.revision){
  $('consultation-prompt').value=s.plan_prompt||'';
  $('consultation-confirm').checked=false;
  consultationDraftRevision=s.revision;
 }
 $('consultation-copy').disabled=!$('consultation-prompt').value.trim();
 $('consultation-job-note').textContent=s.job_id?'作業依頼へ送信済みです。作業ボードで計画を確認してください。':
  s.project_changed?'プロジェクトの場所が変わりました。新しい相談を開始してください。':
  !s.job_allowed?'計画開始にはAI作業対象への登録が必要です。下の対象パスを確認し、登録のチェックを入れて登録してください。':
  !s.plan_prompt?'計画プロンプトを作成すると、ここから作業計画を依頼できます。':'下書きを確認し「対象・変更内容・制約・完了条件を確認しました」にチェックすると、計画開始ボタンを押せます。実装は自動進行しません。';
 $('consultation-register-path').textContent='登録する対象：'+s.project.path;
 consultStatus(s.error|| (s.busy?(s.status==='cancelling'?'中止しています…':s.progress||'計画担当へ接続しています…'):'相談できます。計画プロンプトは下書きです。'));
 consultationModelControls();
 consultationActivity();
 scheduleConsultationPoll();
}

function scheduleConsultationPoll(){
 clearTimeout(consultationTimer);
 if(!consultation?.busy||!consultDialog.open)return;
 const epoch=consultationEpoch,id=consultation.id;
 consultationTimer=setTimeout(async()=>{
  try{
   const value=await api('/api/ledger/consultation?id='+encodeURIComponent(id));
   if(epoch===consultationEpoch&&consultDialog.open)renderConsultation(value);
  }catch(e){
   if(epoch===consultationEpoch){consultStatus(e.message+' 接続が戻ると再取得します。');scheduleConsultationPoll();}
  }
 },1200);
}

async function openConsultation(fresh=false){
 if(!selected)return;
 const epoch=++consultationEpoch;
 $('consultation-open').disabled=true;
 try{
  const value=await api('/api/ledger/consultation/open',{project_id:selected.id,new:fresh});
  if(epoch!==consultationEpoch)return;
  consultationDraftRevision=null;
  $('consultation-kind').value=value.documentation?'documentation':'improvement';
  $('consultation-document-confirm').checked=false;
  $('consultation-input').value='';$('consultation-consent').checked=false;
  if(!consultDialog.open)consultDialog.showModal();
  $('consultation-adapter').value=value.profile.adapter;
  consultationModelsLoading=true;
  renderConsultation(value);
  loadConsultationModels();
 }catch(e){message(e.message,true);if(consultDialog.open)consultStatus(e.message);}
 finally{$('consultation-open').disabled=false;}
}

async function sendConsultation(mode,retry=false){
 if(!consultation||consultation.busy||consultationSending)return;
 const chosen=$('consultation-model-select').selectedOptions[0];
 if(consultationModelsLoading||!chosen||chosen.disabled||!$('consultation-effort').value){consultStatus('利用できるモデルと推論設定を選択してください。');return;}
 if(!$('consultation-consent').checked){consultStatus('モデルへの送信と利用枠の使用を確認してください。');return;}
 const input=$('consultation-input'),text=input.value.trim();
 if(mode==='discuss'&&!text&&!retry){consultStatus('相談内容を入力してください。');input.focus();return;}
 if(retry&&text){consultStatus('再試行は前の内容を送信します。入力した補足を送る場合は「相談を送る」を使ってください。');return;}
 const epoch=consultationEpoch;
 consultationSending=true;consultationPendingMode=mode;consultationActivity();consultationModelControls();
 $('consultation-send').disabled=true;$('consultation-plan').disabled=true;$('consultation-retry').disabled=true;
 try{
  const value=await api('/api/ledger/consultation/send',{id:consultation.id,revision:consultation.revision,
   mode,message:text,kind:$('consultation-kind').value,consent:true,retry,profile:selectedConsultationProfile()});
  if(epoch!==consultationEpoch)return;
  input.value='';renderConsultation(value);
 }catch(e){
  if(epoch!==consultationEpoch)return;
  consultStatus(e.message);
  try{const value=await api('/api/ledger/consultation?id='+encodeURIComponent(consultation.id));renderConsultation(value);consultStatus(e.message);}
  catch{consultStatus('受付状態が未確認です。重複送信せず、一度閉じて相談を開き直してください。入力内容はこの欄に残っています。');}
 }finally{
  if(epoch===consultationEpoch){consultationSending=false;consultationPendingMode=null;consultationActivity();consultationModelControls();}
 }
}

$('consultation-open').onclick=()=>openConsultation();
$('consultation-adapter').onchange=()=>loadConsultationModels();
$('consultation-model-select').onchange=()=>consultationEfforts($('consultation-effort').value);
$('consultation-effort').onchange=consultationModelControls;
$('consultation-model-refresh').onclick=()=>loadConsultationModels(true);
$('consultation-new').onclick=()=>openConsultation(true);
$('consultation-close').onclick=()=>consultDialog.close();
consultDialog.addEventListener('close',()=>{clearTimeout(consultationTimer);clearInterval(consultationActivityTimer);consultationSending=false;consultationStartedAt=null;consultationPendingMode=null;consultationEpoch++;});
$('consultation-form').onsubmit=e=>{e.preventDefault();sendConsultation('discuss');};
$('consultation-plan').onclick=()=>sendConsultation('plan');
$('consultation-retry').onclick=()=>sendConsultation('discuss',true);
$('consultation-cancel').onclick=async()=>{
 try{renderConsultation(await api('/api/ledger/consultation/cancel',{id:consultation.id}));}
 catch(e){consultStatus(e.message);}
};
$('consultation-prompt').oninput=()=>{$('consultation-copy').disabled=!$('consultation-prompt').value.trim();$('consultation-confirm').checked=false;consultationSubmissionControls();};
$('consultation-confirm').onchange=consultationSubmissionControls;
$('consultation-register-consent').onchange=consultationSubmissionControls;
$('consultation-register').onclick=async()=>{
 if(!consultation||!$('consultation-register-consent').checked||consultationRegistering)return;
 consultationRegistering=true;consultationSubmissionControls();consultationModelControls();
 const epoch=consultationEpoch,id=consultation.id,path=consultation.project.path;
 consultStatus('対象プロジェクトをAI作業対象に登録しています…');
 try{
  await api('/api/projects',{path,consent:true});
  const value=await api('/api/ledger/consultation?id='+encodeURIComponent(id));
  if(epoch!==consultationEpoch)return;
  renderConsultation(value);
  $('consultation-register-consent').checked=false;
  consultStatus('AI作業対象に登録しました。計画の確認チェックを入れて、計画を開始してください。');
 }catch(e){if(epoch===consultationEpoch)consultStatus(e.message);}
 finally{consultationRegistering=false;consultationSubmissionControls();consultationModelControls();}
};
$('consultation-copy').onclick=async()=>{
 try{await navigator.clipboard.writeText($('consultation-prompt').value);consultStatus('計画プロンプトをコピーしました。');}
 catch{$('consultation-prompt').focus();$('consultation-prompt').select();consultStatus('コピーできませんでした。選択した文章を手動でコピーしてください。');}
};
$('consultation-submit').onclick=async()=>{
 if(!$('consultation-confirm').checked){consultStatus('計画プロンプトの内容を確認してください。');return;}
 if(consultationSubmitting)return;
 const epoch=consultationEpoch,id=consultation.id;
 consultationSubmitting=true;consultationSubmissionControls();consultationModelControls();consultStatus('作業ボードへ計画を依頼しています…');
 try{
  await api('/api/ledger/consultation/submit',{id,revision:consultation.revision,
   plan_prompt:$('consultation-prompt').value,confirmed:true,kind:$('consultation-kind').value,
   document_output:$('consultation-document-output').value,output_confirmed:$('consultation-document-confirm').checked});
  const value=await api('/api/ledger/consultation?id='+encodeURIComponent(id));
  if(epoch!==consultationEpoch)return;
  renderConsultation(value);
  consultStatus('計画を依頼しました。作業ボードで進捗と計画の承認待ちを確認してください。');
 }catch(e){if(epoch===consultationEpoch)consultStatus(e.message);}
 finally{consultationSubmitting=false;consultationSubmissionControls();consultationModelControls();}
};

$('consultation-kind').onchange=()=>{consultationSubmissionControls();if($('consultation-kind').value==='documentation')$('consultation-job-note').textContent='資料作成は元プロジェクトの登録不要です。別の保存先と資料作成の範囲を確認してください。';};
$('consultation-document-output').oninput=consultationSubmissionControls;
$('consultation-document-confirm').onchange=consultationSubmissionControls;

$('consultation-request-scope').onchange=()=>{$('consultation-kind').value=$('consultation-request-scope').value==='documentation'?'documentation':'improvement';$('consultation-document-confirm').checked=false;consultationSubmissionControls();};
