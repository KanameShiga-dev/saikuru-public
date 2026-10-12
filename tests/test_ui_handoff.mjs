import test from 'node:test';
import assert from 'node:assert/strict';
import {stalledCount,nextProvider,UiBenchmark} from '../ui_decision_benchmark.mjs';
test('two unchanged operations switch to model',()=>{
  const s={query:'',page:'1',rows:[]};
  let n=stalledCount(s,s,0);assert.equal(nextProvider('hybrid','ollama',n),'ollama');
  n=stalledCount(s,s,n);assert.equal(nextProvider('hybrid','ollama',n),'model');
});
test('visible progress resets the counter',()=>assert.equal(stalledCount({query:''},{query:'alpha'},1),0));
test('other arms preserve original behavior',()=>{
  assert.equal(nextProvider('ollama','ollama',8),'ollama');
  assert.equal(nextProvider('model','model',8),'model');
});
test('no return from model to ollama',()=>assert.equal(nextProvider('hybrid','model',8),'model'));
test('handoff records source, reason and current state',()=>{
  const h=new UiBenchmark(null,'.');h.case={arm:'hybrid',activeProvider:'ollama',steps:[],switches:[]};
  h.switchToModel('provider_error',{query:'alpha'});
  assert.equal(h.case.activeProvider,'model');assert.equal(h.case.switches[0].state.query,'alpha');
  h.switchToModel('again',{});assert.equal(h.case.switches.length,1);
});
test('STOP recommendation switches without executing a browser action',async()=>{
  const h=new UiBenchmark(null,'.');h.case={arm:'hybrid',activeProvider:'ollama',steps:[],switches:[],decisionFailures:[]};
  h.last={choices:{STOP:'stop'}};h.observe=async()=>({query:''});h.input=async()=>({provider:h.case.activeProvider});
  assert.equal((await h.execute({action:'STOP',arm:'ollama'})).provider,'model');
  assert.equal(h.case.decisionFailures.length,1);assert.equal(h.case.steps.length,0);
});
test('invalid candidate is never executed',async()=>{
  const h=new UiBenchmark(null,'.');h.case={arm:'hybrid',activeProvider:'ollama',steps:[],switches:[],decisionFailures:[]};
  h.last={choices:{STOP:'stop'}};h.observe=async()=>({});h.input=async()=>({provider:h.case.activeProvider});
  assert.equal((await h.execute({action:'RUN_ARBITRARY_CODE',arm:'ollama'})).provider,'model');
  assert.equal(h.case.switches[0].reason,'invalid_action');assert.equal(h.case.steps.length,0);
});
