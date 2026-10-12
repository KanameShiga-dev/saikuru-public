// Execute only through Browser skill's Node REPL, using its supplied tab handle.
import fs from 'node:fs/promises';
export function stalledCount(before,after,previous){
  return JSON.stringify(before)===JSON.stringify(after)?previous+1:0;
}
export function nextProvider(arm,current,stalls){
  return arm==='hybrid'&&current==='ollama'&&stalls>=2?'model':current;
}
export class UiBenchmark {
  constructor(tab,root){this.tab=tab;this.root=root;this.rows=[];this.case=null;}
  async observe(){
    return this.tab.playwright.evaluate(()=>({
      query:document.querySelector('#query').value,
      status:document.querySelector('#status').value,
      page:document.querySelector('#page-info').textContent,
      message:document.querySelector('#message').textContent,
      nextEnabled:!document.querySelector('#next').disabled,
      rows:[...document.querySelectorAll('#tasks li')].map(e=>({
        title:e.querySelector('strong').textContent,
        label:e.querySelector('select').getAttribute('aria-label'),
        status:e.querySelector('select').value,enabled:!e.querySelector('select').disabled}))
    }));
  }
  async start(arm,name,repetition=1){
    await this.tab.goto('http://127.0.0.1:8792/');
    await this.tab.playwright.getByRole('combobox',{name:'タスク 1 の状態',exact:true}).waitFor({state:'visible'});
    this.case={arm,name,repetition,started:Date.now(),steps:[],status:'running',
      activeProvider:arm==='hybrid'?'ollama':arm,noProgress:0,switches:[],decisionFailures:[]};
    return this.input();
  }
  achieved(s){
    if(this.case.name==='search')return s.query==='alpha'&&s.status==='todo'&&s.rows.map(r=>r.title).join(',')==='alpha21,alpha22,alpha31'&&s.page.includes('全 3 件');
    if(this.case.name==='page')return s.page.startsWith('2 ページ')&&s.rows[0]?.title==='alpha21';
    if(this.case.name==='update')return s.rows.some(r=>r.label==='タスク 21 の状態'&&r.status==='done'&&r.enabled);
    return s.query==='zzznomatch'&&s.rows.length===0&&s.message==='該当するタスクはありません。';
  }
  async input(){
    const s=await this.observe();
    const previous=this.case.steps.at(-1);
    if(previous&&!previous.after){
      previous.after=s;
      this.case.noProgress=stalledCount(previous.before,s,this.case.noProgress);
    }
    if(this.achieved(s))return this.finish('passed',s);
    if(this.case.steps.length>=8)return this.finish('action_limit',s);
    if(nextProvider(this.case.arm,this.case.activeProvider,this.case.noProgress)==='model'&&this.case.activeProvider==='ollama'){
      this.switchToModel('two_consecutive_actions_without_visible_progress',s);
    }
    const target=this.case.name==='empty'?'zzznomatch':'alpha';
    const choices={SEARCH:'Click the visible Search button to apply current search text and status filter.',STOP:'Stop if no safe action can progress toward the goal.'};
    if(s.query!==target)choices.FILL_QUERY=`Fill the visible Search field with ${target}. This does not apply the filter until Search is clicked.`;
    if(s.status!=='todo')choices.FILTER_TODO='Select todo (未着手) in the global status filter. Click Search afterward to apply it.';
    if(s.nextEnabled)choices.NEXT='Click the enabled Next page button.';
    if(s.rows.some(r=>r.label==='タスク 21 の状態'&&r.status!=='done'))choices.UPDATE_21='Set task 21 status to done (完了). Change is saved automatically.';
    const goal={search:'Show only alpha tasks whose status is todo: alpha21, alpha22, alpha31.',page:'Display page 2, with alpha21 as the first visible task.',update:'Find task 21 and change its status to done, confirming the saved screen state.',empty:'Search for zzznomatch and confirm the no-results message.'}[this.case.name];
    const state={goal,visible:s,choices};
    const summary=v=>v?{query:v.query,status:v.status,page:v.page,message:v.message,
      visible_titles:v.rows.map(r=>r.title)}:null;
    if(this.case.switches.length)state.handoff={reason:this.case.switches[0].reason,
      previous_actions:this.case.steps.map(step=>({action:step.action,before:summary(step.before),after:summary(step.after)})),
      history_format:'Summary of visible state; full operation records are retained locally.',
      instruction:'Continue from the current visible state. Do not repeat actions that made no progress. Only choose a supplied safe action.'};
    this.last=state;
    await fs.writeFile(this.root+'/decision-state.json',JSON.stringify(state,null,2));
    return {status:'decision_needed',case:this.case.name,arm:this.case.arm,provider:this.case.activeProvider,state};
  }
  switchToModel(reason,state){
    if(this.case.arm!=='hybrid'||this.case.activeProvider!=='ollama')return;
    this.case.switches.push({from:'ollama',to:'model',reason,after_actions:this.case.steps.length,state});
    this.case.activeProvider='model';
  }
  fixed(){
    const s=this.last.visible,n=this.case.name;
    if(n==='page')return 'NEXT';
    if(n==='update')return this.last.choices.UPDATE_21?'UPDATE_21':'NEXT';
    if(s.query!==(n==='empty'?'zzznomatch':'alpha'))return 'FILL_QUERY';
    if(n==='search'&&s.status!=='todo')return 'FILTER_TODO';
    return 'SEARCH';
  }
  async execute(result){
    const action=result.action, t=this.tab;
    if(!this.last.choices[action]||action==='STOP'){
      if(this.case.arm==='hybrid'&&this.case.activeProvider==='ollama'){
        const s=await this.observe();this.case.decisionFailures.push(result);
        this.switchToModel(result.error?'provider_error':action==='STOP'?'provider_stop':'invalid_action',s);
        return this.input();
      }
      return this.finish(!this.last.choices[action]?'invalid_action':result.error?'provider_error':'provider_stop',await this.observe(),result);
    }
    const started=Date.now();
    if(action==='FILL_QUERY')await t.playwright.getByRole('searchbox',{name:'検索',exact:true}).fill(this.case.name==='empty'?'zzznomatch':'alpha');
    else if(action==='FILTER_TODO')await t.playwright.getByRole('combobox',{name:'状態',exact:true}).selectOption('todo');
    else if(action==='SEARCH')await t.playwright.getByRole('button',{name:'検索',exact:true}).click();
    else if(action==='NEXT')await t.playwright.getByRole('button',{name:'次へ',exact:true}).click();
    else if(action==='UPDATE_21')await t.playwright.getByRole('combobox',{name:'タスク 21 の状態',exact:true}).selectOption('done');
    this.case.steps.push({before:this.last.visible,action,decision:result,execution_ms:Date.now()-started});
    // Read the DOM after every interaction; bounded polling handles async refresh.
    for(let i=0;i<15;i++){
      const s=await this.observe();
      if(this.achieved(s)){this.case.steps.at(-1).after=s;return this.finish('passed',s);}
      if(action==='FILL_QUERY'||action==='FILTER_TODO')break;
      if(action==='NEXT'&&s.page!==this.last.visible.page)break;
      if(action==='SEARCH'&&(s.page.includes('全 3 件')||s.message))break;
    }
    return this.input();
  }
  async finish(status,state,error=null){
    Object.assign(this.case,{status,elapsed_ms:Date.now()-this.case.started,final:state,error});
    const file=`${this.case.arm}-${this.case.name}-${this.case.repetition}.png`;
    await fs.writeFile(this.root+'/'+file,await this.tab.screenshot({fullPage:false}));
    this.case.screenshot=file;this.rows.push(this.case);
    await fs.writeFile(this.root+'/ui-results.json',JSON.stringify(this.rows,null,2));
    if(this.case.name==='update'&&state.rows.some(r=>r.label==='タスク 21 の状態'&&r.status==='done')){
      await this.tab.playwright.getByRole('combobox',{name:'タスク 21 の状態',exact:true}).selectOption('todo');
      let restored=false;
      for(let i=0;i<20;i++){const s=await this.observe();if(s.rows.some(r=>r.label==='タスク 21 の状態'&&r.status==='todo'&&r.enabled)){restored=true;break;}}
      if(!restored)throw new Error('Fixture reset not confirmed');
    }
    return {status,case:this.case.name,arm:this.case.arm,steps:this.case.steps.length};
  }
}
