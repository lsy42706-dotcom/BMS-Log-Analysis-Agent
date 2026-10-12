import assert from "node:assert/strict";
import test from "node:test";
import { canonicalEventFact, canonicalizeConclusion, eventAggregates, validateConclusion } from "../lib/conclusions.mjs";

const event = (id, overrides = {}) => ({ evidence_id: id, code: "OVERCURRENT", rule_id: "overcurrent", field: "current_a",
  channel_id: null, start_s: 1, confirmed_s: 3, recovered_s: 7, peak_value: 55, status: "recovered", ...overrides });
function box(events, issued = events) {
  return { dataset: { sha256: "a".repeat(64) }, analysis: { events },
    calls: ["summarize_log", "list_protection_rules", "detect_faults"].map((tool) => ({ tool, status: "complete" })),
    issuedEvidence: new Map(issued.map((item) => [item.evidence_id, item])) };
}
function proposal(events, coverage = "full") {
  return { summary: "UNTRUSTED_MODEL_SUMMARY_99999", coverage, limitations: ["UNTRUSTED_MODEL_LIMITATION_88888"],
    findings: events.map((item) => ({ code: item.code, evidence_ids: [item.evidence_id], facts: [canonicalEventFact(item)], explanation: "UNTRUSTED_MODEL_EXPLANATION_77777" })) };
}

test("exact facts are copied and all free model prose is discarded", () => {
  const e = event("E001"); const result = canonicalizeConclusion(proposal([e]), box([e]));
  assert.deepEqual(result.findings[0].facts[0], canonicalEventFact(e));
  assert.equal(result.coverage.mode, "full"); assert.equal(result.coverage.cited_events, 1);
  assert.match(result.findings[0].explanation, /55/);
  assert.doesNotMatch(JSON.stringify(result), /UNTRUSTED|99999|88888|77777/);
});
test("legal citation with a wrong number is rejected", () => {
  const e = event("E001"); const p = proposal([e]); p.findings[0].facts[0].peak_value = 550;
  assert.throws(() => validateConclusion(p, box([e])), /facts/);
});
test("typed facts reject nonfinite, boolean, missing, additional and null numbers", () => {
  const e = event("E001");
  for (const value of [NaN, Infinity, true, "55", null]) {
    const p = proposal([e]); p.findings[0].facts[0].peak_value = value;
    assert.throws(() => validateConclusion(p, box([e])), /facts/);
  }
  const p = proposal([e]); delete p.findings[0].facts[0].channel_id;
  assert.throws(() => validateConclusion(p, box([e])), /facts/);
  const q = proposal([e]); q.findings[0].facts[0].inferred = 1;
  assert.throws(() => validateConclusion(q, box([e])), /facts/);
});
test("duplicate and unissued citations and wrong codes are rejected", () => {
  const e = event("E001"); const b = box([e]);
  const duplicate = proposal([e, e]); assert.throws(() => validateConclusion(duplicate, b), /Duplicate/);
  assert.throws(() => validateConclusion(proposal([e]), box([e], [])), /not returned/);
  const wrong = proposal([e]); wrong.findings[0].code = "UNDERVOLTAGE";
  assert.throws(() => validateConclusion(wrong, b), /code/);
});
test("full coverage cannot be claimed for a first page or subset", () => {
  const events = [event("E001"), event("E002")]; const b = box(events, [events[0]]);
  assert.throws(() => validateConclusion(proposal([events[0]]), b), /Full coverage/);
  const result = canonicalizeConclusion(proposal([events[0]], "partial"), b);
  assert.deepEqual({ total: result.coverage.total_events, issued: result.coverage.issued_events,
    cited: result.coverage.cited_events, remaining: result.coverage.remaining_events }, { total: 2, issued: 1, cited: 1, remaining: 1 });
  assert.equal(result.coverage.deterministic_scan_complete, true);
});
test("actual complete coverage is computed, not copied from model declaration", () => {
  const events = [event("E001")]; assert.equal(canonicalizeConclusion(proposal(events, "partial"), box(events)).coverage.mode, "full");
  assert.equal(canonicalizeConclusion(proposal([]), box([])).coverage.mode, "full");
});
test("three successful evidence tools remain mandatory", () => {
  const e = event("E001"); const b = box([e]); b.calls[2].status = "failed";
  assert.throws(() => validateConclusion(proposal([e]), b), /inspect/);
});
test("zero-event conclusions cannot fabricate an event", () => {
  const e = event("SIM001"); const b = box([], [e]);
  assert.throws(() => validateConclusion(proposal([e], "partial"), b), /outside/);
});
test("aggregates scan every event but only expose three representatives", () => {
  const events = Array.from({ length: 5 }, (_, i) => event(`E00${i + 1}`, { start_s: i, confirmed_s: i + 2, peak_value: 55 + i }));
  const [aggregate] = eventAggregates(events);
  assert.equal(aggregate.count, 5); assert.equal(aggregate.peak_max, 59);
  assert.equal(aggregate.first_start_s, 0); assert.equal(aggregate.representative_evidence_ids.length, 3);
  assert.equal(eventAggregates([...events, event("E006", { channel_id: "C01" })]).length, 2);
});
