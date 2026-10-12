import assert from "node:assert/strict";
import test from "node:test";
import { analyzeRequest, inspectRequest, PUBLIC_LIMITS, redact } from "../lib/service.mjs";
import { canonicalEventFact } from "../lib/conclusions.mjs";
import { defaultRules } from "../lib/domain.mjs";

const CSV = "timestamp_s,cell_voltage_v,current_a,temperature_c\n0,3.7,55,25\n1,3.7,55,25\n2,3.7,55,25\n3,3.7,10,25\n4,3.7,10,25\n5,3.7,10,25\n";
const zeroUsage = { requests: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, complete: true, unresolved_requests: 0, simulated: true };
function fakeAgent(toolbox) {
  for (const name of ["summarize_log", "list_protection_rules", "detect_faults"]) toolbox.invoke(name);
  const events = [...toolbox.issuedEvidence.values()];
  return { conclusion: { summary: "MODEL_LIES_123456", limitations: ["MODEL_LIES_234567"], coverage: "full",
    findings: events.map((event) => ({ code: event.code, evidence_ids: [event.evidence_id],
      facts: [canonicalEventFact(event)], explanation: "MODEL_LIES_345678" })) }, usage: zeroUsage };
}

test("public offline upload is complete, fact-based, downloadable and free of model calls", async () => {
  const response = await analyzeRequest({ csv_text: CSV });
  assert.equal(response.report.status, "complete"); assert.equal(response.report.cache_hit, false);
  assert.equal(response.report.usage.total_tokens, 0);
  assert.equal(response.report.conclusion.coverage.mode, "full");
  assert.equal(response.report.conclusion.coverage.consumer, "deterministic_offline_application_not_model");
  assert.equal(response.report.conclusion.findings[0].facts[0].peak_value, 55);
  assert.equal(response.report.conclusion.findings[0].facts[0].confirmed_s, 2);
  assert.equal(response.report.conclusion.findings[0].facts[0].recovered_s, 5);
  assert.match(response.report_markdown, /证据覆盖/); assert.equal(response.waveform.matched_samples, 6);
});
test("agent requires its own explicit key regardless of process environment", async () => {
  const previous = process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY = "TEST_ONLY_FOREIGN_ENV_KEY";
  try {
    await assert.rejects(analyzeRequest({ csv_text: CSV, mode: "agent", model: "test-model" }, { runAgent: fakeAgent }), /own key/);
  } finally {
    if (previous === undefined) delete process.env.OPENAI_API_KEY; else process.env.OPENAI_API_KEY = previous;
  }
});
test("independent concurrent requests pass only their own keys and never return credentials", async () => {
  const keys = ["TEST_ONLY_REQUEST_KEY_A", "TEST_ONLY_REQUEST_KEY_B"];
  const received = [];
  const results = await Promise.all(keys.map((key) => analyzeRequest({ csv_text: CSV, mode: "agent", api_key: key,
    model: "test-model", question: `Analyse ${key}`, source_name: `${key}.csv` }, { runAgent: async (toolbox, question, model, passedKey, limits) => {
      received.push(passedKey); assert.equal(model, "test-model"); assert.equal(limits.timeout_s, 60);
      assert.equal(limits.max_turns, 8); assert.doesNotMatch(question, /TEST_ONLY_REQUEST_KEY/);
      return fakeAgent(toolbox);
    } })));
  assert.deepEqual(received.sort(), [...keys].sort());
  for (const result of results) {
    const serialized = JSON.stringify(result);
    for (const key of keys) assert.equal(serialized.includes(key), false);
    assert.doesNotMatch(serialized, /MODEL_LIES/);
    assert.equal(result.report.conclusion.coverage.consumer, "agent_verified_event_citations");
  }
});
test("already canonical runtime prose is still discarded and numeric facts rechecked", async () => {
  const response = await analyzeRequest({ csv_text: CSV, mode: "agent", api_key: "TEST_ONLY", model: "test-model" },
    { runAgent: async (box) => { const result = fakeAgent(box); result.conclusion.coverage = { mode: "full", total_events: 123456 };
      return result; } });
  assert.equal(response.report.status, "complete"); assert.equal(response.report.conclusion.coverage.total_events, 1);
  assert.doesNotMatch(JSON.stringify(response), /MODEL_LIES|123456/);
  const wrong = await analyzeRequest({ csv_text: CSV, mode: "agent", api_key: "TEST_ONLY", model: "test-model" },
    { runAgent: async (box) => { const result = fakeAgent(box); result.conclusion.findings[0].facts[0].peak_value = 555;
      return result; } });
  assert.equal(wrong.report.status, "failed"); assert.equal(wrong.report.analysis.events[0].peak_value, 55);
  assert.equal(wrong.report.conclusion, undefined);
  assert.equal(wrong.report.error_code, "invalid_conclusion");
  assert.equal(wrong.report.usage.complete, true); assert.equal(wrong.report.usage.unresolved_requests, 0);
});
test("failure returns known usage and explicit unresolved requests without leaking errors", async () => {
  const key = "TEST_ONLY_SECRET_ERROR_KEY";
  const response = await analyzeRequest({ csv_text: CSV, mode: "agent", api_key: key, model: "test-model" },
    { runAgent: async () => { const error = new Error(`provider secret ${key}`); error.bmsCode = "timeout";
      error.bmsUsage = { requests: 2, input_tokens: 100, output_tokens: 50, total_tokens: 150,
        unresolved_requests: 1, complete: false, api_key: key }; throw error; } });
  assert.equal(response.report.status, "failed"); assert.equal(response.report.error_code, "timeout");
  assert.equal(response.report.usage.total_tokens, 150); assert.equal(response.report.usage.unresolved_requests, 1);
  assert.equal(response.report.usage.complete, false); assert.equal(JSON.stringify(response).includes(key), false);
  assert.doesNotMatch(response.report.error, /provider secret/);
});
test("same uploads have stable configuration fingerprints but no cross-request mutable cache", async () => {
  const first = await analyzeRequest({ csv_text: CSV }); const fingerprint = first.report.analysis_fingerprint;
  first.report.analysis.events.length = 0;
  const second = await analyzeRequest({ csv_text: CSV });
  assert.equal(second.report.analysis.events.length, 1); assert.equal(second.report.cache_hit, false);
  assert.equal(second.report.analysis_fingerprint, fingerprint);
  const opposite = await analyzeRequest({ csv_text: CSV, current_convention: "positive_charge" });
  assert.notEqual(opposite.report.analysis_fingerprint, fingerprint);
});
test("mapping and unit changes affect fingerprints even for identical uploaded bytes", async () => {
  const csv = "timestamp_s,current_a,left,right,temperature_c\n0,10,2.8,3.7,25\n1,10,2.8,3.7,25\n2,10,2.8,3.7,25\n";
  const first = await analyzeRequest({ csv_text: csv, column_map: { cell_voltage_v_C01: "left", cell_voltage_v_C02: "right" } });
  const swapped = await analyzeRequest({ csv_text: csv, column_map: { cell_voltage_v_C01: "right", cell_voltage_v_C02: "left" } });
  assert.equal(first.report.source_sha256, swapped.report.source_sha256);
  assert.notEqual(first.report.analysis_fingerprint, swapped.report.analysis_fingerprint);
  const amps = await analyzeRequest({ csv_text: CSV });
  const milli = await analyzeRequest({ csv_text: CSV, units: { current_a: "mA" } });
  assert.equal(amps.report.source_sha256, milli.report.source_sha256);
  assert.notEqual(amps.report.analysis_fingerprint, milli.report.analysis_fingerprint);
});
test("window inspection is independent, deterministic and refuses credentials", async () => {
  const result = await inspectRequest({ csv_text: CSV, start_s: 0, end_s: 5 });
  assert.equal(result.matched_samples, 6);
  await assert.rejects(inspectRequest({ csv_text: CSV, api_key: "TEST_ONLY" }), /credentials/);
});
test("Markdown escapes hostile source names and exports contain no raw key patterns", async () => {
  const response = await analyzeRequest({ csv_text: CSV, source_name: "<img src=x onerror=alert(1)>sk-ABCDEF123456.csv" });
  assert.doesNotMatch(response.report_markdown, /<img|sk-ABCDEF/);
  assert.match(response.report_markdown, /&lt;img/);
  assert.deepEqual(redact({ key: "sk-ABCDEF123456", nested: ["TOKEN_SENTINEL"] }, ["TOKEN_SENTINEL"]),
    { key: "[REDACTED]", nested: ["[REDACTED]"] });
});
test("public size, sample, channel, rule and token budget limits fail closed", async () => {
  assert.equal(PUBLIC_LIMITS.csv_bytes, 512 * 1024);
  await assert.rejects(analyzeRequest({ csv_text: "x".repeat(PUBLIC_LIMITS.csv_bytes + 1) }), /512 KiB/);
  const long = "timestamp_s,cell_voltage_v,current_a,temperature_c\n" + Array.from({ length: 3001 }, (_, i) => `${i},3.7,0,25`).join("\n");
  await assert.rejects(analyzeRequest({ csv_text: long }), /3000/);
  const names = Array.from({ length: 16 }, (_, i) => `cell_voltage_v_C${i + 1}`);
  const wide = `timestamp_s,current_a,${names.join(",")},temperature_c_T01\n0,0,${names.map(() => "3.7").join(",")},25\n`;
  await assert.rejects(analyzeRequest({ csv_text: wide }), /channel limit/);
  const rules = Array.from({ length: 21 }, (_, i) => ({ ...defaultRules()[2], rule_id: `uv_${i}` }));
  await assert.rejects(analyzeRequest({ csv_text: CSV, profile: { rules } }), /20 rules/);
  const cells = Array.from({ length: 14 }, (_, i) => `cell_voltage_v_C${i + 1}`);
  const allowedWide = `timestamp_s,current_a,${cells.join(",")},temperature_c_T01\n0,0,${cells.map(() => "3.7").join(",")},25\n`;
  await assert.rejects(analyzeRequest({ csv_text: allowedWide, profile: { rules: rules.slice(0, 20) } }), /64/);
  for (const [field, value] of Object.entries({ max_turns: 13, timeout_s: 61, max_output_tokens: 2001, max_total_tokens: 48001 })) {
    await assert.rejects(analyzeRequest({ csv_text: CSV, [field]: value }), new RegExp(field));
  }
});
test("public event guard rejects excessive faults during analysis instead of truncating the report", async () => {
  const fields = Array.from({ length: 12 }, (_, i) => `cell_voltage_v_C${i + 1}`);
  const csv = `timestamp_s,current_a,${fields.join(",")},temperature_c_T01\n` + Array.from({ length: 1000 }, (_, i) =>
    `${i},0,${fields.map(() => i % 2 ? "3.7" : "").join(",")},25`).join("\n");
  await assert.rejects(analyzeRequest({ csv_text: csv }), /5000-event/);
});
