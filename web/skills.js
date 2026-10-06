'use strict';
const element=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
async function api(url,body){const response=await fetch(url,body?{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Team-UI':'1'},body:JSON.stringify(body)}:{});const data=await response.json();if(!response.ok)throw Error(data.error||'処理に失敗しました。');return data;}
function download(skill){
 const text=`---\nname: ${skill.name}\ndescription: ${JSON.stringify(skill.description)}\n---\n\n# 適用条件\n${skill.applicability}\n\n# 手順\n${skill.steps}\n\n# 必要な道具と権限\n${skill.tools}\n\n# 検証方法\n${skill.validation}\n\n# 失敗時の対応\n${skill.failure}\n\n今回の利用者の依頼と既存の権限・安全制約を優先してください。\n`;
 const url=URL.createObjectURL(new Blob([text],{type:'text/markdown;charset=utf-8'})),link=element('a');link.href=url;link.download=skill.name+'-SKILL.md';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function load(){
 const status=document.getElementById('status'),list=document.getElementById('list');status.textContent='読み込み中…';
 try{const data=await api('/api/skills');list.replaceChildren();
 const label=path=>{const p=data.projects.find(p=>p.path.toLowerCase()===path.toLowerCase());return p?.name||p?.basic?.name||path;};
 for(const skill of data.skills){const box=element('details');box.append(element('summary',skill.description+' — '+label(skill.applied_project)));
 box.append(element('p',skill.source_current?'有効：依頼内容に合う場合に参照します。':'保留：元の経験を再確認してください。'),element('p','作成元：'+label(skill.source_project)),element('p','版：'+skill.id.slice(0,12)));
 for(const [key,title] of Object.entries({applicability:'適用条件',steps:'手順',tools:'道具と権限',validation:'検証',failure:'失敗時の対応'})){box.append(element('h3',title));const text=element('pre',skill[key]);text.className='text-block';box.append(text);}
 const save=element('button','正式版をダウンロード');save.type='button';save.onclick=()=>download(skill);box.append(save);
 const form=element('form'),select=element('select');select.setAttribute('aria-label','適用先プロジェクト');
 for(const project of data.projects){const option=element('option',project.name||project.path);option.value=project.id;select.append(option);}
 const checked=element('input');checked.type='checkbox';checked.required=true;const consent=element('label');consent.append(checked,document.createTextNode('適用先でも手順・道具・条件が適切で、記載内容を共有してよいことを確認しました。'));
 const submit=element('button','選択したプロジェクトで使用する');submit.type='submit';submit.disabled=!skill.source_current;const message=element('p');message.setAttribute('role','status');form.append(select,consent,submit,message);
 form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;try{await api('/api/skills/apply',{version_id:skill.id,project_id:select.value,reviewed:checked.checked});await load();}catch(error){message.textContent=error.message;submit.disabled=!skill.source_current;}};box.append(form);list.append(box);
 }status.textContent=data.skills.length+'件の適用を表示しています。';
 }catch(error){status.textContent=error.message;}
}
document.getElementById('refresh').onclick=load;load();
