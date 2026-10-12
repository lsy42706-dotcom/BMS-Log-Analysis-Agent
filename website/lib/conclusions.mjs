/** Trusted event facts and application-authored conclusions. No model prose is verified. */
export const FACT_FIELDS = Object.freeze([
  "event_id", "rule_id", "field", "channel_id", "start_s", "confirmed_s",
  "recovered_s", "peak_value", "status",
]);
const NUMERIC_FACT_FIELDS = new Set(["start_s", "confirmed_s", "recovered_s", "peak_value"]);
const isRecord = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

export function canonicalEventFact(event) {
  return {
    event_id: event.evidence_id, rule_id: event.rule_id, field: event.field,
    channel_id: event.channel_id ?? null, start_s: event.start_s,
    confirmed_s: event.confirmed_s, recovered_s: event.recovered_s ?? null,
    peak_value: event.peak_value ?? null, status: event.status,
  };
}

export function eventAggregates(events) {
  const groups = new Map();
  for (const event of events) {
    const key = JSON.stringify([event.code, event.rule_id, event.field, event.channel_id ?? null, event.status]);
    if (!groups.has(key)) groups.set(key, {
      code: event.code, rule_id: event.rule_id, field: event.field,
      channel_id: event.channel_id ?? null, status: event.status,
      count: 0, first_start_s: event.start_s, last_confirmed_s: event.confirmed_s,
      last_recovered_s: null, peak_min: null, peak_max: null, peak_abs_max: null,
      representative_evidence_ids: [],
    });
    const group = groups.get(key);
    group.count += 1;
    group.first_start_s = Math.min(group.first_start_s, event.start_s);
    group.last_confirmed_s = Math.max(group.last_confirmed_s, event.confirmed_s);
    if (event.recovered_s != null) group.last_recovered_s = group.last_recovered_s == null
      ? event.recovered_s : Math.max(group.last_recovered_s, event.recovered_s);
    if (event.peak_value != null) {
      group.peak_min = group.peak_min == null ? event.peak_value : Math.min(group.peak_min, event.peak_value);
      group.peak_max = group.peak_max == null ? event.peak_value : Math.max(group.peak_max, event.peak_value);
      group.peak_abs_max = group.peak_abs_max == null ? Math.abs(event.peak_value) : Math.max(group.peak_abs_max, Math.abs(event.peak_value));
    }
    if (group.representative_evidence_ids.length < 3) group.representative_evidence_ids.push(event.evidence_id);
  }
  return [...groups.values()].sort((a, b) => {
    for (const key of ["code", "rule_id", "field", "channel_id", "status"]) {
      const left = String(a[key] ?? ""); const right = String(b[key] ?? "");
      if (left !== right) return left < right ? -1 : 1;
    }
    return 0;
  });
}

function exactFact(supplied, trusted) {
  if (!isRecord(supplied) || Object.keys(supplied).length !== FACT_FIELDS.length
      || !FACT_FIELDS.every((key) => Object.hasOwn(supplied, key))) return false;
  return FACT_FIELDS.every((key) => {
    const value = supplied[key];
    if (value !== null && (NUMERIC_FACT_FIELDS.has(key)
      ? typeof value !== "number" || !Number.isFinite(value) : typeof value !== "string")) return false;
    return value === trusted[key];
  });
}

/** Validate copied facts, mandatory tool reads, unique citations, and full coverage. */
export function validateConclusion(conclusion, toolbox) {
  const completed = new Set(toolbox.calls.filter((call) => call.status === "complete").map((call) => call.tool));
  if (!["summarize_log", "list_protection_rules", "detect_faults"].every((name) => completed.has(name))) {
    throw new Error("Agent must inspect the log, rules and detected faults before concluding");
  }
  if (!isRecord(conclusion) || typeof conclusion.summary !== "string" || !Array.isArray(conclusion.findings)) {
    throw new Error("Invalid conclusion structure");
  }
  if (!["full", "partial"].includes(conclusion.coverage)) throw new Error("Conclusion must declare full or partial coverage");
  const cited = new Set();
  for (const finding of conclusion.findings) {
    if (!isRecord(finding)) throw new Error("Invalid finding");
    const ids = finding.evidence_ids; const facts = finding.facts;
    if (!Array.isArray(ids) || ids.length === 0 || !ids.every((id) => typeof id === "string")
        || !Array.isArray(facts) || facts.length !== ids.length) {
      throw new Error("Every citation requires an exact event fact");
    }
    const byId = new Map();
    for (const fact of facts) {
      if (!isRecord(fact) || typeof fact.event_id !== "string" || byId.has(fact.event_id)) throw new Error("Invalid or duplicate event fact");
      byId.set(fact.event_id, fact);
    }
    if (new Set(ids).size !== ids.length || ids.some((id) => !byId.has(id))) throw new Error("Facts must match unique evidence IDs");
    for (const id of ids) {
      if (!toolbox.issuedEvidence.has(id)) throw new Error("Conclusion cites evidence not returned by a log tool");
      if (cited.has(id)) throw new Error("Duplicate evidence citations are not allowed");
      const event = toolbox.issuedEvidence.get(id);
      if (event.code !== finding.code) throw new Error("Finding code does not match its evidence");
      if (!exactFact(byId.get(id), canonicalEventFact(event))) throw new Error("Finding facts do not match trusted event evidence");
      cited.add(id);
    }
  }
  const scope = new Set(toolbox.analysis.events.map((event) => event.evidence_id));
  if (scope.size && cited.size === 0) throw new Error("Detected events cannot all be omitted");
  if ([...cited].some((id) => !scope.has(id))) throw new Error("Evidence is outside the selected log");
  if (conclusion.coverage === "full" && (cited.size !== scope.size || [...scope].some((id) => !cited.has(id) || !toolbox.issuedEvidence.has(id)))) {
    throw new Error("Full coverage requires every in-scope event to be issued and cited");
  }
}

/** Discard all raw model prose. Return only verified, program-rendered facts. */
export function canonicalizeConclusion(conclusion, toolbox) {
  validateConclusion(conclusion, toolbox);
  const events = toolbox.analysis.events;
  const scope = new Set(events.map((event) => event.evidence_id));
  const cited = new Set(conclusion.findings.flatMap((finding) => finding.evidence_ids));
  const issued = [...scope].filter((id) => toolbox.issuedEvidence.has(id)).length;
  const full = cited.size === scope.size && issued === scope.size;
  const findings = conclusion.findings.map((finding) => {
    const trusted = finding.evidence_ids.map((id) => toolbox.issuedEvidence.get(id));
    return {
      code: finding.code, evidence_ids: [...finding.evidence_ids],
      facts: trusted.map(canonicalEventFact),
      explanation: trusted.map((event) => {
        const channel = event.channel_id == null ? "" : `，通道 ${event.channel_id}`;
        const recovery = event.recovered_s == null ? "尚未观测到确认恢复" : `恢复时刻 ${event.recovered_s} s`;
        const peak = event.peak_value == null ? "无有效峰值" : `记录极值 ${event.peak_value}`;
        return `${event.evidence_id}：${event.field}${channel}，规则 ${event.rule_id}；起始 ${event.start_s} s，确认 ${event.confirmed_s} s，${recovery}；${peak}，状态 ${event.status}。`;
      }).join(" "),
    };
  });
  const limitations = [
    "结论仅描述已观测采样与配置规则匹配，不证明物理故障根因，不执行电池控制。",
    "当前阈值和规则须按电池体系与实际设备验证；无事件不代表真实电池安全。",
    "模型生成的自由摘要、解释与限制说明已丢弃；这里的数值和覆盖声明由程序生成。",
  ];
  if (!full) limitations.push(`还有 ${scope.size - cited.size} 个事件未在本次模型结论中逐项引用；全量规则扫描聚合不等于全量模型取证。`);
  return {
    summary: `程序已扫描所选日志的全部采样，检测到 ${events.length} 个规则或数据质量事件；结论已核验引用 ${cited.size} 个事件，证据覆盖为${full ? "完整" : "部分"}。`,
    findings, limitations,
    coverage: { mode: full ? "full" : "partial", scope: "entire_selected_log", source_sha256: toolbox.dataset.sha256,
      deterministic_scan_complete: true, total_events: scope.size, issued_events: issued,
      cited_events: cited.size, remaining_events: scope.size - cited.size },
    event_aggregates: eventAggregates(events), text_provenance: "application_generated_from_verified_event_facts",
  };
}
