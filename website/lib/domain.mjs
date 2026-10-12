/** Deterministic BMS demonstration rules. No credentials, model or hardware access. */
export const SENSOR_FIELDS = ['cell_voltage_v', 'current_a', 'temperature_c'];
export const MAX_BYTES = 5 * 1024 * 1024;
export const MAX_SAMPLES = 20000;
export const MAX_SENSOR_CHANNELS = 64;
export const MAX_RULE_CHANNEL_EVALUATIONS = 256;
export const MAX_EVENTS = 100000;
export const AGGREGATE_FIELDS = ['cell_spread_v', 'temperature_spread_c'];
export const QUALITY_CODES = new Set(['SENSOR_INVALID', 'SAMPLE_GAP', 'SENSOR_IMPLAUSIBLE', 'SENSOR_STUCK', 'SENSOR_SPIKE']);
const TOKEN = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
const CHANNEL = /^(cell_voltage_v|temperature_c)_([A-Za-z][A-Za-z0-9_]{0,31})$/;
const CHANNEL_ID = /^[A-Za-z][A-Za-z0-9_]{0,31}$/;
export class DatasetError extends Error { constructor(message) { super(message); this.name = 'DatasetError'; } }
export class RulesError extends Error { constructor(message) { super(message); this.name = 'RulesError'; } }
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const finite = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
const has = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const compareText = (a, b) => a < b ? -1 : a > b ? 1 : 0;

export function channelMetadata(field) {
  const match = typeof field === 'string' ? field.match(CHANNEL) : null;
  const kind = match ? match[1] : field, channel_id = match ? match[2] : null;
  return {field, kind, channel_id,
    cell_id: kind === 'cell_voltage_v' ? channel_id || 'legacy_cell' : null,
    probe_id: kind === 'temperature_c' ? channel_id || 'legacy_probe' : null};
}
const isSensorField = field => typeof field === 'string' && (SENSOR_FIELDS.includes(field) || CHANNEL.test(field));

export function sampleReadings(sample) {
  const cells = sample.cell_voltages_v || {}, probes = sample.temperatures_c || {};
  const readings = {current_a: sample.current_a};
  if (!Object.keys(cells).length || sample.cell_voltage_v !== null) readings.cell_voltage_v = sample.cell_voltage_v;
  if (!Object.keys(probes).length || sample.temperature_c !== null) readings.temperature_c = sample.temperature_c;
  for (const [key, value] of Object.entries(cells)) readings[`cell_voltage_v_${key}`] = value;
  for (const [key, value] of Object.entries(probes)) readings[`temperature_c_${key}`] = value;
  return readings;
}

export function sensorChannels(dataset) {
  const channels = new Set();
  for (const sample of dataset.samples) for (const name of Object.keys(sampleReadings(sample))) channels.add(name);
  for (const name of Array.isArray(dataset.import_metadata?.channels) ? dataset.import_metadata.channels : []) {
    if (isSensorField(name)) channels.add(name);
  }
  return [...channels].sort(compareText);
}

function reading(sample, field, channels = []) {
  if (SENSOR_FIELDS.includes(field)) return sample[field];
  const match = field.match(CHANNEL);
  if (match) {
    const mapping = match[1] === 'cell_voltage_v' ? sample.cell_voltages_v : sample.temperatures_c;
    return has(mapping, match[2]) ? mapping[match[2]] : null;
  }
  if (AGGREGATE_FIELDS.includes(field)) {
    const kind = field === 'cell_spread_v' ? 'cell_voltage_v' : 'temperature_c';
    const values = channels.filter(name => channelMetadata(name).kind === kind).map(name => reading(sample, name));
    if (values.length < 2 || values.some(value => value === null)) return null;
    return finite(Math.max(...values) - Math.min(...values));
  }
  return null;
}

export function defaultRules() {
  return [
    ['overcurrent', 'OVERCURRENT', 'current_a', 'abs_high', 50, 45, 2, 2],
    ['overtemperature', 'OVERTEMPERATURE', 'temperature_c', 'high', 60, 55, 3, 2],
    ['undervoltage', 'UNDERVOLTAGE', 'cell_voltage_v', 'low', 3, 3.2, 2, 2],
    ['overvoltage', 'OVERVOLTAGE', 'cell_voltage_v', 'high', 4.25, 4.15, 2, 2],
  ].map(([rule_id, code, field, direction, threshold, recovery_threshold, confirm_s, recovery_s]) =>
    ({rule_id, code, field, direction, threshold, recovery_threshold, confirm_s, recovery_s,
      max_gap_s: 2, channel_id: null, current_mode: 'any'}));
}

function unitTransform(name, unit) {
  const kind = channelMetadata(name).kind;
  const choices = {
    timestamp_s: {s: [1, 0], ms: [0.001, 0]},
    cell_voltage_v: {V: [1, 0], mV: [0.001, 0]},
    current_a: {A: [1, 0], mA: [0.001, 0]},
    temperature_c: {degC: [1, 0], C: [1, 0], K: [1, -273.15]},
  };
  if (typeof unit !== 'string' || !has(choices[kind] || {}, unit)) throw new DatasetError('Unsupported unit for a selected column.');
  return choices[kind][unit];
}

function numericCell(raw, transform) {
  if (typeof raw !== 'string') return null;
  // Match Python float's Unicode decimal digits / whitespace without JS's
  // extra BOM trimming or its coercion of an empty string to zero.
  const text = raw.replace(/^\p{White_Space}+|\p{White_Space}+$/gu, '').replace(/\p{Decimal_Number}/gu, character => {
    if (character >= '0' && character <= '9') return character;
    const codepoint = character.codePointAt(0);
    let first = codepoint;
    while (first > 0 && /\p{Decimal_Number}/u.test(String.fromCodePoint(first - 1))) first--;
    return String((codepoint - first) % 10);
  });
  // Python float accepts underscores between decimal digits, not empty/hex cells.
  if (!/^[+-]?(?:(?:\d(?:_?\d)*)(?:\.(?:\d(?:_?\d)*)?)?|\.(?:\d(?:_?\d)*))(?:[eE][+-]?\d(?:_?\d)*)?$/.test(text)) return null;
  return finite(Number(text.replaceAll('_', '')) * transform[0] + transform[1]);
}

/** A strict RFC-style CSV scanner. Physical line numbers match Python csv.DictReader. */
function* parseRows(text) {
  let cells = [], cell = '', state = 'start', rowContent = false, line = 1, firstRecord = true;
  function endCell() { cells.push(cell); cell = ''; state = 'start'; }
  function endRow() {
    let record = null;
    if (firstRecord || rowContent || cells.length || cell.length) { endCell(); record = {cells, line}; }
    firstRecord = false;
    cells = []; cell = ''; state = 'start'; rowContent = false;
    return record;
  }
  for (let index = 0; index < text.length; index++) {
    const character = text[index];
    const newline = character === '\n' || character === '\r';
    const combined = character === '\r' && text[index + 1] === '\n';
    if (state === 'quoted') {
      if (character === '"') state = 'after_quote';
      else { cell += combined ? '\r\n' : character; if (newline) { if (combined) index++; line++; } }
      continue;
    }
    if (state === 'after_quote') {
      if (character === '"') { cell += '"'; state = 'quoted'; }
      else if (character === ',') { endCell(); rowContent = true; }
      else if (newline) { const row = endRow(); if (row) yield row; if (combined) index++; line++; }
      else throw new DatasetError('CSV syntax is invalid.');
      continue;
    }
    if (newline) { const row = endRow(); if (row) yield row; if (combined) index++; line++; }
    else if (character === ',') { endCell(); rowContent = true; }
    else if (character === '"' && state === 'start') { state = 'quoted'; rowContent = true; }
    else { cell += character; state = 'plain'; rowContent = true; }
  }
  if (state === 'quoted') throw new DatasetError('CSV syntax is invalid.');
  const last = endRow(); if (last) yield last;
}

export async function loadCsvBytes(content, sourceName = 'upload.csv', columnMap = null, units = null,
  {current_convention = 'positive_discharge', pack_id = 'pack_1'} = {}) {
  let bytes;
  if (typeof content === 'string') bytes = new TextEncoder().encode(content);
  else if (content instanceof Uint8Array) bytes = content;
  else if (content instanceof ArrayBuffer) bytes = new Uint8Array(content);
  else throw new DatasetError('CSV content must be bytes or UTF-8 text.');
  if (bytes.byteLength > MAX_BYTES) throw new DatasetError('CSV exceeds the 5 MiB size limit.');
  if (!['positive_discharge', 'positive_charge'].includes(current_convention)) throw new DatasetError('Current convention must be positive_discharge or positive_charge.');
  if (typeof pack_id !== 'string' || !TOKEN.test(pack_id)) throw new DatasetError('Pack identifier must be a short ASCII identifier.');
  if (typeof sourceName !== 'string') throw new DatasetError('Source name must be text.');
  const source_name = sourceName.split(/[/\\]/).at(-1).slice(0, 160) || 'upload.csv';
  let decoded;
  try { decoded = new TextDecoder('utf-8', {fatal: true}).decode(bytes); }
  catch { throw new DatasetError('CSV must be UTF-8 encoded.'); }
  if (columnMap !== null && !object(columnMap)) throw new DatasetError('Column mapping must be an object.');
  if (units !== null && !object(units)) throw new DatasetError('Unit mapping must be an object.');
  const parsed = parseRows(decoded), headers = parsed.next().value?.cells;
  if (!headers?.length) throw new DatasetError('CSV requires timestamp_s, current_a, voltage and temperature columns.');
  if (new Set(headers).size !== headers.length) throw new DatasetError('CSV contains duplicate column names.');
  const mapping = {};
  for (const name of headers) if (name === 'timestamp_s' || isSensorField(name)) mapping[name] = name;
  for (const [name, source] of Object.entries(columnMap || {})) {
    if (name !== 'timestamp_s' && !isSensorField(name)) throw new DatasetError('Column mapping contains an unsupported canonical name.');
    if (typeof source !== 'string' || !headers.includes(source)) throw new DatasetError('Column mapping references a missing source column.');
    mapping[name] = source;
  }
  if (new Set(Object.values(mapping)).size !== Object.keys(mapping).length) throw new DatasetError('Each selected source column must map to exactly one canonical field.');
  const channels = Object.keys(mapping).filter(name => name !== 'timestamp_s').sort(compareText);
  if (channels.length > MAX_SENSOR_CHANNELS) throw new DatasetError('CSV exceeds the 64-sensor-channel limit.');
  const kinds = new Set(channels.map(name => channelMetadata(name).kind));
  if (!has(mapping, 'timestamp_s') || SENSOR_FIELDS.some(name => !kinds.has(name))) throw new DatasetError('CSV requires timestamp_s, current_a, voltage and temperature columns.');
  const defaults = {timestamp_s: 's', cell_voltage_v: 'V', current_a: 'A', temperature_c: 'degC'};
  const suppliedUnits = units || {};
  if (Object.keys(suppliedUnits).some(name => !has(mapping, name) && !SENSOR_FIELDS.includes(name))) throw new DatasetError('Unit mapping references an unselected canonical field.');
  for (const [name, unit] of Object.entries(suppliedUnits)) unitTransform(name, unit);
  const selectedUnits = {}, transforms = {};
  for (const name of Object.keys(mapping)) {
    const kind = channelMetadata(name).kind;
    selectedUnits[name] = suppliedUnits[name] ?? suppliedUnits[kind] ?? defaults[kind];
    transforms[name] = unitTransform(name, selectedUnits[name]);
  }
  const samples = [], quality_issues = [];
  for (const {cells: cellsRaw, line: row} of parsed) {
    if (samples.length >= MAX_SAMPLES) throw new DatasetError('CSV exceeds the 20000-sample limit.');
    if (cellsRaw.length > headers.length) throw new DatasetError(`Row ${row}: CSV has more cells than declared columns.`);
    const raw = Object.fromEntries(headers.map((name, index) => [name, cellsRaw[index]]));
    const timestamp = numericCell(raw[mapping.timestamp_s], transforms.timestamp_s);
    if (timestamp === null) throw new DatasetError(`Row ${row}: timestamp_s must be finite and numeric.`);
    if (samples.length && timestamp <= samples.at(-1).timestamp_s) throw new DatasetError(`Row ${row}: timestamp_s must strictly increase.`);
    if (samples.length && !Number.isFinite(timestamp - samples[0].timestamp_s)) throw new DatasetError(`Row ${row}: timestamp_s range is too large.`);
    const sample = {timestamp_s: timestamp, cell_voltage_v: null, current_a: null, temperature_c: null,
      row_number: row, cell_voltages_v: {}, temperatures_c: {}};
    for (const name of channels) {
      const value = numericCell(raw[mapping[name]], transforms[name]);
      if (value === null) quality_issues.push({code: 'SENSOR_INVALID', row_number: row, timestamp_s: timestamp,
        field: name, message: `Row ${row}: ${name} is missing, nonnumeric, or nonfinite.`});
      const match = name.match(CHANNEL);
      if (match) (match[1] === 'cell_voltage_v' ? sample.cell_voltages_v : sample.temperatures_c)[match[2]] = value;
      else sample[name] = value;
    }
    samples.push(sample);
  }
  if (!samples.length) throw new DatasetError('CSV must contain at least one sample.');
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const sha256 = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('');
  return {samples, quality_issues, source_name, sha256, pack_id, current_convention,
    import_metadata: {schema_version: 2, channels, column_map: {...mapping}, source_units: selectedUnits, normalized_units: {...defaults}}};
}

const RULE_FIELDS = ['rule_id', 'code', 'field', 'direction', 'threshold', 'recovery_threshold', 'confirm_s', 'recovery_s', 'max_gap_s', 'channel_id', 'current_mode'];
export function validateRules(rules) {
  if (!Array.isArray(rules) || !rules.length || rules.length > 100) throw new RulesError('Rules must be a nonempty list of at most 100 rules.');
  const identifiers = new Set();
  return rules.map((input, index) => {
    const prefix = `Rule ${index + 1}`;
    if (!object(input)) throw new RulesError(`${prefix}: invalid rule structure.`);
    if (Object.keys(input).some(key => !RULE_FIELDS.includes(key)) || RULE_FIELDS.slice(0, 8).some(key => !has(input, key))) throw new RulesError(`${prefix}: missing or unsupported rule fields.`);
    const rule = {max_gap_s: 2, channel_id: null, current_mode: 'any', ...input};
    for (const key of ['rule_id', 'code']) if (typeof rule[key] !== 'string' || !TOKEN.test(rule[key])) throw new RulesError(`${prefix}: ${key} must be a short ASCII identifier.`);
    if (identifiers.has(rule.rule_id)) throw new RulesError(`${prefix}: rule_id must be unique.`);
    identifiers.add(rule.rule_id);
    if (!isSensorField(rule.field) && !AGGREGATE_FIELDS.includes(rule.field)) throw new RulesError(`${prefix}: unsupported sensor field.`);
    if (rule.channel_id !== null && (typeof rule.channel_id !== 'string' || !CHANNEL_ID.test(rule.channel_id) || !['cell_voltage_v', 'temperature_c'].includes(rule.field))) throw new RulesError(`${prefix}: channel_id requires a generic voltage or temperature field.`);
    if (!['any', 'charge', 'discharge'].includes(rule.current_mode)) throw new RulesError(`${prefix}: current_mode must be any, charge, or discharge.`);
    if (rule.current_mode !== 'any' && (rule.field !== 'current_a' || rule.direction !== 'abs_high')) throw new RulesError(`${prefix}: directional current rules require current_a and abs_high.`);
    if (!['high', 'low', 'abs_high'].includes(rule.direction)) throw new RulesError(`${prefix}: direction must be high, low, or abs_high.`);
    for (const key of ['threshold', 'recovery_threshold', 'confirm_s', 'recovery_s', 'max_gap_s']) if (finite(rule[key]) === null) throw new RulesError(`${prefix}: ${key} must be finite and numeric.`);
    if (rule.confirm_s < 0 || rule.recovery_s < 0 || rule.max_gap_s <= 0) throw new RulesError(`${prefix}: delays must be nonnegative and max_gap_s must be positive.`);
    if (!(rule.direction === 'low' ? rule.recovery_threshold >= rule.threshold : rule.recovery_threshold <= rule.threshold)) throw new RulesError(`${prefix}: recovery_threshold must provide valid hysteresis.`);
    if (rule.direction === 'abs_high' && (rule.threshold < 0 || rule.recovery_threshold < 0)) throw new RulesError(`${prefix}: absolute-current thresholds must be nonnegative.`);
    return Object.fromEntries(RULE_FIELDS.map(key => [key, rule[key]]));
  });
}

export function validateQuality(input = null) {
  if (input !== null && !object(input)) throw new RulesError('Quality configuration must be an object.');
  const config = {bounds: {}, stuck_s: {}, stuck_epsilon: {}, max_rate: {}, max_gap_s: 2, ...(input || {})};
  if (Object.keys(config).some(key => !['bounds', 'stuck_s', 'stuck_epsilon', 'max_rate', 'max_gap_s'].includes(key))) throw new RulesError('Quality configuration contains unsupported fields.');
  const result = {};
  for (const key of ['bounds', 'stuck_s', 'stuck_epsilon', 'max_rate']) {
    const mapping = config[key];
    if (!object(mapping) || Object.keys(mapping).length > MAX_SENSOR_CHANNELS) throw new RulesError('Quality maps must contain at most 64 canonical sensor fields.');
    const values = {};
    for (const [name, value] of Object.entries(mapping)) {
      if (!isSensorField(name)) throw new RulesError('Quality configuration contains an unsupported sensor field.');
      if (key === 'bounds') {
        if (!Array.isArray(value) || value.length !== 2 || value.some(item => finite(item) === null) || value[0] > value[1]) throw new RulesError('Quality bounds require finite ordered minimum and maximum values.');
        values[name] = [...value];
      } else {
        if (finite(value) === null || value < 0 || (key !== 'stuck_epsilon' && value === 0)) throw new RulesError('Quality durations and rates must be positive; epsilon must be nonnegative.');
        values[name] = value;
      }
    }
    result[key] = values;
  }
  if (finite(config.max_gap_s) === null || config.max_gap_s <= 0) throw new RulesError('Quality max_gap_s must be finite and positive.');
  result.max_gap_s = config.max_gap_s;
  return result;
}

export function profileFromDict(payload) {
  if (object(payload)) {
    if (Object.keys(payload).some(key => !['rules', 'quality', 'description'].includes(key))) throw new RulesError('Profile contains unsupported top-level fields.');
    if (has(payload, 'description') && (typeof payload.description !== 'string' || payload.description.length > 1000)) throw new RulesError('Profile description must be bounded text.');
  }
  return {rules: validateRules(object(payload) ? payload.rules : payload), quality: validateQuality(object(payload) ? payload.quality ?? {} : {})};
}

function validateSamples(dataset) {
  if (!object(dataset) || !Array.isArray(dataset.samples) || !dataset.samples.length || dataset.samples.length > MAX_SAMPLES) throw new DatasetError('Dataset must contain between 1 and 20000 samples.');
  const seen = new Set((Array.isArray(dataset.import_metadata?.channels) ? dataset.import_metadata.channels : []).filter(isSensorField));
  if (seen.size > MAX_SENSOR_CHANNELS) throw new DatasetError('Dataset exceeds the 64-sensor-channel limit.');
  if (typeof dataset.pack_id !== 'string' || !TOKEN.test(dataset.pack_id)) throw new DatasetError('Pack identifier must be a short ASCII identifier.');
  if (!['positive_discharge', 'positive_charge'].includes(dataset.current_convention)) throw new DatasetError('Current convention must be positive_discharge or positive_charge.');
  let previous = null, first = null;
  return dataset.samples.map(sample => {
    if (!object(sample)) throw new DatasetError('Dataset contains an invalid sample structure.');
    if (!Number.isInteger(sample.row_number) || sample.row_number < 1) throw new DatasetError('Sample row_number must be a positive integer.');
    const timestamp = finite(sample.timestamp_s);
    if (timestamp === null) throw new DatasetError(`Row ${sample.row_number}: timestamp_s must be finite and numeric.`);
    if (previous !== null && timestamp <= previous) throw new DatasetError(`Row ${sample.row_number}: timestamp_s must strictly increase.`);
    if (first === null) first = timestamp;
    if (!Number.isFinite(timestamp - first)) throw new DatasetError(`Row ${sample.row_number}: timestamp_s range is too large.`);
    const mappings = [];
    for (const mapping of [sample.cell_voltages_v || {}, sample.temperatures_c || {}]) {
      if (!object(mapping)) throw new DatasetError('Sample channel readings must be objects.');
      if (Object.keys(mapping).length > MAX_SENSOR_CHANNELS) throw new DatasetError('Dataset exceeds the 64-sensor-channel limit.');
      if (Object.keys(mapping).some(key => !CHANNEL_ID.test(key))) throw new DatasetError('Channel identifiers must be short ASCII identifiers.');
      mappings.push(Object.fromEntries(Object.entries(mapping).map(([key, value]) => [key, finite(value)])));
    }
    const normalized = {timestamp_s: timestamp, row_number: sample.row_number,
      ...Object.fromEntries(SENSOR_FIELDS.map(key => [key, finite(sample[key])])), cell_voltages_v: mappings[0], temperatures_c: mappings[1]};
    for (const name of Object.keys(sampleReadings(normalized))) seen.add(name);
    if (seen.size > MAX_SENSOR_CHANNELS) throw new DatasetError('Dataset exceeds the 64-sensor-channel limit.');
    previous = timestamp;
    return normalized;
  });
}

function event(code, rule_id, field, start, confirmed, trigger_value, peak_value, severity, message) {
  return {code, rule_id, field, start_s: start.timestamp_s, confirmed_s: confirmed.timestamp_s,
    recovered_s: null, status: 'active', trigger_value, peak_value, row_start: start.row_number,
    row_confirm: confirmed.row_number, row_recover: null, severity, message};
}
function recover(event, sample) { Object.assign(event, {recovered_s: sample.timestamp_s, row_recover: sample.row_number, status: 'recovered'}); }
function stronger(rule, current, candidate) {
  if (rule.direction === 'low') return Math.min(current, candidate);
  if (rule.direction === 'abs_high') return Math.abs(candidate) > Math.abs(current) ? candidate : current;
  return Math.max(current, candidate);
}
function matches(rule, value, recovering = false) {
  if (rule.direction === 'abs_high') value = Math.abs(value);
  const threshold = recovering ? rule.recovery_threshold : rule.threshold;
  return rule.direction === 'low' ? recovering ? value >= threshold : value <= threshold : recovering ? value <= threshold : value >= threshold;
}
function addBounded(events, value, maximum) {
  if (events.length >= maximum) throw new DatasetError(`Analysis exceeds the ${maximum}-event limit.`);
  events.push(value);
}

function ruleEvents(samples, rule, field, channels, currentConvention, maximum) {
  const events = [];
  let pending = null, pendingPeak = null, recovering = null, active = null, previous = null;
  for (const sample of samples) {
    const value = reading(sample, field, channels);
    let modeMatches = true;
    if (value !== null && rule.current_mode !== 'any') {
      const discharge = currentConvention === 'positive_discharge' ? value > 0 : value < 0;
      modeMatches = value !== 0 && discharge === (rule.current_mode === 'discharge');
    }
    const testedValue = modeMatches ? value : 0;
    const interrupted = previous !== null && sample.timestamp_s - previous.timestamp_s > rule.max_gap_s;
    previous = sample;
    if (interrupted) { pending = null; pendingPeak = null; recovering = null; }
    if (value === null) { pending = null; pendingPeak = null; recovering = null; continue; }
    if (active === null) {
      if (!modeMatches || !matches(rule, testedValue)) { pending = null; pendingPeak = null; continue; }
      if (pending === null) { pending = sample; pendingPeak = value; }
      else pendingPeak = stronger(rule, pendingPeak, value);
      if (sample.timestamp_s - pending.timestamp_s >= rule.confirm_s) {
        active = event(rule.code, rule.rule_id, field, pending, sample, reading(pending, field, channels), pendingPeak,
          'error', 'Demonstration protection rule confirmed by sampled elapsed time; not hardware validation.');
        active.current_mode = rule.current_mode;
        addBounded(events, active, maximum);
        pending = null; pendingPeak = null;
      }
    }
    if (active !== null) {
      if (modeMatches) active.peak_value = stronger(rule, active.peak_value, value);
      if (matches(rule, testedValue, true)) {
        if (recovering === null) recovering = sample;
        if (sample.timestamp_s - recovering.timestamp_s >= rule.recovery_s) { recover(active, sample); active = null; recovering = null; }
      } else recovering = null;
    }
  }
  return events;
}

function sensorEvents(samples, channels, maximum) {
  const events = [];
  for (const field of channels) {
    let active = null;
    for (const sample of samples) {
      if (reading(sample, field) === null) {
        if (active === null) {
          active = event('SENSOR_INVALID', `sensor_${field}`, field, sample, sample, null, null, 'warning',
            'Missing, nonnumeric, or nonfinite sensor readings interrupt protection timing.');
          addBounded(events, active, maximum);
        }
      } else if (active !== null) { recover(active, sample); active = null; }
    }
  }
  return events;
}

function gapEvents(samples, maxGap, maximum) {
  const events = [];
  for (let index = 1; index < samples.length; index++) {
    const before = samples[index - 1], after = samples[index], gap = after.timestamp_s - before.timestamp_s;
    if (gap > maxGap) {
      const value = event('SAMPLE_GAP', 'sample_gap', 'timestamp_s', before, after, gap, gap, 'warning',
        'Sampling interval exceeds a rule limit; continuous fault or recovery is not inferred across this gap.');
      recover(value, after); addBounded(events, value, maximum);
    }
  }
  return events;
}
const qualitySetting = (mapping, field, fallback = null) => mapping[field] ?? mapping[channelMetadata(field).kind] ?? fallback;

function qualityEvents(samples, channels, config, maximum) {
  if (![config.bounds, config.stuck_s, config.max_rate].some(mapping => Object.keys(mapping).length)) return [];
  const events = [];
  for (const field of channels) {
    const bounds = qualitySetting(config.bounds, field), stuckS = qualitySetting(config.stuck_s, field);
    const epsilon = qualitySetting(config.stuck_epsilon, field, 0), maxRate = qualitySetting(config.max_rate, field);
    let boundsActive = null, stuckActive = null, anchor = null, previous = null, anchorValue = null, previousValue = null;
    for (const sample of samples) {
      const value = reading(sample, field), interrupted = previous !== null && sample.timestamp_s - previous.timestamp_s > config.max_gap_s;
      if (value === null || interrupted) { anchor = null; anchorValue = null; }
      if (value === null) { previous = sample; previousValue = null; continue; }
      if (bounds !== null) {
        const outside = value < bounds[0] || value > bounds[1];
        if (outside && boundsActive === null) {
          boundsActive = event('SENSOR_IMPLAUSIBLE', 'quality_bounds', field, sample, sample, value, value, 'warning',
            'Reading is outside user-configured plausibility bounds; not a validated physical fault.');
          addBounded(events, boundsActive, maximum);
        } else if (!outside && boundsActive !== null) { recover(boundsActive, sample); boundsActive = null; }
        else if (outside && boundsActive !== null) {
          const distance = reading => Math.max(bounds[0] - reading, reading - bounds[1], 0);
          if (distance(value) > distance(boundsActive.peak_value)) boundsActive.peak_value = value;
        }
      }
      if (stuckS !== null) {
        if (stuckActive !== null && Math.abs(value - stuckActive.trigger_value) > epsilon) {
          recover(stuckActive, sample); stuckActive = null; anchor = sample; anchorValue = value;
        }
        if (anchor === null || Math.abs(value - anchorValue) > epsilon) { anchor = sample; anchorValue = value; }
        if (stuckActive === null && sample.timestamp_s - anchor.timestamp_s >= stuckS) {
          stuckActive = event('SENSOR_STUCK', 'quality_stuck', field, anchor, sample, anchorValue, value, 'warning',
            'Low-variation run exceeded a configured duration; stable real conditions can produce the same pattern.');
          stuckActive.epsilon = epsilon; addBounded(events, stuckActive, maximum);
        }
      }
      if (maxRate !== null && previous !== null && previousValue !== null && !interrupted) {
        const rate = Math.abs(value - previousValue) / (sample.timestamp_s - previous.timestamp_s);
        if (rate > maxRate) {
          const spike = event('SENSOR_SPIKE', 'quality_spike', field, previous, sample, value, value, 'warning',
            'Observed sample-to-sample rate exceeded a configured limit; no interpolated waveform is inferred.');
          spike.rate_per_s = finite(rate); spike.rate_exceeds_numeric_range = !Number.isFinite(rate);
          recover(spike, sample); addBounded(events, spike, maximum);
        }
      }
      previous = sample; previousValue = value;
    }
  }
  return events;
}

function ruleFields(rule, channels) {
  if (AGGREGATE_FIELDS.includes(rule.field)) {
    const kind = rule.field === 'cell_spread_v' ? 'cell_voltage_v' : 'temperature_c';
    return channels.filter(name => channelMetadata(name).kind === kind).length >= 2 ? [rule.field] : [];
  }
  if (rule.channel_id !== null) {
    const target = `${rule.field}_${rule.channel_id}`;
    if (!channels.includes(target)) throw new RulesError('A channel-specific rule references a channel absent from the dataset.');
    return [target];
  }
  if (['cell_voltage_v', 'temperature_c'].includes(rule.field)) return channels.filter(name => channelMetadata(name).kind === rule.field);
  if (!channels.includes(rule.field)) throw new RulesError('A rule references a channel absent from the dataset.');
  return [rule.field];
}

// Sum IEEE-754 values exactly as dyadic integers, then round once (Python statistics.mean).
const floatView = new DataView(new ArrayBuffer(8));
function dyadic(value) {
  floatView.setFloat64(0, value);
  const bits = floatView.getBigUint64(0), exponent = Number((bits >> 52n) & 2047n);
  let significand = bits & ((1n << 52n) - 1n);
  if (exponent) significand |= 1n << 52n;
  if (bits >> 63n) significand = -significand;
  return [significand, exponent ? exponent - 1075 : -1074];
}
function roundQuotient(numerator, denominator) {
  const quotient = numerator / denominator, remainder = numerator % denominator, twice = remainder * 2n;
  return quotient + (twice > denominator || (twice === denominator && (quotient & 1n)) ? 1n : 0n);
}
function exactMean(values) {
  if (!values.length) return null;
  const pairs = values.map(dyadic), exponent = Math.min(...pairs.map(pair => pair[1]));
  let sum = 0n;
  for (const [mantissa, power] of pairs) sum += mantissa << BigInt(power - exponent);
  if (!sum) return 0;
  const negative = sum < 0n; if (negative) sum = -sum;
  const denominator = BigInt(values.length);
  let ratioExponent = sum.toString(2).length - denominator.toString(2).length;
  if (ratioExponent >= 0 ? sum < (denominator << BigInt(ratioExponent)) : (sum << BigInt(-ratioExponent)) < denominator) ratioExponent--;
  let resultExponent = exponent + ratioExponent;
  const shift = resultExponent < -1022 ? exponent + 1074 : 52 - ratioExponent;
  let mantissa = shift >= 0 ? roundQuotient(sum << BigInt(shift), denominator) : roundQuotient(sum, denominator << BigInt(-shift));
  let bits;
  if (resultExponent < -1022) bits = mantissa;
  else {
    if (mantissa >= (1n << 53n)) { mantissa >>= 1n; resultExponent++; }
    bits = (BigInt(resultExponent + 1023) << 52n) | (mantissa - (1n << 52n));
  }
  if (negative) bits |= 1n << 63n;
  floatView.setBigUint64(0, bits);
  return floatView.getFloat64(0);
}

export function analyze(dataset, rules = defaultRules(), quality = null, {maxEvents = MAX_EVENTS} = {}) {
  if (!Number.isInteger(maxEvents) || maxEvents < 1 || maxEvents > MAX_EVENTS) throw new DatasetError('Invalid analysis event limit.');
  const selected = validateRules(rules === null ? defaultRules() : rules), qualityConfig = validateQuality(quality), samples = validateSamples(dataset);
  const channels = sensorChannels({...dataset, samples});
  for (const mapping of [qualityConfig.bounds, qualityConfig.stuck_s, qualityConfig.stuck_epsilon, qualityConfig.max_rate]) {
    if (Object.keys(mapping).some(name => !SENSOR_FIELDS.includes(name) && !channels.includes(name))) throw new RulesError('Quality configuration references a channel absent from the dataset.');
  }
  const dispatched = selected.flatMap(rule => ruleFields(rule, channels).map(field => [rule, field]));
  if (dispatched.length > MAX_RULE_CHANNEL_EVALUATIONS) throw new RulesError('Analysis exceeds the 256-rule-channel evaluation limit.');
  const stats = {};
  let invalidCount = 0;
  for (const field of channels) {
    const values = samples.map(sample => reading(sample, field)).filter(value => value !== null), invalid = samples.length - values.length;
    invalidCount += invalid;
    stats[field] = {valid_count: values.length, invalid_count: invalid,
      min: values.length ? Math.min(...values) : null, max: values.length ? Math.max(...values) : null, mean: exactMean(values)};
  }
  const events = sensorEvents(samples, channels, maxEvents);
  function extend(additions) {
    if (events.length + additions.length > maxEvents) throw new DatasetError(`Analysis exceeds the ${maxEvents}-event limit.`);
    events.push(...additions);
  }
  extend(gapEvents(samples, Math.min(...selected.map(rule => rule.max_gap_s)), Math.max(1, maxEvents - events.length)));
  extend(qualityEvents(samples, channels, qualityConfig, Math.max(1, maxEvents - events.length)));
  for (const [rule, field] of dispatched) extend(ruleEvents(samples, rule, field, channels, dataset.current_convention, Math.max(1, maxEvents - events.length)));
  events.sort((a, b) => a.start_s - b.start_s || a.confirmed_s - b.confirmed_s || compareText(a.field, b.field) || compareText(a.code, b.code) || compareText(a.rule_id, b.rule_id) || a.row_start - b.row_start);
  for (let index = 0; index < events.length; index++) {
    const value = events[index], identifiers = channelMetadata(value.field);
    Object.assign(value, {evidence_id: `E${String(index + 1).padStart(3, '0')}`, pack_id: dataset.pack_id,
      channel_id: identifiers.channel_id, cell_id: identifiers.cell_id, probe_id: identifiers.probe_id});
  }
  const units = {cell_voltage_v: 'V', current_a: 'A', temperature_c: 'degC'};
  const phaseCounts = {charge: 0, discharge: 0, idle: 0, unknown: 0};
  for (const sample of samples) {
    const current = sample.current_a, phase = current === null ? 'unknown' : current === 0 ? 'idle' :
      (current > 0) === (dataset.current_convention === 'positive_discharge') ? 'discharge' : 'charge';
    phaseCounts[phase]++;
  }
  const evaluatedIds = new Set(dispatched.map(([rule]) => rule.rule_id));
  return {summary: {sample_count: samples.length, start_s: samples[0].timestamp_s, end_s: samples.at(-1).timestamp_s,
    duration_s: samples.at(-1).timestamp_s - samples[0].timestamp_s, stats, pack_id: dataset.pack_id,
    current_convention: dataset.current_convention, phase_sample_counts: phaseCounts,
    channels: Object.fromEntries(channels.map(name => [name, {...channelMetadata(name), unit: units[channelMetadata(name).kind], stats: stats[name]}])),
    not_evaluated_rules: selected.filter(rule => !evaluatedIds.has(rule.rule_id)).map(rule => rule.rule_id), quality_issue_count: invalidCount,
    quality_event_count: events.filter(value => QUALITY_CODES.has(value.code)).length,
    fault_event_count: events.filter(value => !QUALITY_CODES.has(value.code)).length,
    active_fault_count: events.filter(value => !QUALITY_CODES.has(value.code) && value.status === 'active').length},
    rules: selected, quality_configuration: qualityConfig, events};
}
