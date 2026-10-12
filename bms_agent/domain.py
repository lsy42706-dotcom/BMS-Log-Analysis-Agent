"""Bounded, deterministic offline analysis of demonstration BMS logs.

These rules are illustrative software examples, not validated battery limits.
The module neither operates hardware nor calls a language model.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import asdict, dataclass, field as dataclass_field
from pathlib import Path


SENSOR_FIELDS = ("cell_voltage_v", "current_a", "temperature_c")
CSV_FIELDS = ("timestamp_s", *SENSOR_FIELDS)
MAX_BYTES = 5 * 1024 * 1024
MAX_SAMPLES = 20_000
MAX_SENSOR_CHANNELS = 64
MAX_RULE_CHANNEL_EVALUATIONS = 256
MAX_EVENTS = 100_000
AGGREGATE_FIELDS = ("cell_spread_v", "temperature_spread_c")
QUALITY_CODES = {"SENSOR_INVALID", "SAMPLE_GAP", "SENSOR_IMPLAUSIBLE", "SENSOR_STUCK", "SENSOR_SPIKE"}
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_CHANNEL = re.compile(r"(cell_voltage_v|temperature_c)_([A-Za-z][A-Za-z0-9_]{0,31})\Z")


class DatasetError(ValueError):
    """Invalid input, with messages that do not echo untrusted CSV values."""


class RulesError(ValueError):
    """Invalid rule configuration, with safe diagnostic messages."""


@dataclass(frozen=True)
class Sample:
    timestamp_s: float
    cell_voltage_v: float | None
    current_a: float | None
    temperature_c: float | None
    row_number: int
    cell_voltages_v: dict[str, float | None] = dataclass_field(default_factory=dict)
    temperatures_c: dict[str, float | None] = dataclass_field(default_factory=dict)


@dataclass
class Dataset:
    samples: list[Sample]
    quality_issues: list[dict]
    source_name: str
    sha256: str
    pack_id: str = "pack_1"
    current_convention: str = "positive_discharge"
    import_metadata: dict = dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    code: str
    field: str
    direction: str
    threshold: float
    recovery_threshold: float
    confirm_s: float
    recovery_s: float
    max_gap_s: float = 2.0
    channel_id: str | None = None
    current_mode: str = "any"


@dataclass(frozen=True)
class QualityConfig:
    """Opt-in diagnostic heuristics, not proof of a physical sensor failure.

    Maps use a generic sensor field or a canonical channel name. Channel-specific
    values override generic values. Bounds retain measurements as evidence;
    they do not silently replace out-of-range readings or recalibrate them.
    """

    bounds: dict[str, tuple[float, float]] = dataclass_field(default_factory=dict)
    stuck_s: dict[str, float] = dataclass_field(default_factory=dict)
    stuck_epsilon: dict[str, float] = dataclass_field(default_factory=dict)
    max_rate: dict[str, float] = dataclass_field(default_factory=dict)
    max_gap_s: float = 2.0


def channel_metadata(field: str) -> dict:
    """Return bounded identifiers for a canonical sensor field."""
    match = _CHANNEL.fullmatch(field)
    generic, channel_id = match.groups() if match else (field, None)
    return {
        "field": field,
        "kind": generic,
        "channel_id": channel_id,
        "cell_id": channel_id or "legacy_cell" if generic == "cell_voltage_v" else None,
        "probe_id": channel_id or "legacy_probe" if generic == "temperature_c" else None,
    }


def _is_sensor_field(field: object) -> bool:
    return isinstance(field, str) and (field in SENSOR_FIELDS or _CHANNEL.fullmatch(field) is not None)


def sample_readings(sample: Sample) -> dict[str, float | None]:
    """Flatten actual scalar/wide sensor channels; no aggregate or invented alias."""
    readings = {"current_a": sample.current_a}
    if not sample.cell_voltages_v or sample.cell_voltage_v is not None:
        readings["cell_voltage_v"] = sample.cell_voltage_v
    if not sample.temperatures_c or sample.temperature_c is not None:
        readings["temperature_c"] = sample.temperature_c
    readings.update({f"cell_voltage_v_{key}": value for key, value in sample.cell_voltages_v.items()})
    readings.update({f"temperature_c_{key}": value for key, value in sample.temperatures_c.items()})
    return readings


def sample_channels(sample: Sample) -> list[str]:
    """Return canonical physical sensor names present in a sample."""
    return sorted(sample_readings(sample))


def sensor_channels(dataset: Dataset) -> list[str]:
    """Return configured physical sensor names, including entirely missing columns."""
    channels = set()
    for sample in dataset.samples:
        channels.update(sample_channels(sample))
    if isinstance(dataset.import_metadata, dict):
        configured = dataset.import_metadata.get("channels", [])
        if isinstance(configured, list):
            channels.update(name for name in configured if _is_sensor_field(name))
    return sorted(channels)


def _reading(sample: Sample, field: str, channels: list[str] | None = None) -> float | None:
    if field in SENSOR_FIELDS:
        return getattr(sample, field)
    match = _CHANNEL.fullmatch(field)
    if match:
        generic, channel_id = match.groups()
        mapping = sample.cell_voltages_v if generic == "cell_voltage_v" else sample.temperatures_c
        return mapping.get(channel_id)
    if field in AGGREGATE_FIELDS:
        generic = "cell_voltage_v" if field == "cell_spread_v" else "temperature_c"
        relevant = [name for name in channels or [] if channel_metadata(name)["kind"] == generic]
        values = [_reading(sample, name) for name in relevant]
        if len(values) < 2 or any(value is None for value in values):
            return None
        spread = max(values) - min(values)
        return spread if math.isfinite(spread) else None
    return None


def default_rules() -> list[Rule]:
    """Return demonstration thresholds; do not use as real battery calibration."""
    return [
        Rule("overcurrent", "OVERCURRENT", "current_a", "abs_high", 50.0, 45.0, 2.0, 2.0),
        Rule("overtemperature", "OVERTEMPERATURE", "temperature_c", "high", 60.0, 55.0, 3.0, 2.0),
        Rule("undervoltage", "UNDERVOLTAGE", "cell_voltage_v", "low", 3.0, 3.2, 2.0, 2.0),
        Rule("overvoltage", "OVERVOLTAGE", "cell_voltage_v", "high", 4.25, 4.15, 2.0, 2.0),
    ]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _quality_issue(row: int, timestamp: float, field: str) -> dict:
    return {
        "code": "SENSOR_INVALID",
        "row_number": row,
        "timestamp_s": timestamp,
        "field": field,
        "message": f"Row {row}: {field} is missing, nonnumeric, or nonfinite.",
    }


def load_csv(path: Path, column_map: dict[str, str] | None = None,
             units: dict[str, str] | None = None, *,
             current_convention: str = "positive_discharge", pack_id: str = "pack_1") -> Dataset:
    """Import a bounded local CSV through the same parser used by uploaded bytes."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_BYTES:
            raise DatasetError("CSV exceeds the 5 MiB size limit.")
        content = path.read_bytes()
    except OSError as exc:
        raise DatasetError("CSV could not be read.") from exc
    return load_csv_bytes(content, path.name, column_map, units,
                          current_convention=current_convention, pack_id=pack_id)


def load_csv_bytes(content: bytes, source_name: str = "upload.csv",
                   column_map: dict[str, str] | None = None,
                   units: dict[str, str] | None = None, *,
                   current_convention: str = "positive_discharge", pack_id: str = "pack_1") -> Dataset:
    """Import bytes without accessing paths; column_map is canonical -> source."""
    from .importer import parse_csv_bytes

    return parse_csv_bytes(content, source_name, column_map, units,
                           current_convention=current_convention, pack_id=pack_id)


def _validate_rules(rules: list[Rule]) -> list[Rule]:
    if not isinstance(rules, list) or not rules or len(rules) > 100:
        raise RulesError("Rules must be a nonempty list of at most 100 rules.")
    identifiers: set[str] = set()
    normalized: list[Rule] = []
    for index, rule in enumerate(rules, start=1):
        prefix = f"Rule {index}"
        if not isinstance(rule, Rule):
            raise RulesError(f"{prefix}: invalid rule structure.")
        for key in ("rule_id", "code"):
            value = getattr(rule, key)
            if not isinstance(value, str) or not _TOKEN.fullmatch(value):
                raise RulesError(f"{prefix}: {key} must be a short ASCII identifier.")
        if rule.rule_id in identifiers:
            raise RulesError(f"{prefix}: rule_id must be unique.")
        identifiers.add(rule.rule_id)
        if not _is_sensor_field(rule.field) and rule.field not in AGGREGATE_FIELDS:
            raise RulesError(f"{prefix}: unsupported sensor field.")
        if rule.channel_id is not None:
            if (not isinstance(rule.channel_id, str) or
                    not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", rule.channel_id) or
                    rule.field not in ("cell_voltage_v", "temperature_c")):
                raise RulesError(f"{prefix}: channel_id requires a generic voltage or temperature field.")
        if rule.current_mode not in ("any", "charge", "discharge"):
            raise RulesError(f"{prefix}: current_mode must be any, charge, or discharge.")
        if rule.current_mode != "any" and (rule.field != "current_a" or rule.direction != "abs_high"):
            raise RulesError(f"{prefix}: directional current rules require current_a and abs_high.")
        if rule.direction not in ("high", "low", "abs_high"):
            raise RulesError(f"{prefix}: direction must be high, low, or abs_high.")
        numbers = {}
        for field in ("threshold", "recovery_threshold", "confirm_s", "recovery_s", "max_gap_s"):
            value = _number(getattr(rule, field))
            if value is None:
                raise RulesError(f"{prefix}: {field} must be finite and numeric.")
            numbers[field] = value
        if numbers["confirm_s"] < 0 or numbers["recovery_s"] < 0 or numbers["max_gap_s"] <= 0:
            raise RulesError(f"{prefix}: delays must be nonnegative and max_gap_s must be positive.")
        if rule.direction == "low":
            valid_hysteresis = numbers["recovery_threshold"] >= numbers["threshold"]
        else:
            valid_hysteresis = numbers["recovery_threshold"] <= numbers["threshold"]
        if not valid_hysteresis:
            raise RulesError(f"{prefix}: recovery_threshold must provide valid hysteresis.")
        if rule.direction == "abs_high" and (numbers["threshold"] < 0 or numbers["recovery_threshold"] < 0):
            raise RulesError(f"{prefix}: absolute-current thresholds must be nonnegative.")
        normalized.append(Rule(rule.rule_id, rule.code, rule.field, rule.direction, **numbers,
                               channel_id=rule.channel_id, current_mode=rule.current_mode))
    return normalized


def _load_profile_payload(path: Path) -> object:
    try:
        if Path(path).stat().st_size > 1024 * 1024:
            raise RulesError("Rule configuration exceeds the 1 MiB size limit.")
        content = Path(path).read_bytes()
        if len(content) > 1024 * 1024:
            raise RulesError("Rule configuration exceeds the 1 MiB size limit.")
        payload = json.loads(content.decode("utf-8-sig"))
    except OSError as exc:
        raise RulesError("Rule configuration could not be read.") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RulesError("Rule configuration must be valid UTF-8 JSON.") from exc
    return payload


def _validate_quality(config: QualityConfig | None) -> QualityConfig:
    if config is None:
        return QualityConfig()
    if not isinstance(config, QualityConfig):
        raise RulesError("Quality configuration must be an object.")
    normalized = {}
    for key in ("bounds", "stuck_s", "stuck_epsilon", "max_rate"):
        mapping = getattr(config, key)
        if not isinstance(mapping, dict) or len(mapping) > MAX_SENSOR_CHANNELS:
            raise RulesError("Quality maps must contain at most 64 canonical sensor fields.")
        values = {}
        for name, value in mapping.items():
            if not _is_sensor_field(name):
                raise RulesError("Quality configuration contains an unsupported sensor field.")
            if key == "bounds":
                if not isinstance(value, (list, tuple)) or len(value) != 2:
                    raise RulesError("Quality bounds require finite minimum and maximum values.")
                lower, upper = (_number(item) for item in value)
                if lower is None or upper is None or lower > upper:
                    raise RulesError("Quality bounds require finite ordered minimum and maximum values.")
                values[name] = (lower, upper)
            else:
                number = _number(value)
                if number is None or number < 0 or (key != "stuck_epsilon" and number == 0):
                    raise RulesError("Quality durations and rates must be positive; epsilon must be nonnegative.")
                values[name] = number
        normalized[key] = values
    gap = _number(config.max_gap_s)
    if gap is None or gap <= 0:
        raise RulesError("Quality max_gap_s must be finite and positive.")
    return QualityConfig(**normalized, max_gap_s=gap)


def profile_from_dict(payload: object) -> tuple[list[Rule], QualityConfig]:
    """Parse a JSON-compatible rules array/profile, keeping quality disabled by default."""
    if isinstance(payload, dict):
        if set(payload) - {"rules", "quality", "description"}:
            raise RulesError("Profile contains unsupported top-level fields.")
        if "description" in payload and (not isinstance(payload["description"], str) or len(payload["description"]) > 1000):
            raise RulesError("Profile description must be bounded text.")
    quality_payload = payload.get("quality", {}) if isinstance(payload, dict) else {}
    rules_payload = payload.get("rules") if isinstance(payload, dict) else payload
    if not isinstance(rules_payload, list) or not rules_payload or len(rules_payload) > 100:
        raise RulesError("Rules must be a nonempty list of at most 100 rules.")
    rules = []
    for index, item in enumerate(rules_payload, start=1):
        if not isinstance(item, dict):
            raise RulesError(f"Rule {index}: invalid rule structure.")
        try:
            rules.append(Rule(**item))
        except TypeError as exc:
            raise RulesError(f"Rule {index}: missing or unsupported rule fields.") from exc
    if not isinstance(quality_payload, dict):
        raise RulesError("Quality configuration must be an object.")
    try:
        quality = QualityConfig(**quality_payload)
    except TypeError as exc:
        raise RulesError("Quality configuration contains unsupported fields.") from exc
    return _validate_rules(rules), _validate_quality(quality)


def load_profile(path: Path | None = None) -> tuple[list[Rule], QualityConfig]:
    """Load illustrative rules and optional diagnostic quality heuristics."""
    if path is None:
        return default_rules(), QualityConfig()
    return profile_from_dict(_load_profile_payload(path))


def load_rules(path: Path | None = None) -> list[Rule]:
    """Backward-compatible rule-only loader; callers opt into quality via load_profile."""
    if path is None:
        return default_rules()
    payload = _load_profile_payload(path)
    return profile_from_dict(payload.get("rules") if isinstance(payload, dict) else payload)[0]


def _validate_samples(dataset: Dataset) -> list[Sample]:
    if not dataset.samples or len(dataset.samples) > MAX_SAMPLES:
        raise DatasetError("Dataset must contain between 1 and 20000 samples.")
    samples = []
    configured = dataset.import_metadata.get("channels", []) if isinstance(dataset.import_metadata, dict) else []
    seen_channels = {name for name in configured if _is_sensor_field(name)} if isinstance(configured, list) else set()
    if len(seen_channels) > MAX_SENSOR_CHANNELS:
        raise DatasetError("Dataset exceeds the 64-sensor-channel limit.")
    if not isinstance(dataset.pack_id, str) or not _TOKEN.fullmatch(dataset.pack_id):
        raise DatasetError("Pack identifier must be a short ASCII identifier.")
    if dataset.current_convention not in ("positive_discharge", "positive_charge"):
        raise DatasetError("Current convention must be positive_discharge or positive_charge.")
    previous = None
    first = None
    for sample in dataset.samples:
        if not isinstance(sample, Sample):
            raise DatasetError("Dataset contains an invalid sample structure.")
        if isinstance(sample.row_number, bool) or not isinstance(sample.row_number, int) or sample.row_number < 1:
            raise DatasetError("Sample row_number must be a positive integer.")
        timestamp = _number(sample.timestamp_s)
        if timestamp is None:
            raise DatasetError(f"Row {sample.row_number}: timestamp_s must be finite and numeric.")
        if previous is not None and timestamp <= previous:
            raise DatasetError(f"Row {sample.row_number}: timestamp_s must strictly increase.")
        if first is None:
            first = timestamp
        if not math.isfinite(timestamp - first):
            raise DatasetError(f"Row {sample.row_number}: timestamp_s range is too large.")
        mappings = []
        for mapping in (sample.cell_voltages_v, sample.temperatures_c):
            if not isinstance(mapping, dict):
                raise DatasetError("Sample channel readings must be objects.")
            if len(mapping) > MAX_SENSOR_CHANNELS:
                raise DatasetError("Dataset exceeds the 64-sensor-channel limit.")
            if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", key) for key in mapping):
                raise DatasetError("Channel identifiers must be short ASCII identifiers.")
            mappings.append({key: _number(value) for key, value in mapping.items()})
        normalized = Sample(timestamp, *(_number(getattr(sample, field)) for field in SENSOR_FIELDS),
                            sample.row_number, *mappings)
        seen_channels.update(sample_channels(normalized))
        if len(seen_channels) > MAX_SENSOR_CHANNELS:
            raise DatasetError("Dataset exceeds the 64-sensor-channel limit.")
        samples.append(normalized)
        previous = timestamp
    if len(sensor_channels(Dataset(samples, [], dataset.source_name, dataset.sha256,
                                   import_metadata=dataset.import_metadata))) > MAX_SENSOR_CHANNELS:
        raise DatasetError("Dataset exceeds the 64-sensor-channel limit.")
    return samples


def _event(code: str, rule_id: str, field: str, start: Sample, confirmed: Sample,
           trigger_value: float | None, peak_value: float | None,
           severity: str, message: str) -> dict:
    return {
        "code": code,
        "rule_id": rule_id,
        "field": field,
        "start_s": start.timestamp_s,
        "confirmed_s": confirmed.timestamp_s,
        "recovered_s": None,
        "status": "active",
        "trigger_value": trigger_value,
        "peak_value": peak_value,
        "row_start": start.row_number,
        "row_confirm": confirmed.row_number,
        "row_recover": None,
        "severity": severity,
        "message": message,
    }


def _recover(event: dict, sample: Sample) -> None:
    event.update(recovered_s=sample.timestamp_s, row_recover=sample.row_number, status="recovered")


def _stronger(rule: Rule, current: float, candidate: float) -> float:
    if rule.direction == "low":
        return min(current, candidate)
    if rule.direction == "abs_high":
        return candidate if abs(candidate) > abs(current) else current
    return max(current, candidate)


def _matches(rule: Rule, value: float, recovering: bool = False) -> bool:
    if rule.direction == "abs_high":
        value = abs(value)
    threshold = rule.recovery_threshold if recovering else rule.threshold
    if rule.direction == "low":
        return value >= threshold if recovering else value <= threshold
    return value <= threshold if recovering else value >= threshold


def _rule_events(samples: list[Sample], rule: Rule, field: str | None = None,
                 channels: list[str] | None = None,
                 current_convention: str = "positive_discharge") -> list[dict]:
    field = rule.field if field is None else field
    events: list[dict] = []
    pending: Sample | None = None
    pending_peak: float | None = None
    recovering: Sample | None = None
    active: dict | None = None
    previous: Sample | None = None
    for sample in samples:
        value = _reading(sample, field, channels)
        mode_matches = True
        if value is not None and rule.current_mode != "any":
            discharge = value > 0 if current_convention == "positive_discharge" else value < 0
            mode_matches = value != 0 and (discharge == (rule.current_mode == "discharge"))
        tested_value = value if mode_matches else 0.0
        interrupted = previous is not None and sample.timestamp_s - previous.timestamp_s > rule.max_gap_s
        previous = sample
        if interrupted:
            pending = None
            pending_peak = None
            recovering = None
        if value is None:
            pending = None
            pending_peak = None
            recovering = None
            continue
        if active is None:
            if not mode_matches or not _matches(rule, tested_value):
                pending = None
                pending_peak = None
                continue
            if pending is None:
                pending = sample
                pending_peak = value
            else:
                pending_peak = _stronger(rule, pending_peak, value)
            if sample.timestamp_s - pending.timestamp_s >= rule.confirm_s:
                active = _event(
                    rule.code, rule.rule_id, field, pending, sample,
                    _reading(pending, field, channels), pending_peak, "error",
                    "Demonstration protection rule confirmed by sampled elapsed time; not hardware validation.",
                )
                events.append(active)
                active["current_mode"] = rule.current_mode
                pending = None
                pending_peak = None
        if active is not None:
            if mode_matches:
                active["peak_value"] = _stronger(rule, active["peak_value"], value)
            if _matches(rule, tested_value, recovering=True):
                if recovering is None:
                    recovering = sample
                if sample.timestamp_s - recovering.timestamp_s >= rule.recovery_s:
                    _recover(active, sample)
                    active = None
                    recovering = None
            else:
                recovering = None
    return events


def _sensor_events(samples: list[Sample], channels: list[str] | None = None) -> list[dict]:
    events = []
    for field in SENSOR_FIELDS if channels is None else channels:
        active = None
        for sample in samples:
            if _reading(sample, field) is None:
                if active is None:
                    if len(events) >= MAX_EVENTS:
                        raise DatasetError("Analysis exceeds the 100000-event limit.")
                    active = _event(
                        "SENSOR_INVALID", f"sensor_{field}", field, sample, sample,
                        None, None, "warning",
                        "Missing, nonnumeric, or nonfinite sensor readings interrupt protection timing.",
                    )
                    events.append(active)
            elif active is not None:
                _recover(active, sample)
                active = None
        if len(events) > MAX_EVENTS:
            raise DatasetError("Analysis exceeds the 100000-event limit.")
    return events


def _gap_events(samples: list[Sample], max_gap_s: float) -> list[dict]:
    events = []
    for before, after in zip(samples, samples[1:]):
        gap = after.timestamp_s - before.timestamp_s
        if gap > max_gap_s:
            event = _event(
                "SAMPLE_GAP", "sample_gap", "timestamp_s", before, after,
                gap, gap, "warning",
                "Sampling interval exceeds a rule limit; continuous fault or recovery is not inferred across this gap.",
            )
            _recover(event, after)
            events.append(event)
    return events


def _quality_setting(mapping: dict, field: str, fallback=None):
    return mapping.get(field, mapping.get(channel_metadata(field)["kind"], fallback))


def _quality_events(samples: list[Sample], channels: list[str], config: QualityConfig) -> list[dict]:
    if not any((config.bounds, config.stuck_s, config.max_rate)):
        return []
    events = []
    for field in channels:
        bounds = _quality_setting(config.bounds, field)
        stuck_s = _quality_setting(config.stuck_s, field)
        epsilon = _quality_setting(config.stuck_epsilon, field, 0.0)
        max_rate = _quality_setting(config.max_rate, field)
        bounds_active = stuck_active = None
        anchor = previous = None
        anchor_value = previous_value = None
        for sample in samples:
            value = _reading(sample, field)
            interrupted = previous is not None and sample.timestamp_s - previous.timestamp_s > config.max_gap_s
            if value is None or interrupted:
                anchor = None
                anchor_value = None
            if value is None:
                previous = sample
                previous_value = None
                continue
            if bounds is not None:
                outside = value < bounds[0] or value > bounds[1]
                if outside and bounds_active is None:
                    bounds_active = _event(
                        "SENSOR_IMPLAUSIBLE", "quality_bounds", field, sample, sample, value, value,
                        "warning", "Reading is outside user-configured plausibility bounds; not a validated physical fault.",
                    )
                    events.append(bounds_active)
                elif not outside and bounds_active is not None:
                    _recover(bounds_active, sample)
                    bounds_active = None
                elif outside and bounds_active is not None:
                    # Keep the reading furthest outside the configured interval.
                    def distance(reading):
                        return max(bounds[0] - reading, reading - bounds[1], 0.0)
                    if distance(value) > distance(bounds_active["peak_value"]):
                        bounds_active["peak_value"] = value
            if stuck_s is not None:
                if stuck_active is not None and abs(value - stuck_active["trigger_value"]) > epsilon:
                    _recover(stuck_active, sample)
                    stuck_active = None
                    anchor = sample
                    anchor_value = value
                if anchor is None or abs(value - anchor_value) > epsilon:
                    anchor = sample
                    anchor_value = value
                if stuck_active is None and sample.timestamp_s - anchor.timestamp_s >= stuck_s:
                    stuck_active = _event(
                        "SENSOR_STUCK", "quality_stuck", field, anchor, sample,
                        anchor_value, value, "warning",
                        "Low-variation run exceeded a configured duration; stable real conditions can produce the same pattern.",
                    )
                    stuck_active["epsilon"] = epsilon
                    events.append(stuck_active)
            if max_rate is not None and previous is not None and previous_value is not None and not interrupted:
                elapsed = sample.timestamp_s - previous.timestamp_s
                rate = abs(value - previous_value) / elapsed
                if rate > max_rate:
                    spike = _event(
                        "SENSOR_SPIKE", "quality_spike", field, previous, sample, value, value,
                        "warning", "Observed sample-to-sample rate exceeded a configured limit; no interpolated waveform is inferred.",
                    )
                    spike["rate_per_s"] = rate if math.isfinite(rate) else None
                    spike["rate_exceeds_numeric_range"] = not math.isfinite(rate)
                    _recover(spike, sample)
                    events.append(spike)
            previous, previous_value = sample, value
        if len(events) > MAX_EVENTS:
            raise DatasetError("Analysis exceeds the 100000-event limit.")
    return events


def _rule_fields(rule: Rule, channels: list[str]) -> list[str]:
    if rule.field in AGGREGATE_FIELDS:
        generic = "cell_voltage_v" if rule.field == "cell_spread_v" else "temperature_c"
        return [rule.field] if sum(channel_metadata(name)["kind"] == generic for name in channels) >= 2 else []
    if rule.channel_id is not None:
        targeted = f"{rule.field}_{rule.channel_id}"
        if targeted not in channels:
            raise RulesError("A channel-specific rule references a channel absent from the dataset.")
        return [targeted]
    if rule.field in ("cell_voltage_v", "temperature_c"):
        return [name for name in channels if channel_metadata(name)["kind"] == rule.field]
    if rule.field not in channels:
        raise RulesError("A rule references a channel absent from the dataset.")
    return [rule.field]


def analyze(dataset: Dataset, rules: list[Rule] | None = None,
            quality: QualityConfig | None = None) -> dict:
    """Analyze timestamped readings, generating stable evidence identifiers.

    The result only establishes conditions at observed samples. Pending timers
    reset on missing readings or excessive sampling gaps. Confirmed faults stay
    latched through unknown intervals until sampled recovery is confirmed.
    """
    selected = _validate_rules(default_rules() if rules is None else rules)
    quality_config = _validate_quality(quality)
    samples = _validate_samples(dataset)
    normalized = Dataset(samples, [], dataset.source_name, dataset.sha256, dataset.pack_id,
                         dataset.current_convention, dataset.import_metadata)
    channels = sensor_channels(normalized)
    for mapping in (quality_config.bounds, quality_config.stuck_s, quality_config.stuck_epsilon, quality_config.max_rate):
        if any(name not in SENSOR_FIELDS and name not in channels for name in mapping):
            raise RulesError("Quality configuration references a channel absent from the dataset.")
    dispatched = [(rule, field) for rule in selected for field in _rule_fields(rule, channels)]
    if len(dispatched) > MAX_RULE_CHANNEL_EVALUATIONS:
        raise RulesError("Analysis exceeds the 256-rule-channel evaluation limit.")
    stats = {}
    invalid_count = 0
    for field in channels:
        values = [_reading(sample, field) for sample in samples if _reading(sample, field) is not None]
        invalid = len(samples) - len(values)
        invalid_count += invalid
        stats[field] = {
            "valid_count": len(values),
            "invalid_count": invalid,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "mean": statistics.mean(values) if values else None,
        }
    events = _sensor_events(samples, channels)
    def extend_bounded(additions):
        if len(events) + len(additions) > MAX_EVENTS:
            raise DatasetError("Analysis exceeds the 100000-event limit.")
        events.extend(additions)
    extend_bounded(_gap_events(samples, min(rule.max_gap_s for rule in selected)))
    extend_bounded(_quality_events(samples, channels, quality_config))
    for rule, field in dispatched:
        extend_bounded(_rule_events(samples, rule, field, channels, dataset.current_convention))
    events.sort(key=lambda event: (event["start_s"], event["confirmed_s"], event["field"], event["code"], event["rule_id"], event["row_start"]))
    for index, event in enumerate(events, start=1):
        event["evidence_id"] = f"E{index:03d}"
        identifiers = channel_metadata(event["field"])
        event.update(pack_id=dataset.pack_id, channel_id=identifiers["channel_id"],
                     cell_id=identifiers["cell_id"], probe_id=identifiers["probe_id"])
    units = {"cell_voltage_v": "V", "current_a": "A", "temperature_c": "degC"}
    phase_counts = {"charge": 0, "discharge": 0, "idle": 0, "unknown": 0}
    for sample in samples:
        current = sample.current_a
        phase = "unknown" if current is None else "idle" if current == 0 else (
            "discharge" if (current > 0) == (dataset.current_convention == "positive_discharge") else "charge")
        phase_counts[phase] += 1
    evaluated_ids = {rule.rule_id for rule, field in dispatched}
    return {
        "summary": {
            "sample_count": len(samples),
            "start_s": samples[0].timestamp_s,
            "end_s": samples[-1].timestamp_s,
            "duration_s": samples[-1].timestamp_s - samples[0].timestamp_s,
            "stats": stats,
            "pack_id": dataset.pack_id,
            "current_convention": dataset.current_convention,
            "phase_sample_counts": phase_counts,
            "channels": {name: {**channel_metadata(name), "unit": units[channel_metadata(name)["kind"]],
                                 "stats": stats[name]} for name in channels},
            "not_evaluated_rules": [rule.rule_id for rule in selected if rule.rule_id not in evaluated_ids],
            "quality_issue_count": invalid_count,
            "quality_event_count": sum(event["code"] in QUALITY_CODES for event in events),
            "fault_event_count": sum(event["code"] not in QUALITY_CODES for event in events),
            "active_fault_count": sum(event["code"] not in QUALITY_CODES and event["status"] == "active" for event in events),
        },
        "rules": [asdict(rule) for rule in selected],
        "quality_configuration": asdict(quality_config),
        "events": events,
    }
