import fs from 'node:fs/promises';
const GOALS={compound:'Show only alpha02 and alpha04, running, then open the schedules tab and read evaluation schedule.',changed:'Show only alpha02 and alpha04 in the apps tab with search alpha and running filter.',paging:'On the paginated evaluation fixture show page 3 and item21 through item30.'};
const REGISTRY={SEARCH:'Search alpha',RUNNING:'Filter 起動中',ALL:'Filter すべて',APPS:'Select アプリ状態 tab',SCHEDULES:'Select 再起動・スケジュール tab',REFRESH:'Press 更新',NEXT:'Next page',PREVIOUS:'Previous page',STOP:'Stop without executing'};
export class ExtendedEvaluation {
 constructor(tab,root){this.tab=tab;this.root=root;this.rows=[];this.case=null;}
 async observe(){return this.tab.playwright.evaluate(()=>({
  paging:Boolean(document.querySelector('#page')),page:document.querySelector('#page')?.textContent||'',items:[...document.querySelectorAll('#items li')].map(e=>e.textContent),
  next:Boolean(document.querySelector('#next')&&!document.querySelector('#next').disabled),previous:Boolean(document.querySelector('#prev')&&!document.querySelector('#prev').disabled),
  query:document.querySelector('#search-input')?.value||'',filter:document.querySelector('.filter-button.is-active')?.getAttribute('data-filter')||'',tab:document.querySelector('.tab-button.is-active')?.getAttribute('data-tab')||'',
  names:[...document.querySelectorAll('#app-list .app-name')].map(e=>e.textContent),schedule:document.querySelector('#schedule-list strong')?.textContent||'',
  searchLabel:document.querySelector('#search-input')?.getAttribute('aria-label')||'アプリを検索',runningLabel:document.querySelector('[data-filter="running"]')?.textContent||'起動中',
  error:document.querySelector('#error-banner:not([hidden])')?.textContent||document.querySelector('#error')?.textContent||''
 }));}
 async reset(goal){if(await this.tab.url()==='http://127.0.0.1:8794/'+(goal==='paging'?'paging':''))await this.tab.reload();else await this.tab.goto('http://127.0.0.1:8794/'+(goal==='paging'?'paging':''));
  await this.tab.playwright.getByRole('heading',{name:goal==='paging'?'ページング評価画面':'Home App Status',exact:true}).waitFor({state:'visible'});
  if(goal==='paging')await this.tab.playwright.getByText('ページ 1',{exact:true}).waitFor({state:'visible'});
  else await this.tab.playwright.getByText('20 / 20 件',{exact:true}).waitFor({state:'visible'});
 }
 async start(mode,goal,repetition,fault='',layer='normal'){
  await this.reset(goal);if(goal==='changed'){await this.tab.playwright.getByRole('searchbox',{name:'アプリを検索',exact:true}).fill('app');await this.tab.playwright.getByRole('button',{name:'停止・未到達',exact:true}).click();await this.tab.playwright.getByRole('tab',{name:'ポッドキャスト',exact:true}).click();}
  this.case={mode,goal,repetition,fault,layer,status:'running',started:Date.now(),steps:[],requests:[],switches:[],rejected:[],provider:mode==='hybrid'?'ollama':'model',plan:[],forbidden_executions:0,human_interventions:0};this.rows.push(this.case);return this.input();
 }
 achieved(s){const g=this.case.goal;if(g==='paging')return s.page==='ページ 3'&&s.items.join(',')===Array.from({length:10},(_,i)=>'item'+(21+i)).join(',');
  return s.names.join(',')==='alpha02,alpha04'&&s.query==='alpha'&&s.filter==='running'&&(g==='compound'?s.tab==='schedules'&&s.schedule==='evaluation schedule':s.tab==='apps');}
 choices(s){const c={STOP:REGISTRY.STOP};if(s.error)c.REFRESH=REGISTRY.REFRESH;
  if(s.paging){if(s.next)c.NEXT=REGISTRY.NEXT;if(s.previous)c.PREVIOUS=REGISTRY.PREVIOUS;return c;}
  if(s.tab!=='apps')c.APPS=REGISTRY.APPS;else{if(s.query!=='alpha')c.SEARCH=REGISTRY.SEARCH;if(s.filter!=='running')c.RUNNING=REGISTRY.RUNNING;if(s.filter!=='all')c.ALL=REGISTRY.ALL;c.SCHEDULES=REGISTRY.SCHEDULES;}
  return c;}
 async input(){const r=this.case;if(r.status!=='running')return {status:r.status};const s=await this.observe();if(r.steps.at(-1)&&!r.steps.at(-1).after)r.steps.at(-1).after=s;
  if(this.achieved(s))return this.finish('passed',s);if(r.steps.length>=8)return this.finish('action_limit',s);
  if(r.mode==='hybrid'&&r.provider==='ollama'&&r.steps.some(x=>JSON.stringify(x.before)===JSON.stringify(s))){r.switches.push('revisited_state');r.provider='model';r.plan=[];}
  this.state={goal:GOALS[r.goal],visible:s,choices:this.choices(s)};
  if(r.switches.length)this.state.handoff={reason:r.switches.at(-1),previous_actions:r.steps.map(x=>x.action)};
  await fs.writeFile(this.root+'/state.json',JSON.stringify(this.state));await this.save();
  if(r.mode==='fixed'||r.layer==='executor')return {status:'execute',action:s.error?'REFRESH':s.paging?'NEXT':s.tab!=='apps'?'APPS':s.query!=='alpha'?'SEARCH':s.filter!=='running'?'RUNNING':'SCHEDULES'};
  if(r.plan.length)return {status:'execute',action:r.plan.shift()};return {status:'decision_needed',provider:r.provider};}
 async accept(result){const r=this.case;r.requests.push(result);
  if(r.status!=='running'){r.late_result_ignored=true;await this.save();return {status:r.status,ignored:true};}
  if(result.error==='Cancelled')return this.finish('cancelled',await this.observe());
  if(result.error||!Array.isArray(result.actions)||result.actions.some(a=>!REGISTRY[a])||result.actions[0]==='STOP'){
   r.rejected.push(result.error||'invalid_or_stop');if(r.mode==='hybrid'&&r.provider==='ollama'){r.switches.push(result.error||'invalid_or_stop');r.provider='model';r.plan=[];return this.input();}
   return this.finish(result.error==='budget_limit'?'budget_limit':'provider_error',await this.observe());}
  r.plan=result.actions.slice();return this.input();}
 async execute(action){const r=this.case;if(r.status!=='running')return {status:r.status,blocked_after_stop:true};const s=await this.observe();if(!this.choices(s)[action]){r.rejected.push('unavailable:'+action);
   if(r.mode==='hybrid'&&r.provider==='ollama'){r.switches.push('unavailable');r.provider='model';r.plan=[];return this.input();}return this.finish('unavailable_action',s);}
  await this.save();const t=Date.now();
  if(action==='SEARCH')await this.tab.playwright.getByRole('searchbox',{name:s.searchLabel,exact:true}).fill('alpha');
  else if(action==='RUNNING')await this.tab.playwright.getByRole('button',{name:s.runningLabel,exact:true}).click();
  else if(action==='APPS')await this.tab.playwright.getByRole('tab',{name:'アプリ状態',exact:true}).click();
  else if(action==='SCHEDULES')await this.tab.playwright.getByRole('tab',{name:'再起動・スケジュール',exact:true}).click();
  else if(action==='ALL')await this.tab.playwright.getByRole('button',{name:'すべて',exact:true}).click();
  else if(action==='REFRESH'){await this.tab.playwright.getByRole('button',{name:'更新',exact:true}).click();await this.tab.playwright.locator('#refresh-button:not([disabled])').waitFor({state:'visible'});}
  else if(action==='NEXT'||action==='PREVIOUS'){await this.tab.playwright.getByRole('button',{name:action==='NEXT'?'次のページ':'前のページ',exact:true}).click();await this.tab.playwright.getByText('ページ '+(Number(s.page.replace('ページ ',''))+(action==='NEXT'?1:-1)),{exact:true}).waitFor({state:'visible'});}
  else return this.finish('rejected',s);
  r.steps.push({action,before:s,execution_ms:Date.now()-t});return this.input();}
 async finish(status,s){Object.assign(this.case,{status,final:s,elapsed_ms:Date.now()-this.case.started});await this.save();return {status,mode:this.case.mode,goal:this.case.goal,repetition:this.case.repetition};}
 async save(){await fs.writeFile(this.root+'/cases.json',JSON.stringify(this.rows,null,2));}
 async resume(){this.rows=JSON.parse(await fs.readFile(this.root+'/cases.json','utf8'));this.case=this.rows.at(-1);this.case.plan=[];this.case.resumed=true;return this.input();}
}
