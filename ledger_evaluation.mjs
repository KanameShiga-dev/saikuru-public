// Browser skill supplies the tab. No external browser, Computer Use or raw automation.
import fs from 'node:fs/promises';
export class LedgerEvaluation {
 constructor(tab,root){this.tab=tab;this.root=root;this.rows=[];this.case=null;}
 async observe(){return this.tab.playwright.evaluate(()=>{
  const ledger=Boolean(document.querySelector('#list')),dialog=document.querySelector('#editor');
  const metadata=document.querySelector('#metadata-section');
  const dt=[...document.querySelectorAll('#metadata dt')].find(e=>e.textContent==='README表題');
  return {page:ledger?'ledger':'board',query:document.querySelector('#search')?.value||'',category:document.querySelector('#category')?.value||'',
   names:ledger?[...document.querySelectorAll('#list article > button')].map(e=>e.textContent):[],
   dialog:Boolean(dialog?.open),detail:dialog?.open?document.querySelector('#editor-title').textContent:'',
   metadataOpen:Boolean(dialog?.open&&metadata?.open),readme:dialog?.open&&metadata?.open?dt?.nextElementSibling?.textContent||'':'',
   board:!ledger?document.querySelector('.board-heading h2')?.textContent||'':''};
 });}
 async start(mode,goal,repetition,fault=''){
  await this.tab.goto('http://127.0.0.1:8793/ledger');
  await this.tab.playwright.getByRole('button',{name:'project1',exact:true}).waitFor({state:'visible'});
  if(fault==='screen_change'){
   await this.tab.playwright.getByRole('searchbox',{name:'名前・パス・目的を検索',exact:true}).fill('alpha');
   await this.tab.playwright.getByRole('combobox',{name:'分類',exact:true}).selectOption('資料');
  }
  this.case={mode,goal,repetition,fault,status:'running',started:Date.now(),steps:[],requests:[],switches:[],
   rejected:[],provider:mode==='hybrid'?'ollama':'model',stalls:0,plan:[],human_interventions:0,human_corrections:0,
   forbidden_executions:0};this.rows.push(this.case);await this.save();return this.input();
 }
 achieved(s){const g=this.case.goal;
  if(g==='search')return s.query==='alpha'&&s.names.join(',')==='alpha21,alpha22,alpha23,alpha31';
  if(g==='filter')return s.query==='alpha'&&s.category==='アプリ'&&s.names.join(',')==='alpha21,alpha22,alpha31';
  if(g==='read')return s.detail==='alpha21'&&s.metadataOpen&&s.readme==='evaluation project21';
  return s.page==='board'&&s.board.startsWith('作業ボード');
 }
 choices(s){const c={STOP:'Stop if no safe action reaches the goal.'};
  if(s.page==='ledger'&&!s.dialog){
   if(s.query!=='alpha')c.FILL_SEARCH='Fill search with alpha; filtering is immediate.';
   if(s.category!=='アプリ')c.FILTER_APPS='Select category アプリ (apps); filtering is immediate.';
   if(s.category)c.CLEAR_CATEGORY='Clear the category filter.';
   if(s.names.includes('alpha21'))c.OPEN_DETAILS='Open alpha21 project details.';
   c.NAV_BOARD='Follow 作業ボード to the work board.';
  }
  if(s.dialog){c.CLOSE_DETAILS='Close the project dialog.';if(!s.metadataOpen)c.EXPAND_METADATA='Expand 確認したメタ情報 to read the metadata.';}
  return c;
 }
 async input(){const s=await this.observe(),r=this.case,previous=r.steps.at(-1);
  if(previous&&!previous.after){previous.after=s;r.stalls=JSON.stringify(previous.before)===JSON.stringify(s)?r.stalls+1:0;}
  if(this.achieved(s))return this.finish('passed',s);
  if(r.steps.length>=8)return this.finish('action_limit',s);
  if(r.mode==='hybrid'&&r.provider==='ollama'&&r.stalls>=2)this.switch('no_progress',s);
  const goals={search:'Show exactly alpha21, alpha22, alpha23, alpha31 using search.',filter:'Show exactly alpha21, alpha22, alpha31 by searching alpha and selecting category アプリ.',read:'Open alpha21 details and expand 確認したメタ情報 to read README title evaluation project21.',navigate:'Navigate from the ledger to the work board; confirm the 作業ボード heading.'};
  const state={goal:goals[r.goal],visible:s,choices:this.choices(s)};
  if(r.switches.length)state.handoff={reason:r.switches[0].reason,previous_actions:r.steps.map(x=>x.action),instruction:'Continue from current state. Avoid no-progress actions.'};
  this.state=state;await fs.writeFile(this.root+'/state.json',JSON.stringify(state));await this.save();
  if(r.mode==='fixed'){
   let action;
   if(r.goal==='search')action='FILL_SEARCH';
   else if(r.goal==='filter')action=s.query!=='alpha'?'FILL_SEARCH':'FILTER_APPS';
   else if(r.goal==='read')action=s.dialog?'EXPAND_METADATA':s.names.includes('alpha21')?'OPEN_DETAILS':'CLEAR_CATEGORY';
   else action='NAV_BOARD';
   return {status:'execute',action};
  }
  if(r.plan.length)return {status:'execute',action:r.plan.shift()};
  return {status:'decision_needed',provider:r.provider,fault:r.fault&&!r.requests.length&&r.fault!=='screen_change'?r.fault:''};
 }
 switch(reason,state){const r=this.case;if(r.provider!=='ollama')return;r.switches.push({reason,after_actions:r.steps.length,state});r.provider='model';r.plan=[];}
 async accept(result){const r=this.case;r.requests.push(result);
  if(result.error||!Array.isArray(result.actions)||!result.actions.length||result.actions.some(a=>!['FILL_SEARCH','FILTER_APPS','CLEAR_CATEGORY','OPEN_DETAILS','EXPAND_METADATA','CLOSE_DETAILS','NAV_BOARD','STOP'].includes(a))||result.actions[0]==='STOP'){
   r.rejected.push(result.error||'invalid_or_stop');
   if(r.mode==='hybrid'&&r.provider==='ollama'&&r.fault!=='cancel'){this.switch(result.error||'invalid_or_stop',await this.observe());return this.input();}
   return this.finish(result.error==='Cancelled'?'cancelled':result.error==='budget_limit'?'budget_limit':result.error?'provider_error':'provider_stop',await this.observe());
  }
  r.plan=result.actions.slice();return this.input();
 }
 async execute(action){const r=this.case,t=this.tab,s=this.state.visible;
  if(!this.state.choices[action]){r.rejected.push('unavailable:'+action);
   if(r.mode==='hybrid'&&r.provider==='ollama'){this.switch('unavailable_action',s);return this.input();}
   return this.finish('unavailable_action',s);
  }
  const start=Date.now();
  try{
   if(action==='FILL_SEARCH')await t.playwright.getByRole('searchbox',{name:'名前・パス・目的を検索',exact:true}).fill('alpha');
   else if(action==='FILTER_APPS')await t.playwright.getByRole('combobox',{name:'分類',exact:true}).selectOption('アプリ');
   else if(action==='CLEAR_CATEGORY')await t.playwright.getByRole('combobox',{name:'分類',exact:true}).selectOption('');
   else if(action==='OPEN_DETAILS')await t.playwright.getByRole('button',{name:'alpha21',exact:true}).click();
   else if(action==='EXPAND_METADATA')await t.playwright.getByText('確認したメタ情報',{exact:false}).click();
   else if(action==='CLOSE_DETAILS')await t.playwright.getByRole('button',{name:'閉じる',exact:true}).click();
   else if(action==='NAV_BOARD')await t.playwright.getByRole('link',{name:/^(← )?作業ボード$/}).first().click();
   else return this.finish('invalid_action',s);
  }catch(e){r.execution_error=String(e).slice(0,150);return this.finish('execution_error',await this.observe());}
  r.steps.push({action,before:s,execution_ms:Date.now()-start});
  if(action==='NAV_BOARD')await t.playwright.getByRole('heading',{name:'作業ボード',exact:false}).waitFor({state:'visible'});
  return this.input();
 }
 async finish(status,s){Object.assign(this.case,{status,final:s,elapsed_ms:Date.now()-this.case.started});await this.save();return {status,mode:this.case.mode,goal:this.case.goal,repetition:this.case.repetition};}
 async save(){await fs.writeFile(this.root+'/cases.json',JSON.stringify(this.rows,null,2));}
}
