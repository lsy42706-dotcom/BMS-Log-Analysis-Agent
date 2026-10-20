import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {analyzeLocally,inspectLocally,localPayload} from '../browser/analysis.mjs';
import {parseEvidenceSelection,FREE_MODEL,MAX_REVIEW_EVENTS} from '../browser/free-agent.mjs';
const CSV='timestamp_s,cell_voltage_v,current_a,temperature_c\n0,3.7,55,25\n1,3.7,55,25\n2,3.7,55,25\n3,3.7,10,25\n4,3.7,10,25\n5,3.7,10,25\n';
function engine(replyTransform=null) {
  const seen=[];let resets=0;
  return {seen,get resets(){return resets;},resetChat:async()=>{resets++;},chat:{completions:{create:async request=>{
    seen.push(request);const data=JSON.parse(request.messages[1].content);
    const reply={choices:[{finish_reason:'stop',message:{content:JSON.stringify({evidence_ids:data.events.map(e=>e.evidence_id)})}}],
      usage:{prompt_tokens:123,completion_tokens:14}};
    return replyTransform?replyTransform(reply):reply;
  }}}};
}
test('real SDK + local engine: no key, no network, numeric facts and zero API fees',async()=>{
  const e=engine();const fetchBefore=globalThis.fetch;let network=0;
  globalThis.fetch=()=>{network++;throw new Error('No network permitted');};
  try {
    const result=await analyzeLocally({csv_text:CSV,question:'核对保护事件'},{getEngine:async()=>e});
    assert.equal(result.report.status,'complete');assert.equal(result.report.mode,'browser');
    assert.equal(result.report.model,FREE_MODEL);assert.equal(result.report.usage.requests,0);
    assert.equal(result.report.usage.total_tokens,0);assert.equal(result.report.usage.local_inference.input_tokens,123);
    assert.equal(result.report.usage.local_inference.output_tokens,14);assert.equal(e.seen.length,1);assert.equal(e.resets,1);
    assert.deepEqual(result.report.tool_calls.map(c=>c.tool),['summarize_log','list_protection_rules','detect_faults']);
    assert.equal(result.report.conclusion.findings[0].facts[0].peak_value,55);
    assert.equal(result.report.conclusion.findings[0].facts[0].confirmed_s,2);
    assert.equal(result.report.conclusion.coverage.consumer,'local_model_selected_ids_application_copied_facts');
    assert.equal(result.report.local_ai.bootstrap_tools,'application_driven');assert.equal(network,0);
    assert.ok(!JSON.stringify(result).includes('browser-local-not-a-key'));
    assert.match(result.report_markdown,/不是 API 账单/);
  } finally {globalThis.fetch=fetchBefore;}
});
test('local request rejects credentials, arbitrary providers and unknown fields before engine access',async()=>{
  for(const extra of [{api_key:'TEST_ONLY_SECRET'},{model:'paid-model'},{base_url:'https://external.invalid'}, {mode:'agent'}]){
    let initialized=false;
    await assert.rejects(analyzeLocally({csv_text:CSV,...extra},{getEngine:async()=>{initialized=true;return engine();}}));
    assert.equal(initialized,false);
  }
  assert.throws(()=>localPayload(null));
});
test('unsupported GPU/consent/download error retains genuinely local rule evidence, never paid fallback',async()=>{
  for(const message of ['webgpu_unavailable','download_consent_required','PRIVATE_DOWNLOAD_DETAILS']) {
    const result=await analyzeLocally({csv_text:CSV},{getEngine:async()=>{throw new Error(message);}});
    assert.equal(result.report.status,'fallback');assert.equal(result.report.usage.requests,0);
    assert.equal(result.report.local_ai.model_used,false);assert.equal(result.report.analysis.events.length,1);
    assert.equal(result.report.conclusion.coverage.consumer,'deterministic_offline_application_not_model');
    assert.ok(!JSON.stringify(result).includes('PRIVATE_DOWNLOAD_DETAILS'));
    assert.match(result.report_markdown,/AI 未完成/);
  }
});
test('model cannot fabricate facts, cite unknown/SIM/duplicate IDs, or inject free prose',async()=>{
  for(const content of ['{}','{"evidence_ids":[],"explanation":"LIES_999"}',
    '{"evidence_ids":["E001"],"evidence_ids":["E001"]}',
    '{"evidence_ids":["SIM-E001"]}','{"evidence_ids":["E001","E001"]}','{"evidence_ids":[]}']) {
    const e=engine(r=>{r.choices[0].message.content=content;return r;});
    const result=await analyzeLocally({csv_text:CSV},{getEngine:async()=>e});
    assert.equal(result.report.status,'fallback');assert.equal(result.report.analysis.events[0].peak_value,55);
    assert.ok(!JSON.stringify(result).includes('LIES_999'));assert.equal(result.report.usage.requests,0);
    assert.equal(result.report.usage.local_inference.input_tokens,123);
  }
});
test('large logs disclose partial model review while retaining full deterministic scan',async()=>{
  const rows=['timestamp_s,cell_voltage_v,current_a,temperature_c'];
  for(let t=0;t<60;t++)rows.push(`${t},3.7,${t%6<3?55:10},25`);
  const e=engine();const result=await analyzeLocally({csv_text:rows.join('\n'),question:'x'.repeat(600)},{getEngine:async()=>e});
  assert.equal(result.report.analysis.events.length,10);
  assert.equal(result.report.conclusion.coverage.total_events,10);
  assert.equal(result.report.conclusion.coverage.cited_events,MAX_REVIEW_EVENTS);
  assert.equal(result.report.conclusion.coverage.mode,'partial');assert.equal(result.report.local_ai.question_chars_used,500);
  assert.equal(JSON.parse(e.seen[0].messages[1].content).question.length,500);
});
test('incomplete model generation and cancellation remain free and explicit',async()=>{
  const incomplete=engine(r=>{r.choices[0].finish_reason='length';return r;});
  const first=await analyzeLocally({csv_text:CSV},{getEngine:async()=>incomplete});
  assert.equal(first.report.status,'fallback');assert.match(first.report.local_ai.reason,/未完整/);
  const controller=new AbortController();controller.abort();let called=false;
  const second=await analyzeLocally({csv_text:CSV},{signal:controller.signal,getEngine:async()=>{called=true;return engine();}});
  assert.equal(called,false);assert.equal(second.report.status,'fallback');assert.match(second.report.local_ai.reason,/取消/);
});
test('zero-event log still invokes the model and yields no invented event',async()=>{
  const e=engine();const result=await analyzeLocally({csv_text:CSV.replaceAll(',55,',',10,')},{getEngine:async()=>e});
  assert.equal(result.report.status,'complete');assert.equal(result.report.conclusion.findings.length,0);
  assert.equal(e.seen.length,1);
});
test('local evidence windows require no model or server and preserve sampled facts',async()=>{
  const result=await inspectLocally({csv_text:CSV,start_s:1,end_s:3,max_samples:10});
  assert.equal(result.samples.length,3);assert.equal(result.samples[0].timestamp_s,1);
  await assert.rejects(inspectLocally({csv_text:CSV,api_key:'NOT_ALLOWED'}));
});
test('selection parser fails closed, including prototype keys and oversized output',()=>{
  const events=[{evidence_id:'E001'}];assert.deepEqual(parseEvidenceSelection('{"evidence_ids":["E001"]}',events),['E001']);
  for(const text of ['null','[]','{"evidence_ids":["E001"],"__proto__":{}}',' '.repeat(4097)])assert.throws(()=>parseEvidenceSelection(text,events));
});
test('real Qwen template empty think prefix accepted, nonempty reasoning and prose rejected',async()=>{
  const events=[{evidence_id:'E001'}];
  assert.deepEqual(parseEvidenceSelection('<think>\n\n</think>\n\n{"evidence_ids":["E001"]}',events),['E001']);
  for(const text of ['<think>unverified private reasoning</think>{"evidence_ids":["E001"]}',
    '```json\n{"evidence_ids":["E001"]}\n```','untrusted prefix {"evidence_ids":["E001"]}'])assert.throws(()=>parseEvidenceSelection(text,events));
  const e=engine(r=>{r.choices[0].message.content='<think>\n\n</think>\n\n'+r.choices[0].message.content;return r;});
  const result=await analyzeLocally({csv_text:CSV},{getEngine:async()=>e});assert.equal(result.report.status,'complete');
  assert.ok(!JSON.stringify(result).includes('<think>'));
});
test('browser source pins the model and worker and excludes paid application loader',()=>{
  const entry=fs.readFileSync(new URL('../browser/entry.mjs',import.meta.url),'utf8');
  const build=fs.readFileSync(new URL('../scripts/build-browser.mjs',import.meta.url),'utf8');
  assert.match(entry,/Qwen3-1\.7B-/);assert.match(entry,/new Worker\('\/assets\/local-built\/engine-worker\.js'/);
  assert.match(entry,/360000/);assert.match(entry,/Promise\.race/);assert.match(build,/paid-disabled\.mjs/);
  const record=JSON.parse(fs.readFileSync(new URL('../public/assets/local-built/build-info.json',import.meta.url),'utf8'));
  assert.equal(record.paid_application_runtime,false);assert.equal(record.model,FREE_MODEL);
});
