'use strict';
// Request-form skills: checked skills are added to the request's project and handed over as candidates,
// together with the skills matched from the request text. "Use no skills" hands over none.
window.SkillPicker = class {
  constructor(host) {
    this.host=host;this.skills=[];
    const legend=document.createElement('legend');legend.textContent='スキルをプロジェクトに追加して候補にする';
    const hint=document.createElement('p');hint.className='hint';
    hint.textContent='チェックしたスキルは、この依頼のプロジェクトに追加され（スキル一覧の「適用先」）、依頼文から自動で合わせたスキルと一緒に候補として担当に渡ります。使うかどうかは計画担当が依頼に合わせて決めます。道具の権限は増えません。';
    this.list=document.createElement('div');this.list.className='skill-picker-list';
    this.status=document.createElement('p');this.status.className='hint';this.status.setAttribute('role','status');
    const noneLabel=document.createElement('label');noneLabel.className='check';
    this.none=document.createElement('input');this.none.type='checkbox';
    noneLabel.append(this.none,document.createTextNode('この依頼ではスキルを使わない（自動で合わせる分も渡さない）'));
    host.append(legend,hint,this.list,this.status,noneLabel);
    this.none.onchange=()=>this.render();
  }
  async load(api) {
    try{this.skills=(await api('/api/skills?for=request')).skills||[];}
    catch(error){this.skills=[];this.status.textContent='スキル一覧を読み込めませんでした：'+error.message;}
    this.render();
  }
  render() {
    const chosen=new Set(this.ids());
    this.list.replaceChildren();
    for(const skill of this.skills){
      const label=document.createElement('label'),box=document.createElement('input'),name=document.createElement('strong'),desc=document.createElement('span');
      label.className='check skill-picker-item';box.type='checkbox';box.value=skill.id;box.checked=chosen.has(skill.id);box.disabled=this.none.checked;
      name.textContent=skill.display_name||skill.name;desc.className='hint';desc.textContent=' '+String(skill.description||'').slice(0,120);
      box.onchange=()=>this.count();label.append(box,name,desc);this.list.append(label);
    }
    this.count();
  }
  count() {
    if(this.none.checked){this.status.textContent='この依頼ではスキルを担当に渡しません。';return;}
    if(!this.skills.length){this.status.textContent='追加できるスキルはありません。依頼文に合うスキルは自動で候補になります。';return;}
    const n=this.ids().length;
    this.status.textContent=n?n+'件のスキルを追加して候補にします。依頼文に合うスキルも自動で候補になります。':'追加しない場合も、依頼文に合うスキルは自動で候補になります。';
  }
  ids() {return [...this.list.querySelectorAll('input:checked')].map(x=>x.value);}
  value() {
    if(this.none.checked)return {skill_mode:'none',skill_ids:[]};
    const ids=this.ids();
    if(ids.length>10)throw new Error('候補に追加するスキルは10件までにしてください。');
    return {skill_mode:'auto',skill_ids:ids};
  }
  reset() {this.none.checked=false;for(const x of this.list.querySelectorAll('input'))x.checked=false;this.render();}
  set disabled(value) {this.none.disabled=value;for(const x of this.list.querySelectorAll('input'))x.disabled=value||this.none.checked;}
};
// Input Guard: the server asks for confirmation when a request contains personal data or confidential names.
// The person decides; only then is the same request sent again with sensitive_confirmed. Secrets are never resendable.
window.sendWithInputGuard = async (api, path, body) => {
  try { return await api(path, body); }
  catch (error) {
    const tag = '[INPUT_GUARD_CONFIRM] ', message = String(error.message || '');
    if (!message.startsWith(tag)) throw error;
    if (!confirm(message.slice(tag.length) + '\n\nこのまま送信しますか？（キャンセルで入力に戻ります）'))
      throw new Error('送信を取りやめました。内容を見直してから送信してください。');
    return api(path, Object.assign({}, body, {sensitive_confirmed: true}));
  }
};
