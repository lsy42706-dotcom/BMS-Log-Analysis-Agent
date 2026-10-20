import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const html = fs.readFileSync(path.join(root, "public/index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "public/assets/app.js"), "utf8");

class Element {
  constructor(tag, owner) { this.tag = tag; this.owner = owner; this.children = []; this.listeners = {}; this.value = ""; this.hidden = false; this.textContent = ""; }
  set id(value) { this._id = value; this.owner.set(value, this); }
  get id() { return this._id; }
  get options() { return this.children; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { this[name] = value; }
  getBoundingClientRect() { return { width: 600 }; }
  getContext() { return new Proxy({}, { get: () => () => {}, set: () => true }); }
  click() {}
  remove() {}
}

function browser(fetch = async () => ({ ok: true, json: async () => ({}) }), localFixture = null) {
  const elements = new Map();
  const document = {
    getElementById(id) { if (!elements.has(id)) { const el = new Element("div", elements); el.id = id; } return elements.get(id); },
    createElement(tag) { return new Element(tag, elements); },
    body: new Element("body", elements),
  };
  const sandbox = { document, fetch, TextEncoder, TextDecoder, Blob, URL, setTimeout, AbortController, localFixture, window: { devicePixelRatio: 1, addEventListener() {} } };
  vm.createContext(sandbox);
  // Expose internal helpers only in this in-memory test copy; the public script has no test API.
  vm.runInContext(script.replace("  buildMapping([]);", "  globalThis.fixture = {readHeaders, format, channels, request, setUpload, state};\n  buildMapping([]);").replace(/  const loadLocal = .*\n/, '  const loadLocal = async () => globalThis.localFixture;\n'), sandbox);
  for (const [id, value] of Object.entries({ mode: "offline", question: "检查保护事件", "current-convention": "positive_discharge", "max-turns": "8", timeout: "60", "max-output": "1200", "max-total": "24000" })) document.getElementById(id).value = value;
  return { fixture: sandbox.fixture, document, elements };
}

function standardUpload(env) {
  env.fixture.setUpload("timestamp_s,cell_voltage_v,current_a,temperature_c\n0,3.7,10,25\n", "example.csv");
}

test("public UI declares server-bound logs and BYOK billing", () => {
  assert.match(html, /公开工作台 · 默认免费 Qwen3-1.7B/);
  assert.match(html, /value="browser" selected/);
  assert.match(html, /本地推理/);
  assert.match(html, /不勾选时仅显示本地规则结果/);
  assert.match(html, /通过 HTTPS 将日志发送至本站服务/);
  assert.match(html, /不表示日志仅留在浏览器/);
  assert.match(html, /你的 API 账户计费/);
  assert.match(html, /必要日志证据发送至 OpenAI/);
  assert.doesNotMatch(html + script, /localhost|127\.0\.0\.1|本机运行|本地服务/);
});

test("hosted limits are visible and compatible with the backend", () => {
  assert.match(html, /512 KiB \/ 3,000 行 \/ 16 通道/);
  assert.match(html, /20 条配置规则、5,000 个事件/);
  assert.match(html, /本地完整版本仍支持 5 MiB \/ 20,000 行/);
  assert.match(script, /MAX_BYTES = 512 \* 1024/);
  for (const [id, maximum, value] of [["max-turns", 12, 8], ["timeout", 60, 60], ["max-output", 2000, 1200], ["max-total", 48000, 24000]]) {
    assert.match(html, new RegExp(`id="${id}"[^>]*max="${maximum}" value="${value}"`));
  }
});

test("public UI uses same-site assets, no browser persistence or unsafe HTML sinks", () => {
  assert.match(html, /href="\/assets\/style\.css"/);
  assert.match(html, /src="\/assets\/app\.js"/);
  assert.doesNotMatch(html, /<script[^>]+src="https?:/);
  assert.match(html, /http-equiv="Content-Security-Policy"/);
  assert.doesNotMatch(script, /localStorage|sessionStorage|indexedDB|document\.cookie|innerHTML|insertAdjacentHTML/);
  assert.doesNotMatch(script, /X-BMS-Local/);
  assert.match(script, /"X-BMS-Request": "1"/);
});

test("CSV header reader preserves quoted names and rejects duplicates, blank and broken headers", () => {
  const { fixture } = browser();
  assert.deepEqual(Array.from(fixture.readHeaders('"time,s","a""b",voltage\n0,1,2\n')), ["time,s", 'a"b', "voltage"]);
  for (const text of ["", "a,a\n", "a, \n", '"unclosed,a']) assert.throws(() => fixture.readHeaders(text), /CSV/);
});

test("formatting makes missing and non-finite values explicit", () => {
  const { fixture } = browser();
  assert.equal(fixture.format(null), "—");
  assert.equal(fixture.format(undefined), "—");
  assert.equal(fixture.format(2.345678), "2.3457");
  assert.equal(fixture.format(0.00002), "2.000e-5");
  assert.equal(fixture.format(Infinity), "Infinity");
});

test("multichannel waveforms retain canonical channel names and refuse incomplete spreads", () => {
  const { fixture } = browser();
  fixture.state.result = { report: { analysis: { summary: { channels: {} } } } };
  fixture.state.waveform = { samples: [{ current_a: 10, cell_voltage_v: null, temperature_c: null, cell_voltages_v: { C01: 3.7, C02: 3.9 }, temperatures_c: { T01: 20, T02: null } }] };
  const channels = fixture.channels();
  assert.deepEqual(Array.from(channels, (c) => c.id), ["current_a", "cell_voltage_v_C01", "cell_voltage_v_C02", "temperature_c_T01", "temperature_c_T02", "cell_spread_v", "temperature_spread_c"]);
  assert.ok(Math.abs(channels.find((c) => c.id === "cell_spread_v").value(fixture.state.waveform.samples[0]) - 0.2) < 1e-12);
  assert.equal(channels.find((c) => c.id === "temperature_spread_c").value(fixture.state.waveform.samples[0]), null);
});

test("requests use same-site POST, omit cookies, and carry the explicit request header", async () => {
  let call;
  const { fixture } = browser(async (url, options) => { call = { url, options }; return { ok: true, json: async () => ({ accepted: true }) }; });
  const result = await fixture.request("/api/window", { start_s: 0, end_s: 1 });
  assert.equal(result.accepted, true);
  assert.equal(call.url, "/api/window");
  assert.equal(call.options.method, "POST");
  assert.equal(call.options.credentials, "omit");
  assert.equal(call.options.cache, "no-store");
  assert.equal(call.options.headers["X-BMS-Request"], "1");
  assert.deepEqual(JSON.parse(call.options.body), { start_s: 0, end_s: 1 });
});

test("invalid service responses yield concise public errors", async () => {
  const env = browser(async () => ({ ok: false, json: async () => { throw new Error("parse failed"); } }));
  await assert.rejects(env.fixture.request("/api/analyze", {}), /服务返回无效响应/);
  const denied = browser(async () => ({ ok: false, json: async () => ({ error: { message: "请求不符合上限" } }) }));
  await assert.rejects(denied.fixture.request("/api/analyze", {}), /请求不符合上限/);
});

test("BYOK is sent once then erased; window snapshots contain no credentials", async () => {
  const calls = [];
  const env = browser(async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ report: { mode: "agent", status: "complete", analysis: { summary: { sample_count: 1, fault_event_count: 0, quality_event_count: 0, duration_s: 0, start_s: 0, end_s: 0 }, events: [] }, conclusion: { summary: "完成", limitations: [] }, usage: {} }, waveform: { samples: [] }, report_markdown: "# Report" }) };
  });
  standardUpload(env);
  env.document.getElementById("mode").value = "agent";
  env.document.getElementById("api-key").value = "test-request-credential";
  env.document.getElementById("model").value = "test-model";
  await env.document.getElementById("analysis-form").listeners.submit({ preventDefault() {} });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.api_key, "test-request-credential");
  assert.equal(env.document.getElementById("api-key").value, "");
  assert.equal(Object.hasOwn(env.fixture.state.input, "api_key"), false);
  assert.equal(Object.hasOwn(env.fixture.state.input, "model"), false);
  assert.equal(JSON.stringify(env.fixture.state).includes("test-request-credential"), false);
});

test("failed BYOK requests clear credentials and leave no retained secret", async () => {
  const env = browser(async () => { throw new Error("网络请求未完成"); });
  standardUpload(env);
  env.document.getElementById("mode").value = "agent";
  env.document.getElementById("api-key").value = "test-request-credential";
  env.document.getElementById("model").value = "test-model";
  await env.document.getElementById("analysis-form").listeners.submit({ preventDefault() {} });
  assert.equal(env.document.getElementById("api-key").value, "");
  assert.equal(JSON.stringify(env.fixture.state).includes("test-request-credential"), false);
  assert.equal(env.fixture.state.busy, false);
  assert.match(env.document.getElementById("error-message").textContent, /网络请求/);
});

test("demo data stays synthetic and file size rejection happens before reading", async () => {
  const env = browser();
  env.document.getElementById("load-demo").listeners.click();
  assert.equal(env.fixture.state.csv.trim().split("\n").length, 32);
  assert.equal(env.fixture.state.source, "browser-demo.csv");
  let read = false;
  env.document.getElementById("csv-file").files = [{ size: 512 * 1024 + 1, arrayBuffer: async () => { read = true; return new ArrayBuffer(0); } }];
  await env.document.getElementById("csv-file").listeners.change();
  assert.equal(read, false);
  assert.equal(env.fixture.state.csv, "");
  assert.match(env.document.getElementById("error-message").textContent, /512 KiB/);
});

function freeResult(fallback=false) {
  return {report:{mode:'browser',status:fallback?'fallback':'complete',analysis:{summary:{sample_count:1,
    fault_event_count:0,quality_event_count:0,duration_s:0,start_s:0,end_s:0},events:[]},
    conclusion:{summary:'本地结果',limitations:[]},usage:{requests:0,local_inference:{input_tokens:10,output_tokens:2}},
    local_ai:{model_used:!fallback,reason:fallback?'尚未允许首次模型下载。':null}},waveform:{samples:[]},report_markdown:'# Local report'};
}
test('default free submission never posts CSV or stale credentials to the hosted API',async()=>{
  let net=0,received;
  const local={analyzeFree:async(payload,options)=>{received={payload,options};return freeResult();}};
  const env=browser(async()=>{net++;throw new Error('No remote requests');},local);
  standardUpload(env);env.document.getElementById('mode').value='browser';
  env.document.getElementById('api-key').value='TEST_ONLY_STALE_KEY';
  env.document.getElementById('model-consent').checked=true;
  await env.document.getElementById('analysis-form').listeners.submit({preventDefault(){}});
  assert.equal(net,0);assert.equal(received.options.consent,true);
  assert.equal(Object.hasOwn(received.payload,'api_key'),false);assert.equal(Object.hasOwn(received.payload,'mode'),false);
  assert.equal(env.document.getElementById('api-key').value,'');
  assert.ok(!JSON.stringify(env.fixture.state).includes('TEST_ONLY_STALE_KEY'));
  assert.match(env.document.getElementById('usage-note').textContent,/付费 API 调用数 0/);
});
test('free fallback is labelled honestly and evidence windows stay local',async()=>{
  let net=0,windowCalls=0;
  const local={analyzeFree:async()=>freeResult(true),inspectLocally:async()=>{windowCalls++;return {samples:[]};}};
  const env=browser(async()=>{net++;throw new Error('No remote requests');},local);
  standardUpload(env);env.document.getElementById('mode').value='browser';
  await env.document.getElementById('analysis-form').listeners.submit({preventDefault(){}});
  assert.match(env.document.getElementById('run-status').textContent,/免费 AI 未完成/);
  assert.match(env.document.getElementById('usage-note').textContent,/本地规则/);
  env.document.getElementById('window-start').value='0';env.document.getElementById('window-end').value='1';
  await env.document.getElementById('inspect-window').listeners.click();
  // The click listener dispatches the async helper without awaiting it.
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(net,0);assert.equal(windowCalls,1);
});
test('switching from paid to free removes the key and hides paid settings',()=>{
  const env=browser();env.document.getElementById('mode').value='browser';
  env.document.getElementById('api-key').value='TEST_ONLY_STALE_KEY';
  env.document.getElementById('mode').listeners.change();
  assert.equal(env.document.getElementById('api-key').value,'');
  assert.equal(env.document.getElementById('api-key').required,false);
  assert.equal(env.document.getElementById('agent-settings').hidden,true);
  assert.equal(env.document.getElementById('free-settings').hidden,false);
});
