'use strict';
const $=id=>document.getElementById(id);
const statusName=s=>({passed:'目標を達成しました',action_limit:'操作上限に達したため停止しました',provider_stop:'通常モデルが停止を選びました',provider_error:'モデル接続に失敗しました',invalid_action:'不正な操作候補を拒否しました',cancelled:'中止済み',interrupted:'再起動により中断',running:'実行中'}[s]||s);
let rows=[],page=1,appliedQuery='',appliedStatus='',active=null,stopped=false,running=false;
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-Agent-Team-UI':'1'}:{},body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok)throw Error(d.error||'通信に失敗しました');return d;}
function reset(){rows=Array.from({length:41},(_,i)=>({id:i+1,title:([21,22,23,31].includes(i+1)?'alpha':'task')+(i+1),status:i===22?'done':'todo'}));page=1;appliedQuery=appliedStatus='';$('query').value=$('status').value='';render();}
function filtered(){return rows.filter(r=>r.title.includes(appliedQuery)&&(!appliedStatus||r.status===appliedStatus));}
function render(){const all=filtered(),visible=all.slice((page-1)*20,page*20);$('page-info').textContent=`${page}ページ / 全${all.length}件`;$('message').textContent=all.length?'':'該当するタスクはありません。';$('next').disabled=page*20>=all.length;$('rows').replaceChildren();for(const r of visible){const li=document.createElement('li');li.dataset.id=r.id;const title=document.createElement('strong');title.textContent=r.title;const select=document.createElement('select');select.setAttribute('aria-label',`テスト用タスク${r.id}の状態`);select.append(new Option('未着手','todo'),new Option('完了','done'));select.value=r.status;select.disabled=r.id!==21;select.addEventListener('change',()=>{r.status=select.value;});li.append(title,select);$('rows').append(li);}}
$('search').onclick=()=>{appliedQuery=$('query').value;appliedStatus=$('status').value;page=1;render();};
$('next').onclick=()=>{if(!$('next').disabled){page++;render();}};
function observe(){return {query:$('query').value,status:$('status').value,page,message:$('message').textContent,nextEnabled:!$('next').disabled,rows:[...$('rows').children].map(li=>({id:Number(li.dataset.id),title:li.querySelector('strong').textContent,status:li.querySelector('select').value}))};}
function execute(action,goal){
  // Fixed IDs and fixed values only. Model output cannot supply selectors, code, URLs or arbitrary text.
  if(action==='FILL_QUERY'){$('query').value=goal==='empty'?'zzznomatch':'alpha';$('query').dispatchEvent(new Event('input',{bubbles:true}));}
  else if(action==='FILTER_TODO'){$('status').value='todo';$('status').dispatchEvent(new Event('change',{bubbles:true}));}
  else if(action==='SEARCH')$('search').click();
  else if(action==='NEXT'){if($('next').disabled)throw Error('無効な次ページ操作');$('next').click();}
  else if(action==='UPDATE_21'){const select=$('rows').querySelector('li[data-id="21"] select');if(!select||select.disabled)throw Error('対象が画面にありません');select.value='done';select.dispatchEvent(new Event('change',{bubbles:true}));}
  else throw Error('登録されていない操作を拒否しました');
}
function controls(value){running=value;$('fixture').inert=value;for(const id of ['run','compare','mode','goal','consent','query','status','search'])$(id).disabled=value;$('cancel').disabled=!value;}
async function one(mode,goal){reset();active=await api('/api/ui-automation/start',{mode,goal,consent:true,normal_adapter:$('normal-adapter').value});let revision=0;
  for(let requests=0;requests<11&&!stopped;requests++){
    $('progress').textContent=`${mode} / ${goal}：操作${revision+1}の判断待ち…`;
    const d=await api('/api/ui-automation/next',{id:active.id,revision,visible:observe()});
    if(stopped)break;
    if(d.status==='retry')continue;
    if(d.status!=='action'){$('progress').textContent=`${mode} / ${goal}：${statusName(d.status)}`;active=null;return;}
    // Controls blocked to humans during automation; fixed executor temporarily enables its own button.
    for(const id of ['search','status','query'])$(id).disabled=false;
    try{execute(d.action,goal);}finally{for(const id of ['search','status','query'])$(id).disabled=true;}
    revision=d.revision;
    $('progress').textContent=`${d.provider}：${d.action}（引き継ぎ${d.switches}回）`;
    await new Promise(resolve=>setTimeout(resolve,120));
  }
  if(active){await api('/api/ui-automation/cancel',{id:active.id});active=null;}
}
async function run(compare){if(running)return;if(!$('consent').checked){$('progress').textContent='利用枠使用の確認にチェックしてください。';return;}controls(true);stopped=false;
  try{if(compare){for(const goal of ['search','page','update','empty'])for(const mode of [$('mode').value,$('mode').value==='model'?'hybrid':'model']){if(stopped)break;await one(mode,goal);}}else await one($('mode').value,$('goal').value);}
  catch(e){$('progress').textContent=e.message;if(active){try{await api('/api/ui-automation/cancel',{id:active.id});}catch{}active=null;}}
  finally{controls(false);await history();}
}
$('run').onclick=()=>run(false);$('compare').onclick=()=>run(true);
$('cancel').onclick=async()=>{stopped=true;$('progress').textContent='中止しました。未実行の操作を取り消します。';if(active)await api('/api/ui-automation/cancel',{id:active.id});};
function total(calls,key){const aliases={input:['input_tokens','inputTokens','prompt_tokens'],cached:['cached_input_tokens','cachedInputTokens'],output:['output_tokens','outputTokens','completion_tokens']};const values=calls.map(c=>{const u=c.usage||{};return aliases[key].map(k=>u[k]).find(v=>Number.isFinite(v));});return !values.length?0:values.some(v=>v===undefined)?'不明':values.reduce((a,b)=>a+b,0);}
async function history(){try{const d=await api('/api/ui-automation/history');$('results').replaceChildren();for(const r of d.records){const calls=[...r.steps.map(s=>s.decision),...r.decisionFailures,...(r.error?[r.error]:[])],cli=calls.filter(c=>c.arm==='model'),local=calls.filter(c=>c.arm==='ollama'),p=document.createElement('p');p.textContent=`${r.arm} / ${r.name}：${statusName(r.status)} ｜ 操作${r.steps.length}・引き継ぎ${r.switches.length} ｜ CLI入力${total(cli,'input')}・キャッシュ${total(cli,'cached')}・出力${total(cli,'output')} ｜ ローカル入力${total(local,'input')}・出力${total(local,'output')}`;$('results').append(p);if(r.status==='running'){const button=document.createElement('button');button.textContent='この未終了テストを中止';button.onclick=async()=>{await api('/api/ui-automation/cancel',{id:r.id});await history();};$('results').append(button);}}$('evidence').textContent=JSON.stringify(d.records,null,2);}catch(e){$('results').textContent=e.message;}}
$('history').onclick=history;reset();history();
