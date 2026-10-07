'use strict';
const element=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
async function api(url,body,retried=false){const response=await fetch(url,body?{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Agent-Team-UI':'1'},body:JSON.stringify(body)}:{credentials:'same-origin',cache:'no-store'});const data=await response.json();
 // After a restart the browser session is gone; these exact errors come before any operation, so renew once and retry (same as the work board).
 if(!retried&&response.status===403&&['画面を再読み込みしてください。','ブラウザからの認証済み操作が必要です。'].includes(data.error)){
  const renew=await fetch('/',{credentials:'same-origin',cache:'no-store',redirect:'error'});if(!renew.ok)throw Error('画面の接続を復旧できませんでした。再読み込みしてください。');await renew.text();return api(url,body,true);}
 if(!response.ok)throw Error(data.error||'処理に失敗しました。');return data;}
function download(skill){
 const text=`---\nname: ${skill.name}\ndescription: ${JSON.stringify(skill.description)}\n---\n\n# 適用条件\n${skill.applicability}\n\n# 手順\n${skill.steps}\n\n# 必要な道具と権限\n${skill.tools}\n\n# 検証方法\n${skill.validation}\n\n# 失敗時の対応\n${skill.failure}\n\n今回の利用者の依頼と既存の権限・安全制約を優先してください。\n`;
 const url=URL.createObjectURL(new Blob([text],{type:'text/markdown;charset=utf-8'})),link=element('a');link.href=url;link.download=skill.name+'-SKILL.md';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function load(){
 const status=document.getElementById('status'),list=document.getElementById('list');status.textContent='読み込み中…';
 try{const data=await api('/api/skills');list.replaceChildren();
 const label=path=>{const p=data.projects.find(p=>p.path.toLowerCase()===path.toLowerCase());return p?.name||p?.basic?.name||path;};
 // One entry per skill (version); the projects it is used in are listed inside.
 const groups=new Map();
 for(const item of data.skills){if(!groups.has(item.id))groups.set(item.id,Object.assign({},item,{applied:[]}));groups.get(item.id).applied.push(item.applied_project);}
 for(const skill of groups.values()){const box=element('details');box.className='skill-card'+(skill.imported?' imported':'');
 const name=skill.imported?skill.name:skill.description;
 const scriptCount=skill.imported?(skill.tools.match(/^- /gm)||[]).length:0;
 const head=element('summary'),titleRow=element('div',name);titleRow.className='skill-title';head.append(titleRow);
 if(skill.imported){const desc=element('p',skill.description.split('。')[0]+'。');desc.className='skill-desc';head.append(desc);}
 const badges=element('ul');badges.className='badges';
 const badge=(text,cls)=>{const b=element('li',text);b.className='badge '+cls;badges.append(b);};
 badge(skill.source_current?'有効':'保留',skill.source_current?'ok':'hold');
 badge(skill.imported?'取り込み':'経験から作成','src');
 if(scriptCount)badge('スクリプト '+scriptCount+'件','script');
 badge('適用先 '+skill.applied.length+'件','');
 head.append(badges);
 const chips=element('ul');chips.className='chips';for(const path of skill.applied){const c=element('li',label(path));c.className='chip';chips.append(c);}head.append(chips);
 box.append(head);
 const body=element('div');body.className='skill-body';box.append(body);
 body.append(element('p',skill.source_current?'依頼内容が説明に合う場合に、適用先の依頼で担当へ渡します。':(skill.imported?'保留中：取り込み後にスキルのファイルが変更されています。担当には渡さず、スクリプトも実行できません。':'保留中：元の経験を再確認してください。')));
 const meta=element('dl');meta.className='skill-meta';
 const row=(k,v)=>{meta.append(element('dt',k),element('dd',v));};
 row('作成元',skill.imported?'取り込み（'+skill.skill_dir+'）':label(skill.source_project));row('版',skill.id.slice(0,12));
 if(skill.imported)row('説明',skill.description);
 body.append(meta);
 // Projects using this skill, each with an unassign button.
 body.append(element('h3','適用先プロジェクト'));
 const where=element('ul');where.className='assigned';
 for(const path of skill.applied){
  const li=element('li'),name=element('span',label(path)),off=element('button','解除');off.type='button';off.className='small';
  const project=data.projects.find(p=>p.path.toLowerCase()===path.toLowerCase());
  off.disabled=!project;
  const skillTitle=skill.imported?skill.name:skill.description.slice(0,30);
  off.onclick=async()=>{if(!confirm('「'+label(path)+'」でのスキル「'+skillTitle+'」の使用を解除します。\nこのプロジェクトの依頼では、このスキルを担当に渡さなくなります。よろしいですか？'))return;
   off.disabled=true;try{await api('/api/skills/unassign',{version_id:skill.id,project_id:project.id,confirmed:true});await load();}catch(error){alert(error.message);off.disabled=false;}};
  li.append(name,off);where.append(li);}
 body.append(where);
 for(const [key,title] of Object.entries({applicability:'適用条件',steps:'手順',tools:'道具と権限',validation:'検証',failure:'失敗時の対応'})){body.append(element('h3',title));const text=element('pre',skill[key]);text.className='text-block';body.append(text);}
 const save=element('button','正式版をダウンロード');save.type='button';save.onclick=()=>download(skill);body.append(save);
 const apply=element('div');apply.className='apply-box';apply.append(element('h3','ほかのプロジェクトでも使う'));
 const form=element('form'),select=element('select');select.setAttribute('aria-label','適用先プロジェクト');
 const appliedPaths=new Set(skill.applied.map(p=>p.toLowerCase()));
 for(const project of data.projects){if(appliedPaths.has(project.path.toLowerCase()))continue;const option=element('option',project.name||project.path);option.value=project.id;select.append(option);}
 const checked=element('input');checked.type='checkbox';checked.required=true;const consent=element('label');consent.append(checked,document.createTextNode(' 適用先でも手順・道具・条件が適切で、記載内容を共有してよいことを確認しました。'));
 const submit=element('button','選択したプロジェクトで使用する');submit.type='submit';submit.disabled=!skill.source_current||!select.options.length;const message=element('p');message.setAttribute('role','status');
 if(!select.options.length)message.textContent='台帳のすべてのプロジェクトで使用中です。';
 form.append(select,element('br'),consent,element('br'),submit,message);
 form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;try{await api('/api/skills/apply',{version_id:skill.id,project_id:select.value,reviewed:checked.checked});await load();}catch(error){message.textContent=error.message;submit.disabled=!skill.source_current;}};
 apply.append(form);body.append(apply);
 // Delete the skill from every project (imported files move to data/backups/skills; nothing is hard-deleted).
 const danger=element('div');danger.className='danger-box';danger.append(element('h3','このスキルを削除'));
 danger.append(element('p',skill.imported?'すべての適用先から外し、一覧から消します。スキルのフォルダは控え（data\\backups\\skills）に移します。':'すべての適用先から外し、一覧から消します。元の経験の記録は残ります。'));
 let desktopBox=null;
 if(skill.imported){const l=element('label');desktopBox=element('input');desktopBox.type='checkbox';l.append(desktopBox,document.createTextNode(' デスクトップ（~\\.claude\\skills\\'+skill.name+'）に置いた分も控えに移す'));danger.append(l,element('br'));}
 const del=element('button','削除する');del.type='button';del.className='danger';
 del.onclick=async()=>{const title=skill.imported?skill.name:skill.description.slice(0,40);
  if(!confirm('スキル「'+title+'」を削除します。\n適用先 '+skill.applied.length+'件すべてで使えなくなります。よろしいですか？'))return;
  del.disabled=true;try{await api('/api/skills/delete',{version_id:skill.id,confirmed:true,remove_desktop:!!desktopBox?.checked});await load();}catch(error){alert(error.message);del.disabled=false;}};
 danger.append(del);body.append(danger);list.append(box);
 }status.textContent=groups.size+'件のスキル（適用 '+data.skills.length+'件）を表示しています。';
 renderInbox(data);
 }catch(error){status.textContent=error.message;}
}
function renderInbox(data){
 const area=document.getElementById('inbox');area.replaceChildren();
 if(!data.inbox?.length){area.append(element('p','取り込み待ちのスキルはありません。'));return;}
 for(const item of data.inbox){
  const box=element('details');box.open=true;box.className='skill-card waiting';
  const head=element('summary'),titleRow=element('div',item.name);titleRow.className='skill-title';head.append(titleRow);
  const desc=element('p',item.description||'（説明なし）');desc.className='skill-desc';head.append(desc);
  const badges=element('ul');badges.className='badges';
  const badge=(text,cls)=>{const b=element('li',text);b.className='badge '+cls;badges.append(b);};
  badge('取り込み待ち','hold');
  badge(item.findings.length?'取り込めない指摘 '+item.findings.length+'件':item.reviews?.length?'要確認 '+item.reviews.length+'件':'機械チェック 指摘なし',item.findings.length||item.reviews?.length?'hold':'ok');
  if(item.scripts.length)badge('スクリプト '+item.scripts.length+'件','script');
  badge('ファイル '+item.files.length+'件','');
  head.append(badges);box.append(head);
  const inner=element('div');inner.className='skill-body';box.append(inner);
  if(item.existing)inner.append(element('p','同じ名前のスキルが取り込み済みです。取り込むと新しい版になり、今の版は控え（data/backups/skills）に移ります。'));
  if(item.findings.length){const warn=element('div');warn.className='notice';warn.append(element('strong','機械チェックの指摘（直すまで取り込めません）'));for(const f of item.findings)warn.append(element('p',f));inner.append(warn);}
  else if(!item.reviews?.length)inner.append(element('p','機械チェック：指摘なし（認証情報・個人のパス・AIへの指示の乗っ取りらしい文・ファイルの種類と大きさ）。'));
  const reviewBoxes=[];
  if(item.reviews?.length){const area2=element('div');area2.className='review-box';
   area2.append(element('strong','要確認（AIへの指示の乗っ取りの疑い '+item.reviews.length+'件）：前後の文を読み、普通の文章で問題ないものだけチェックしてください。1件でも未チェックなら取り込めません。'));
   for(const review of item.reviews){const label=element('label'),check=element('input');check.type='checkbox';check.value=review.id;reviewBoxes.push(check);
    const text=element('pre','［'+review.file+'］'+review.reason+'\n…'+review.excerpt+'…');text.className='text-block';
    label.append(check,document.createTextNode(' 誤検知と確認した（指示の乗っ取りではない）'));area2.append(text,label,element('br'));}
   inner.append(area2);}
  inner.append(element('h3','SKILL.md の本文'+(item.body_truncated?'（先頭8000文字）':'')));const body=element('pre',item.body);body.className='text-block';inner.append(body);
  inner.append(element('h3','ファイル（'+item.files.length+'件・'+Math.ceil(item.bytes/1024)+'KB）'));
  const files=element('pre',item.files.map(f=>`${f.kind==='script'?'［スクリプト］':f.kind==='doc'?'［文書］':'［データ］'} ${f.path}  ${f.bytes}B  SHA256 ${f.sha256.slice(0,16)}…`).join('\n'));files.className='text-block';inner.append(files);
  if(item.scripts.length)inner.append(element('p','同梱スクリプト '+item.scripts.length+'件：取り込むと、選んだプロジェクトの開発の依頼で、制作担当が実行できるようになります（取り込み時から変更されていない場合だけ。実行ごとに記録）。中身を必ず確認してください。'));
  const form=element('form'),projects=element('fieldset');projects.append(element('legend','このスキルを使うプロジェクト（1つ以上）'));
  for(const project of data.projects){const label=element('label'),box2=element('input');box2.type='checkbox';box2.value=project.id;label.append(box2,document.createTextNode(' '+(project.name||project.path)));projects.append(label,element('br'));}
  const desktop=element('label'),desktopBox=element('input');desktopBox.type='checkbox';desktop.append(desktopBox,document.createTextNode(' デスクトップアプリの Claude Code でも使う（~\\.claude\\skills に配置）'));
  const consent=element('label'),consentBox=element('input');consentBox.type='checkbox';consentBox.required=true;consent.append(consentBox,document.createTextNode(' 本文・同梱スクリプト・機械チェックの結果を確認し、選んだプロジェクトで使ってよいことを確認しました。'));
  const submit=element('button','取り込む');submit.type='submit';submit.disabled=item.findings.length>0;const message=element('p');message.setAttribute('role','status');
  form.append(projects,desktop,element('br'),consent,element('br'),submit,message);
  form.onsubmit=async event=>{event.preventDefault();const ids=[...projects.querySelectorAll('input:checked')].map(i=>i.value);
   if(!ids.length){message.textContent='プロジェクトを1つ以上選んでください。';return;}
   submit.disabled=true;message.textContent='取り込んでいます…';
   try{const r=await api('/api/skills/import',{folder:item.folder,project_ids:ids,desktop:desktopBox.checked,reviewed:consentBox.checked,confirmed_reviews:reviewBoxes.filter(c=>c.checked).map(c=>c.value)});message.textContent='取り込みました：'+r.name;await load();}
   catch(error){message.textContent=error.message;submit.disabled=false;}};
  inner.append(form);area.append(box);
 }
}
document.getElementById('refresh').onclick=load;load();
