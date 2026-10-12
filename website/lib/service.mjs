/** Public application boundary. Each request has its own data, evidence and credentials. */
import * as domain from "./domain.mjs";
import { Toolbox } from "./toolbox.mjs";
import { canonicalEventFact, canonicalizeConclusion } from "./conclusions.mjs";

export const ANALYSIS_VERSION = "bms-rules-v2-public-1";
export const PUBLIC_LIMITS = Object.freeze({ csv_bytes: 512 * 1024, samples: 3000, channels: 16,
  profile_rules: 20, rule_evaluations: 64, events: 5000, tool_calls: 20 });
const isRecord = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const DEFAULT_QUESTION = "分析保护事件与数据质量，引用证据。";

export function redact(value, secrets = []) {
  if (typeof value === "string") {
    for (const secret of secrets) if (typeof secret === "string" && secret) value = value.replaceAll(secret, "[REDACTED]");
    return value.replace(/sk-[A-Za-z0-9_-]{8,}/g, "[REDACTED]");
  }
  if (Array.isArray(value)) return value.map((item) => redact(item, secrets));
  if (isRecord(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [redact(key, secrets), redact(item, secrets)]));
  return value;
}

function number(value, name, low, high, integer = false) {
  if (typeof value !== "number" || !Number.isFinite(value) || (integer && !Number.isInteger(value)) || value < low || value > high) {
    throw new Error(`${name} must be ${integer ? "an integer" : "a number"} within ${low}..${high}`);
  }
  return value;
}

function limits(payload) {
  return {
    max_turns: number(payload.max_turns ?? 8, "max_turns", 4, 12, true),
    timeout_s: number(payload.timeout_s ?? 60, "timeout_s", 10, 60),
    max_output_tokens: number(payload.max_output_tokens ?? 1200, "max_output_tokens", 256, 2000, true),
    max_total_tokens: number(payload.max_total_tokens ?? 24000, "max_total_tokens", 2000, 48000, true),
  };
}

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (isRecord(value)) return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
  return value;
}

export async function analysisFingerprint(dataset, rules, quality = null) {
  const value = { version: ANALYSIS_VERSION, source_sha256: dataset.sha256,
    import_metadata: dataset.import_metadata ?? {}, current_convention: dataset.current_convention ?? "positive_discharge",
    pack_id: dataset.pack_id ?? "pack_1", rules, quality };
  const bytes = new TextEncoder().encode(JSON.stringify(stable(value)));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function readingFields(dataset) {
  if (typeof domain.sensorChannels === "function") {
    const channels = domain.sensorChannels(dataset);
    if (Array.isArray(channels)) return channels.map((channel) => typeof channel === "string" ? channel : channel.field);
    if (isRecord(channels)) return Object.keys(channels);
  }
  if (typeof domain.sampleReadings === "function") return Object.keys(domain.sampleReadings(dataset.samples[0]));
  // Compatibility with the legacy single-cell layout, not an inferred channel.
  return ["cell_voltage_v", "current_a", "temperature_c"];
}

async function inputs(payload) {
  if (!isRecord(payload)) throw new Error("Request must be an object");
  if (typeof payload.csv_text !== "string") throw new Error("A UTF-8 CSV upload is required");
  if (new TextEncoder().encode(payload.csv_text).byteLength > PUBLIC_LIMITS.csv_bytes) throw new Error("Public CSV upload limit is 512 KiB");
  const sourceName = payload.source_name ?? "upload.csv";
  if (typeof sourceName !== "string" || sourceName.length > 200) throw new Error("Invalid source name");
  const dataset = await domain.loadCsvBytes(payload.csv_text, sourceName, payload.column_map, payload.units,
    { current_convention: payload.current_convention ?? "positive_discharge", pack_id: payload.pack_id ?? "pack_1" });
  if (dataset.samples.length > PUBLIC_LIMITS.samples) throw new Error("Public sample limit is 3000");
  const fields = readingFields(dataset);
  if (fields.length > PUBLIC_LIMITS.channels) throw new Error("Public sensor channel limit is 16");
  const { rules, quality } = payload.profile == null
    ? { rules: domain.defaultRules(), quality: null } : domain.profileFromDict(payload.profile);
  if (rules.length > PUBLIC_LIMITS.profile_rules) throw new Error("Public profile limit is 20 rules");
  const dispatchCount = rules.reduce((count, rule) => {
    if (["cell_spread_v", "temperature_spread_c"].includes(rule.field)) {
      const kind = rule.field === "cell_spread_v" ? "cell_voltage_v" : "temperature_c";
      return count + (fields.filter((field) => field === kind || field.startsWith(`${kind}_`)).length >= 2 ? 1 : 0);
    }
    if (rule.channel_id != null) return count + (fields.includes(`${rule.field}_${rule.channel_id}`) ? 1 : 0);
    if (["cell_voltage_v", "temperature_c"].includes(rule.field)) {
      return count + fields.filter((field) => field === rule.field || field.startsWith(`${rule.field}_`)).length;
    }
    return count + (fields.includes(rule.field) ? 1 : 0);
  }, 0);
  if (dispatchCount > PUBLIC_LIMITS.rule_evaluations) throw new Error("Public evaluated rule-channel limit is 64");
  return { dataset, rules, quality };
}

async function makeToolbox(dataset, rules, quality) {
  // Deliberately no process-level analysis cache: one user's data cannot be reused by another.
  const box = new Toolbox(dataset, rules, { quality, maxCalls: PUBLIC_LIMITS.tool_calls, maxEvents: PUBLIC_LIMITS.events });
  if (box.analysis.events.length > PUBLIC_LIMITS.events) throw new Error("Public event limit is 5000");
  box.fingerprint = await analysisFingerprint(dataset, rules, quality);
  return box;
}

export function offlineAnalysis(toolbox) {
  for (const name of ["summarize_log", "list_protection_rules", "detect_faults"]) toolbox.invoke(name);
  const events = toolbox.analysis.events;
  // Direct program inspection of all events is not evidence of a model fetching all pages.
  for (const event of events) toolbox.issuedEvidence.set(event.evidence_id, event);
  const proposal = { summary: "", coverage: "full", limitations: [], findings: events.map((event) => ({
    code: event.code, evidence_ids: [event.evidence_id], facts: [canonicalEventFact(event)], explanation: "",
  })) };
  const conclusion = canonicalizeConclusion(proposal, toolbox);
  conclusion.coverage.consumer = "deterministic_offline_application_not_model";
  conclusion.summary = `离线规则分析完成，完整扫描并引用 ${events.length} 个规则或数据质量事件。未调用语言模型。`;
  conclusion.limitations[2] = "这是程序按规则和日志事实生成的报告，不是模型诊断；未进行实物电池安全验证。";
  return { conclusion, usage: { requests: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0,
    simulated: false, complete: true, unresolved_requests: 0 } };
}

function safeFallbackFailure(error) {
  const labels = { token_budget: "软件 Token 预算已用尽。", timeout: "模型请求超时；未完成请求的用量可能未知。",
    invalid_conclusion: "模型结论未通过事实或证据校验。", invalid_api_key: "API Key 无效或无权使用所选模型。",
    rate_limited: "API 请求受到速率限制。", quota_exhausted: "API 额度不足。", cancelled: "模型运行已取消。" };
  const code = Object.hasOwn(labels, error?.bmsCode ?? "") ? error.bmsCode : "agent_failed";
  const usage = { requests: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0,
    complete: false, unresolved_requests: 1, simulated: false };
  if (isRecord(error?.bmsUsage)) {
    for (const key of ["requests", "input_tokens", "output_tokens", "total_tokens", "unresolved_requests"]) {
      if (typeof error.bmsUsage[key] === "number" && Number.isFinite(error.bmsUsage[key]) && error.bmsUsage[key] >= 0) usage[key] = error.bmsUsage[key];
    }
    usage.complete = error.bmsUsage.complete === true;
    usage.simulated = error.bmsUsage.simulated === true;
  }
  return { error_code: code, error: labels[code] ?? "模型分析未完成。下方保留确定性规则结果；模型请求用量可能未知。", usage };
}

export async function analyzeRequest(payload, { runAgent = null } = {}) {
  const { dataset, rules, quality } = await inputs(payload);
  const mode = payload.mode ?? "offline";
  if (!["offline", "agent"].includes(mode)) throw new Error("Unsupported analysis mode");
  const key = payload.api_key ?? ""; const model = payload.model ?? "";
  if (typeof key !== "string" || key.length > 512 || typeof model !== "string" || model.length > 150) throw new Error("Invalid credentials or model selection");
  if (mode === "agent" && (!key.trim() || !model.trim())) throw new Error("Agent mode requires this request's own key and model");
  const rawQuestion = payload.question ?? DEFAULT_QUESTION;
  if (typeof rawQuestion !== "string" || !rawQuestion.trim() || rawQuestion.length > 4000) throw new Error("Question must contain 1..4000 characters");
  const question = redact(rawQuestion, [key]);
  const runLimits = limits(payload);
  dataset.source_name = redact(dataset.source_name, [key]);
  const toolbox = await makeToolbox(dataset, rules, quality);
  const report = { mode, status: "complete", source_name: dataset.source_name,
    source_sha256: dataset.sha256, analysis_fingerprint: toolbox.fingerprint,
    analysis_version: ANALYSIS_VERSION, cache_hit: false, question, analysis: toolbox.analysis,
    deployment: "public_request_isolated", public_limits: PUBLIC_LIMITS,
    billing: "Current request's user-provided key only; no shared or author key",
    privacy: "No application-level persistence of uploaded CSV, API keys, analysis cache, or checkpoints. Agent mode sends evidence to the selected OpenAI API model." };
  let runtime = null;
  try {
    let result;
    if (mode === "offline") result = offlineAnalysis(toolbox);
    else {
      if (!runAgent) { runtime = await import("./runtime.mjs"); runAgent = runtime.runAgent; }
      result = await runAgent(toolbox, question, model.trim(), key.trim(), runLimits);
      const candidate = result.conclusion;
      const raw = { summary: "", limitations: [], findings: candidate?.findings,
        coverage: isRecord(candidate?.coverage) ? candidate.coverage.mode : candidate?.coverage };
      // Recheck SDK results at this boundary and regenerate all prose, even with an injected runtime.
      try {
        result = { ...result, conclusion: canonicalizeConclusion(raw, toolbox) };
      } catch {
        const error = new Error("Model conclusion failed event fact validation");
        error.bmsCode = "invalid_conclusion";
        // The model returned a response: retain known usage even if the final facts are rejected.
        error.bmsUsage = result?.usage;
        throw error;
      }
      result.conclusion.coverage.consumer = "agent_verified_event_citations";
    }
    report.conclusion = result.conclusion;
    report.usage = result.usage;
  } catch (error) {
    report.status = "failed";
    Object.assign(report, runtime?.failureDetails ? runtime.failureDetails(error) : safeFallbackFailure(error));
  }
  report.tool_calls = toolbox.calls;
  const waveform = toolbox._window(dataset.samples[0].timestamp_s, dataset.samples.at(-1).timestamp_s, 50);
  const safe = redact({ report, waveform }, [key, key.trim()]);
  return { ...safe, report_markdown: markdownReport(safe.report) };
}

export async function inspectRequest(payload) {
  if (isRecord(payload) && Object.hasOwn(payload, "api_key")) throw new Error("Window inspection must not contain credentials");
  const { dataset, rules, quality } = await inputs(payload);
  const toolbox = await makeToolbox(dataset, rules, quality);
  return redact(toolbox._window(payload.start_s ?? dataset.samples[0].timestamp_s,
    payload.end_s ?? dataset.samples.at(-1).timestamp_s, payload.max_samples ?? 50));
}

function cell(value) {
  if (value == null) return "—";
  // Reports are downloadable plain Markdown; escaping also prevents raw HTML when opened elsewhere.
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll("\r", " ").replaceAll("\n", " ").replace(/[\\`*_\[\]|]/g, "\\$&");
}

export function markdownReport(payload) {
  const analysis = payload.analysis; const summary = analysis.summary; const conclusion = payload.conclusion ?? {};
  const lines = ["# BMS 日志分析报告", "", `- 模式：${cell(payload.mode)}；状态：${cell(payload.status)}`,
    `- 来源：${cell(payload.source_name)}`, `- 输入 SHA-256：${cell(payload.source_sha256)}`,
    `- 样本数：${summary.sample_count}；时间：${summary.start_s}–${summary.end_s} s`,
    `- API 已知调用数：${payload.usage?.requests ?? "未知"}`, "",
    "阈值为演示配置。报告描述观测和规则匹配，不证明物理故障根因，也不是实物电池安全验证。", "", "## 分析结论", "",
    cell(conclusion.summary ?? "模型分析未完成；下方保留确定性规则检查结果。"), ""];
  for (const finding of conclusion.findings ?? []) lines.push(`- ${cell(finding.code)} [${finding.evidence_ids.map(cell).join(", ")}]：${cell(finding.explanation)}`);
  const coverage = conclusion.coverage;
  if (isRecord(coverage)) lines.push("", "## 证据覆盖", "",
    `- 程序全量扫描：${coverage.deterministic_scan_complete}；逐项引用：${cell(coverage.mode)}`,
    `- 总事件 ${coverage.total_events}；已取证 ${coverage.issued_events}；已引用 ${coverage.cited_events}；未引用 ${coverage.remaining_events}`,
    "- 确定性全量聚合与模型逐项取证是不同指标。");
  lines.push("", "## 观测与规则证据", "", "| 证据 | 类型 / 通道 | 开始 / 确认 / 恢复 (s) | 状态 | 原始 CSV 行 |", "|---|---|---|---|---|");
  for (const event of analysis.events) lines.push(`| ${cell(event.evidence_id)} | ${cell(event.code)} / ${cell(event.channel_id ?? event.field)} | ${cell(event.start_s)} / ${cell(event.confirmed_s)} / ${cell(event.recovered_s)} | ${cell(event.status)} | ${cell(event.row_start)} / ${cell(event.row_confirm)} / ${cell(event.row_recover)} |`);
  if (!analysis.events.length) lines.push("| — | 当前规则未检出事件 | — | — | — |");
  lines.push("", "## 配置与可复现性", "", `- 分析版本：${cell(payload.analysis_version)}`,
    `- 配置/导入指纹：${cell(payload.analysis_fingerprint)}`, `- 电流约定：${cell(summary.current_convention ?? "positive_discharge")}`,
    "", "```json", JSON.stringify({ rules: analysis.rules, quality: analysis.quality_configuration ?? null }, null, 2), "```",
    "", "## 用量", "", "```json", JSON.stringify(payload.usage ?? {}, null, 2), "```", "",
    payload.mode === "offline" ? "离线模式未调用模型，API 用量为零。" : "Token 用量为已返回的 SDK 用量；未返回请求另记为未知，不假报零。软件预算不是绝对 Token 或金额上限；不自动重试。",
    "", "## 局限", "");
  for (const limitation of conclusion.limitations ?? []) lines.push(`- ${cell(limitation)}`);
  if (payload.error) lines.push(`- ${cell(payload.error)}`);
  return `${lines.join("\n")}\n`;
}
