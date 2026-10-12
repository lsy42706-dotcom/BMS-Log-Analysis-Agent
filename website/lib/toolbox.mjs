/** Six deterministic evidence tools, shared by the public UI and the Agent. */
import {analyze, sampleReadings, channelMetadata, defaultRules} from './domain.mjs';
import {canonicalEventFact, eventAggregates} from './conclusions.mjs';

const has = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const valueOr = (arguments_, key, fallback) => has(arguments_, key) ? arguments_[key] : fallback;
export function number(value, name) {
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`${name} must be a finite number`);
  return value;
}
export function integer(value, name, low, high) {
  if (!Number.isInteger(value) || value < low || value > high) throw new Error(`${name} must be an integer in [${low}, ${high}]`);
  return value;
}
export function auditValue(value) {
  if (typeof value === 'number' && !Number.isFinite(value)) return '[NONFINITE]';
  if (Array.isArray(value)) return value.map(auditValue);
  if (object(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, auditValue(item)]));
  if (value === null || ['string', 'boolean', 'number'].includes(typeof value)) return value;
  return '[UNSUPPORTED]';
}
function jsonSafe(value) {
  if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('Nonfinite tool result');
  if (Array.isArray(value)) for (const item of value) jsonSafe(item);
  else if (object(value)) for (const item of Object.values(value)) jsonSafe(item);
  else if (value !== null && !['string', 'boolean', 'number'].includes(typeof value)) throw new Error('Non-JSON tool result');
}

const TOOL_FIELDS = {
  summarize_log: [], list_protection_rules: ['offset', 'limit'], detect_faults: ['code', 'offset', 'limit'],
  inspect_window: ['start_s', 'end_s', 'max_samples'], get_evidence: ['evidence_id'], simulate_rule: ['rule_id', 'values', 'step_s'],
};

export class Toolbox {
  constructor(dataset, rules = defaultRules(), {quality = null, maxCalls = 20, maxEvents = 100000} = {}) {
    integer(maxCalls, 'maxCalls', 1, 200);
    this.dataset = dataset; this.rules = rules; this.quality = quality; this.maxCalls = maxCalls;
    this.calls = []; this.issuedEvidence = new Map();
    this.analysis = analyze(dataset, rules, quality, {maxEvents});
    this.rules = this.analysis.rules;
    this._events = new Map(this.analysis.events.map(item => [item.evidence_id, item]));
    this.analysis.event_aggregates = eventAggregates(this.analysis.events);
  }

  invoke(name, arguments_ = {}) {
    if (this.calls.length >= this.maxCalls) throw new Error('Tool call budget exhausted');
    const entry = {tool: name, arguments: auditValue(arguments_), status: 'running'};
    this.calls.push(entry);
    try {
      if (!has(TOOL_FIELDS, name) || !object(arguments_) || Object.keys(arguments_).some(key => !TOOL_FIELDS[name].includes(key))) throw new Error('Unknown tool or arguments');
      let result;
      switch (name) {
        case 'summarize_log': result = this._summary(); break;
        case 'list_protection_rules': result = this._rules(valueOr(arguments_, 'offset', 0), valueOr(arguments_, 'limit', 20)); break;
        case 'detect_faults': result = this._faults(valueOr(arguments_, 'code', 'all'), valueOr(arguments_, 'offset', 0), valueOr(arguments_, 'limit', 20)); break;
        case 'inspect_window': result = this._window(arguments_.start_s, arguments_.end_s, valueOr(arguments_, 'max_samples', 20)); break;
        case 'get_evidence': result = this._evidence(arguments_.evidence_id); break;
        case 'simulate_rule': result = this._simulate(arguments_.rule_id, arguments_.values, valueOr(arguments_, 'step_s', 1)); break;
      }
      jsonSafe(result);
      if (JSON.stringify(result).length > 32000) throw new Error('Tool response too large; request a smaller page or window');
      Object.assign(entry, {status: 'complete', result});
      if (['detect_faults', 'get_evidence'].includes(name)) {
        for (const item of result.event ? [result.event] : result.events || []) this.issuedEvidence.set(item.evidence_id, item);
      }
      return result;
    } catch {
      Object.assign(entry, {status: 'failed', error: 'InvalidToolArguments'});
      throw new Error('Invalid tool arguments or tool budget exhausted');
    }
  }

  _summary() {
    const summary = {...this.analysis.summary}, names = Object.keys(summary.stats).sort();
    summary.stats = Object.fromEntries(names.slice(0, 12).map(name => [name, summary.stats[name]]));
    Object.assign(summary, {stats_total_channels: names.length, stats_returned_channels: Math.min(12, names.length), stats_truncated: names.length > 12});
    summary.channels = Object.fromEntries(Object.entries(summary.channels || {}).map(([name, info]) => [name, Object.fromEntries(Object.entries(info).filter(([key]) => key !== 'stats'))]));
    return {source_name: this.dataset.source_name, source_sha256: this.dataset.sha256, ...summary,
      event_count: this.analysis.events.length, event_aggregate_summary: this._aggregateSummary(this.analysis.events),
      event_groups: this.analysis.event_aggregates.slice(0, 12), event_groups_returned: Math.min(12, this.analysis.event_aggregates.length),
      event_groups_total: this.analysis.event_aggregates.length, event_groups_truncated: this.analysis.event_aggregates.length > 12,
      units: {timestamp_s: 's', ...Object.fromEntries(Object.entries(this.analysis.summary.channels || {}).map(([name, info]) => [name, info.unit]))},
      current_convention: this.dataset.current_convention || 'positive_discharge'};
  }

  _rules(offset = 0, limit = 20) {
    const rules = this.analysis.rules;
    integer(offset, 'offset', 0, rules.length); integer(limit, 'limit', 1, 20);
    const page = rules.slice(offset, offset + limit);
    return {scope: 'illustrative software analysis, not calibrated battery protection', rules: page,
      total: rules.length, next_offset: offset + page.length < rules.length ? offset + page.length : null,
      quality_configuration: this.analysis.quality_configuration ?? null, not_evaluated_rules: this.analysis.summary.not_evaluated_rules || []};
  }

  _aggregateSummary(events) {
    const byCode = Object.create(null), byStatus = Object.create(null);
    for (const event of events) { byCode[event.code] = (byCode[event.code] || 0) + 1; byStatus[event.status] = (byStatus[event.status] || 0) + 1; }
    return {total_events: events.length, by_code: Object.fromEntries(Object.entries(byCode).sort()), by_status: Object.fromEntries(Object.entries(byStatus).sort()),
      scan_scope: 'entire_selected_log', individual_evidence_coverage: 'Counts are deterministic full-scope aggregates, not model inspection of each event.'};
  }

  _faults(code = 'all', offset = 0, limit = 20) {
    integer(limit, 'limit', 1, 40);
    if (typeof code !== 'string' || code.length > 64) throw new Error('Invalid fault code');
    let events = this.analysis.events;
    if (code !== 'all') {
      const codes = new Set([...events.map(event => event.code), ...this.rules.map(rule => rule.code)]);
      if (!codes.has(code)) throw new Error('Unknown fault code');
      events = events.filter(event => event.code === code);
    }
    integer(offset, 'offset', 0, events.length);
    const page = events.slice(offset, offset + limit);
    return {total: events.length, events: page, facts: page.map(canonicalEventFact),
      next_offset: offset + page.length < events.length ? offset + page.length : null,
      aggregate_summary: this._aggregateSummary(events), filter_code: code};
  }

  _window(start_s, end_s, max_samples = 20) {
    number(start_s, 'start_s'); number(end_s, 'end_s'); integer(max_samples, 'max_samples', 1, 50);
    if (start_s > end_s) throw new Error('Window is reversed');
    const selected = this.dataset.samples.filter(sample => start_s <= sample.timestamp_s && sample.timestamp_s <= end_s);
    const critical = [], reasons = new Map();
    function add(index, reason) {
      if (!reasons.has(index)) { critical.push(index); reasons.set(index, new Set()); }
      reasons.get(index).add(reason);
    }
    if (selected.length) {
      add(0, 'first'); add(selected.length - 1, 'last');
      const values = selected.map(sampleReadings), channels = [...new Set(values.flatMap(value => Object.keys(value)))].sort();
      for (const name of channels) {
        const valid = values.map((value, index) => [index, value[name]]).filter(([, value]) => value !== null && value !== undefined);
        if (valid.length) {
          const minimum = valid.reduce((best, item) => item[1] < best[1] ? item : best), maximum = valid.reduce((best, item) => item[1] > best[1] ? item : best);
          add(minimum[0], `${name}:min`); add(maximum[0], `${name}:max`);
        }
      }
      const positions = new Map(selected.map((sample, index) => [sample.timestamp_s, index]));
      for (const event of this.analysis.events) for (const key of ['start_s', 'confirmed_s', 'recovered_s']) if (positions.has(event[key])) add(positions.get(event[key]), `event:${key}`);
    }
    const chosen = new Set(critical.slice(0, max_samples)), desired = Math.min(max_samples, selected.length);
    while (chosen.size < desired) {
      let candidate = -1, greatestDistance = -1;
      for (let index = 0; index < selected.length; index++) if (!chosen.has(index)) {
        const distance = Math.min(...[...chosen].map(present => Math.abs(index - present)));
        if (distance > greatestDistance) { candidate = index; greatestDistance = distance; }
      }
      chosen.add(candidate);
    }
    const shown = [...chosen].sort((a, b) => a - b).map(index => selected[index]), missing = critical.filter(index => !chosen.has(index));
    return {matched_samples: selected.length, returned_samples: shown.length, downsampled: shown.length < selected.length,
      samples: shown.map(sample => structuredClone(sample)), sampling: {
        method: 'endpoints_extrema_event_boundaries_then_gap_fill', critical_points_total: critical.length,
        critical_points_retained: critical.length - missing.length, critical_points_complete: !missing.length,
        critical_points_missing: missing.length, missing_point_examples: missing.slice(0, 5).map(index => ({timestamp_s: selected[index].timestamp_s, reasons: [...reasons.get(index)].sort()})),
        not_a_full_resolution_waveform: shown.length < selected.length}};
  }
  window(start_s, end_s, max_samples = 20) { return this._window(start_s, end_s, max_samples); }

  _evidence(evidence_id) {
    if (typeof evidence_id !== 'string' || !this._events.has(evidence_id)) throw new Error('Unknown evidence ID');
    const event = this._events.get(evidence_id);
    return {event, fact: canonicalEventFact(event), source_sha256: this.dataset.sha256};
  }

  _simulate(rule_id, values, step_s = 1) {
    if (!Array.isArray(values) || values.length < 1 || values.length > 128) throw new Error('Simulation requires 1..128 samples');
    number(step_s, 'step_s'); if (step_s < 0.01 || step_s > 10) throw new Error('Invalid simulation step');
    const rule = this.rules.find(item => item.rule_id === rule_id); if (!rule) throw new Error('Unknown rule ID');
    const clean = values.map(value => number(value, 'value'));
    if (['cell_spread_v', 'temperature_spread_c'].includes(rule.field) && clean.some(value => value < 0)) throw new Error('Synthetic spread values must be nonnegative');
    const samples = clean.map((value, index) => {
      const sample = {timestamp_s: index * step_s, row_number: index + 2, cell_voltage_v: 3.7, current_a: 10, temperature_c: 25, cell_voltages_v: {}, temperatures_c: {}};
      if (rule.field === 'cell_spread_v') { sample.cell_voltage_v = null; sample.cell_voltages_v = {C01: 3.7, C02: 3.7 + value}; }
      else if (rule.field === 'temperature_spread_c') { sample.temperature_c = null; sample.temperatures_c = {T01: 25, T02: 25 + value}; }
      else if (rule.channel_id !== null || !has(sample, rule.field)) {
        const metadata = channelMetadata(rule.field), kind = metadata.kind, channelId = rule.channel_id || metadata.channel_id;
        sample[kind] = null; (kind === 'cell_voltage_v' ? sample.cell_voltages_v : sample.temperatures_c)[channelId] = value;
      } else sample[rule.field] = value;
      return sample;
    });
    // This label is intentionally not a source-file hash and is never issued as source evidence.
    const synthetic = {samples, quality_issues: [], source_name: 'synthetic_rule_probe', sha256: 'synthetic_not_source_evidence',
      pack_id: 'pack_1', current_convention: this.dataset.current_convention || 'positive_discharge', import_metadata: {}};
    const result = analyze(synthetic, [rule]);
    return {synthetic: true, rule_id, events: result.events.map(event => ({...event, evidence_id: `SIM-${event.evidence_id}`})),
      note: 'Synthetic rule probe is not evidence of a fault in the input log.'};
  }
}
