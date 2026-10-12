'use strict';
// 参考情報の画像化（トークン削減）の設定（2026-10-10）。保存できるのは ON/OFF（動作）と基準（10〜30%）だけ。
// ON = auto、OFF で記録する = shadow（比較用）、OFF で記録しない = off。ほかの値は config.json と校正で決める。
const $=id=>document.getElementById(id);
const roleNames={planner:'計画',builder:'実装',researcher:'調査',reviewer:'レビュー'};
const reasons={image_candidate:'画像が有利',below_threshold:'文字数が閾値未満',contains_execution_details:'コード・コマンド・パスを含む',policy_blocked:'秘密情報らしき文字列を含む',
  no_extractable_part:'画像にできる文章の部分が無い',reference_too_large:'大きすぎる（6万字超）',unsupported_or_unknown:'画像を送れない担当',render_or_readability_fail:'規定の枚数に収まらない',
  uncertain_cost:'実測の見積もりが無い',insufficient_savings:'削減が基準未満',retry_uses_text:'再試行はテキスト',reference_not_located:'置き換え位置を特定できない',optimizer_error:'判定の失敗'};
let data=null;
const pct=v=>v==null?'-':Math.round(v*100)+'%';
const num=v=>v==null?'-':Number(v).toLocaleString();
const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;return e;};
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-Agent-Team-UI':'1'}:{},body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok)throw Error(d.error||'通信に失敗しました');return d;}
function table(headers,rows){const t=node('table');const h=t.createTHead().insertRow();for(const x of headers)h.append(node('th',x));const b=t.createTBody();for(const row of rows){const tr=b.insertRow();for(const x of row)tr.append(node('td',x));}return t;}
const chosenMode=()=>$('reduction').checked?'auto':$('record').checked?'shadow':'off';
function verdict(role,ratio){
  if(!role.image_input)return 'テキスト（画像に未対応）';
  const m=role.measured;
  if(!m)return 'テキスト（このモデルの実測が無い）';
  if(!m.usable)return 'テキスト（実測を使えない：'+m.unusable_reason+'）';
  const first=m.saving_by_length.find(([,saving])=>saving>=ratio);
  const best=Math.max(...m.saving_by_length.map(([,saving])=>saving));
  const large=data.settings.large_reference_chars.toLocaleString();
  return (first?`約${first[0].toLocaleString()}字以上の参考情報で画像（推定の削減 ${pct(first[1])}〜${pct(best)}）`
    :`テキスト（この基準では画像にならない。推定の削減は最大 ${pct(best)}）`)
    +(first?`。${large}字以上は、文章の部分を抜き出して画像に`:'');
}
function render(){
  if(!data)return;   // before the first load (the controls are disabled until then)
  const s=data.settings,ratio=Number($('ratio').value)/100,mode=chosenMode();
  $('ratio-value').textContent=$('ratio').value+'%';$('ratio').setAttribute('aria-valuetext',$('ratio').value+'%');
  $('record').disabled=$('reduction').checked;
  if($('reduction').checked)$('record').checked=true;
  $('status').textContent={auto:'トークン削減：ON（条件を満たした参考情報を画像で送る）',shadow:'トークン削減：OFF（テキストで送り、判定は記録する）',off:'トークン削減：OFF（記録もしない）'}[mode]
    +(mode!==s.mode?' — 未保存':'');
  $('roles').replaceChildren(table(['担当','接続','モデル','判定の見込み'],data.roles.map(r=>[roleNames[r.role]||r.role,r.adapter||'-',r.model||'-',
    mode==='off'?'使わない設定':verdict(r,ratio)+(mode==='shadow'?'（OFF のため記録だけ）':'')])));
  $('fixed').textContent=`文字サイズ ${s.min_font_px}px・幅 ${s.render_width_px}px・最大 ${s.max_generated_pages}枚・閾値 Claude ${s.claude_min_chars}字／Codex ${s.codex_min_chars}字・抜き出し ${s.large_reference_chars}字以上（これらは config.json で変更し、文字サイズを変えたら測り直してください）`;
}
function load(d){
  data=d;const s=d.settings;
  for(const control of $('settings-form').querySelectorAll('input,button'))control.disabled=false;
  $('reduction').checked=s.mode==='auto';$('record').checked=s.mode!=='off';
  $('ratio').min=Math.round(d.ratio_range[0]*100);$('ratio').max=Math.round(d.ratio_range[1]*100);
  $('ratio').value=Math.round(s.minimum_estimated_saving_ratio*100);
  $('measurements').replaceChildren(d.measurements.length?table(['モデル','文字サイズ','実測の削減','読み取り','測った日','使えるか'],d.measurements.map(m=>[
    m.adapter+' / '+m.model,m.font_px?m.font_px+'px':'-',pct(m.measured_saving_ratio),m.readable?'正確':'確認できない',
    m.measured_at?new Date(m.measured_at*1000).toLocaleString('ja-JP'):'-',m.usable?'使える':'使えない（'+m.unusable_reason+'）'])):node('p','実測の記録はありません。','hint'));
  const r=d.records,t=r.by_transport||{},modes=r.by_mode||{};
  const names={auto:'ON',shadow:'OFF（記録だけ）'};
  $('comparison').replaceChildren(Object.keys(modes).length?table(['トークン削減','作業数','成功','入力トークンの平均','出力トークンの平均','画像で送った作業'],
    ['auto','shadow'].filter(k=>modes[k]).map(k=>[names[k],String(modes[k].runs),`${modes[k].succeeded}/${modes[k].runs}`,
      `${num(modes[k].input_tokens_mean)}（${modes[k].input_tokens_runs}件）`,num(modes[k].output_tokens_mean),k==='auto'?String(t.image?.runs||0):'0'])):node('p','まだ記録がありません。','hint'));
  const statusNames={planning:'計画中',running:'実行中',awaiting_approval:'承認待ち',awaiting_acceptance:'成果確認待ち',blocked:'判断待ち',accepted:'受け入れ済み',
    accepted_with_pending_checks:'受け入れ（未確認あり）',cancelled:'中止',failed:'失敗',interrupted:'中断'};
  const change=v=>v==null?'-':(v>0?'+':'')+Math.round(v*1000)/10+'%';
  $('pairs').replaceChildren(d.comparisons?.length?table(['依頼','側','状態','実行数','入力トークン（合計）','うちキャッシュ読込','出力トークン','重み付き','Copilotクレジット','画像で送った実行','ON の増減'],
    d.comparisons.flatMap(c=>['off','on'].filter(k=>c.sides[k]).map(k=>{const s=c.sides[k];return [
      s.title||'-',k==='on'?'ON':'OFF',statusNames[s.status]||s.status,String(s.runs)+(s.unmeasured_runs?`（測定なし ${s.unmeasured_runs}）`:''),
      num(s.input_total)+(s.complete?'':'（不完全）'),num(s.cache_read),num(s.output),num(s.weighted),s.copilot_credits==null?'-':String(s.copilot_credits),String(s.image_runs),
      k==='on'?`入力 ${change(c.input_change_ratio)}・重み付き ${change(c.weighted_change_ratio)}`:''];})))
    :node('p','比較用の依頼はまだありません。','hint'));
  const box=node('div');
  box.append(node('p',`記録した実行：${r.runs_recorded}件（画像 ${t.image?.runs||0}件・テキスト ${t.text?.runs||0}件）`));
  if(r.runs_recorded)box.append(table(['判定の理由','件数'],Object.entries(r.decision_reasons).map(([k,v])=>[reasons[k]||k,String(v)])));
  box.append(node('p',r.note,'hint'));
  $('records').replaceChildren(box);
  render();
}
$('ratio').addEventListener('input',render);
$('reduction').addEventListener('change',render);
$('record').addEventListener('change',render);
$('settings-form').addEventListener('submit',async e=>{
  e.preventDefault();$('save').disabled=true;$('message').textContent='';
  try{
    await api('/api/context-optimization',{mode:chosenMode(),minimum_estimated_saving_ratio:Number($('ratio').value)/100});
    load(await api('/api/context-optimization'));
    $('message').textContent='保存しました。次に始まる作業から使います（実行中の作業はそのまま）。';
  }catch(err){$('message').textContent=err.message;}
  finally{$('save').disabled=false;}
});
for(const control of $('settings-form').querySelectorAll('input,button'))control.disabled=true;
$('message').textContent='読み込み中…';
api('/api/context-optimization').then(d=>{$('message').textContent='';load(d);}).catch(err=>{$('message').textContent=err.message;});
