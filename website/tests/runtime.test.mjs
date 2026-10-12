import test from 'node:test';
import assert from 'node:assert/strict';
import {Usage,OpenAIResponsesModel} from '@openai/agents';
import {runAgent,BudgetModel,TokenBudgetError,failureDetails} from '../lib/runtime.mjs';
import {loadCsvBytes,defaultRules} from '../lib/domain.mjs';
import {Toolbox} from '../lib/toolbox.mjs';
import {canonicalEventFact} from '../lib/conclusions.mjs';
const CSV='timestamp_s,cell_voltage_v,current_a,temperature_c\n0,3.7,55,25\n1,3.7,55,25\n2,3.7,55,25\n3,3.7,10,25\n4,3.7,10,25\n5,3.7,10,25\n';
async function box(){return new Toolbox(await loadCsvBytes(CSV),defaultRules());}
class ScriptedModel{
  constructor(proposal,failAt=null){this.proposal=proposal;this.seen=[];this.failAt=failAt;}
  async getResponse(request){const turn=this.seen.length;this.seen.push(request);
    if(turn===this.failAt)throw Object.assign(new Error('PRIVATE_PROVIDER_TEXT'),{name:'APITimeoutError'});
    const names=['summarize_log','list_protection_rules','detect_faults'];
    const output=turn<3?[{type:'function_call',name:names[turn],callId:'call-'+turn,arguments:JSON.stringify(turn===2?{code:'all',offset:0,limit:20}:{})}]
      :[{type:'message',role:'assistant',status:'completed',content:[{type:'output_text',text:JSON.stringify(this.proposal)}]}];
    return {output,usage:new Usage({requests:1,inputTokens:10,outputTokens:5,totalTokens:15})};
  }
  async *getStreamedResponse(){throw new Error('Never stream');}
}
function proposal(b){const e=b.analysis.events[0];return {summary:'UNVERIFIED 999A',findings:[{code:e.code,evidence_ids:[e.evidence_id],facts:[canonicalEventFact(e)],explanation:'UNVERIFIED 999A'}],limitations:[],coverage:'full'};}
test('actual JS SDK Runner executes tools and emits exact canonical facts',async()=>{const b=await box();const fake=new ScriptedModel(proposal(b));const original=globalThis.fetch;globalThis.fetch=()=>{throw new Error('No network permitted');};try{const result=await runAgent(b,'Check faults','','',{}, {modelOverride:fake});assert.equal(result.usage.total_tokens,60);assert.equal(result.usage.simulated,true);assert.equal(result.conclusion.coverage.mode,'full');assert.ok(!result.conclusion.summary.includes('999'));assert.equal(b.calls.length,3);assert.ok(fake.seen[1].input.some(item=>item.type==='function_call_result'));assert.ok(fake.seen.every(item=>item.tracing===false&&item.modelSettings.store===false));}finally{globalThis.fetch=original;}});
test('wrong numerical fact rejected with returned failure usage retained',async()=>{const b=await box();const p=proposal(b);p.findings[0].facts[0].peak_value=999;await assert.rejects(runAgent(b,'Check faults','','',{}, {modelOverride:new ScriptedModel(p)}),error=>error.bmsCode==='invalid_conclusion'&&error.bmsUsage.total_tokens===60);});
test('preflight budget stops before a model request',async()=>{const b=await box();const fake=new ScriptedModel(proposal(b));await assert.rejects(runAgent(b,'Check faults','','',{max_total_tokens:2000},{modelOverride:fake}),error=>error instanceof TokenBudgetError&&error.bmsUsage.requests===0);assert.equal(fake.seen.length,0);});
test('partial known usage and outstanding request survive provider failure',async()=>{const b=await box();await assert.rejects(runAgent(b,'Check faults','','',{}, {modelOverride:new ScriptedModel(proposal(b),1)}),error=>{const details=failureDetails(error);assert.equal(details.error_code,'timeout');assert.equal(details.usage.total_tokens,15);assert.equal(details.usage.unresolved_requests,1);assert.equal(details.usage.complete,false);assert.ok(!JSON.stringify(details).includes('PRIVATE_PROVIDER_TEXT'));return true;});});
test('source name and question credentials are removed at model boundary',async()=>{const b=await box();const key='TEST_ONLY_REQUEST_KEY';b.dataset.source_name=key+'.csv';const fake=new ScriptedModel(proposal(b));await runAgent(b,'Check '+key,'test-model',key,{}, {modelOverride:fake});assert.ok(!JSON.stringify(fake.seen).includes(key));});
test('no key/model or invalid controls are rejected without fetch',async()=>{const b=await box();await assert.rejects(runAgent(b,'Check','',''));await assert.rejects(runAgent(b,'Check','sk-test-credential','TEST_ONLY'));await assert.rejects(runAgent(b,'Check','test-model','TEST_ONLY',{timeout_s:61}));});
test('actual SDK terminal incomplete responses preserve returned billed usage',async()=>{const b=await box();const fakeClient={baseURL:'https://api.openai.com/v1',responses:{create:async()=>({id:'resp_test',status:'incomplete',output:[],usage:{input_tokens:100,output_tokens:20,total_tokens:120}})}};const model=new OpenAIResponsesModel(fakeClient,'test-model');await assert.rejects(runAgent(b,'Check faults','','',{}, {modelOverride:model}),error=>{assert.equal(error.bmsUsage.requests,1);assert.equal(error.bmsUsage.total_tokens,120);assert.equal(error.bmsUsage.complete,true);assert.equal(error.bmsUsage.unresolved_requests,0);return true;});});
test('provider omitted usage is unknown and stops subsequent model turns',async()=>{const b=await box();let calls=0;const fakeClient={baseURL:'https://api.openai.com/v1',responses:{create:async()=>{calls++;return {id:'resp_test',status:'completed',output:[]};}}};const model=new OpenAIResponsesModel(fakeClient,'test-model');await assert.rejects(runAgent(b,'Check faults','','',{}, {modelOverride:model}),error=>{assert.equal(error.bmsCode,'usage_unknown');assert.equal(error.bmsUsage.total_tokens,null);assert.equal(error.bmsUsage.known_returned_usage.total_tokens,0);assert.equal(error.bmsUsage.complete,false);assert.equal(error.bmsUsage.unknown_usage_requests,1);return true;});assert.equal(calls,1);});
