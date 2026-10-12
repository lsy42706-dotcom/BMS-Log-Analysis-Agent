"""Bounded, deterministic tools."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from .conclusions import canonical_event_fact, event_aggregates, validate_conclusion
from .domain import Dataset, Rule, Sample, analyze


def number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def audit_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return "[NONFINITE]"
    if isinstance(value, dict):
        return {str(key): audit_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [audit_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return "[UNSUPPORTED]"


@dataclass
class Toolbox:
    dataset: Dataset
    rules: list[Rule]
    max_calls: int = 20
    calls: list[dict[str, Any]] = field(default_factory=list)
    issued_evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    quality: Any = None
    cached_analysis: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.analysis = self.cached_analysis if self.cached_analysis is not None else (
            analyze(self.dataset, self.rules, quality=self.quality) if self.quality is not None
            else analyze(self.dataset, self.rules))
        self._events = {item["evidence_id"]: item for item in self.analysis["events"]}
        self.analysis["event_aggregates"] = event_aggregates(self.analysis["events"])
        self._allowed = {
            "summarize_log": self._summary,
            "list_protection_rules": self._rules,
            "detect_faults": self._faults,
            "inspect_window": self._window,
            "get_evidence": self._evidence,
            "simulate_rule": self._simulate,
        }

    def invoke(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        if len(self.calls) >= self.max_calls:
            raise ValueError("Tool call budget exhausted")
        entry: dict[str, Any] = {"tool": name, "arguments": audit_value(arguments), "status": "running"}
        self.calls.append(entry)
        try:
            if name not in self._allowed:
                raise ValueError("Unknown tool")
            result = self._allowed[name](**arguments)
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(encoded) > 32_000:
                raise ValueError("Tool response too large; request a smaller page or window")
            entry.update(status="complete", result=result)
            if name in {"detect_faults", "get_evidence"}:
                items = result.get("events", [])
                if "event" in result:
                    items = [result["event"]]
                for item in items:
                    self.issued_evidence[item["evidence_id"]] = item
            return result
        except (ValueError, TypeError, KeyError) as exc:
            entry.update(status="failed", error=type(exc).__name__)
            raise ValueError("Invalid tool arguments or tool budget exhausted") from None

    def _summary(self) -> dict[str, Any]:
        summary = dict(self.analysis["summary"])
        channels = summary.get("channels", {})
        names = sorted(summary["stats"])
        summary["stats"] = {name: summary["stats"][name] for name in names[:12]}
        summary["stats_total_channels"] = len(names)
        summary["stats_returned_channels"] = min(12, len(names))
        summary["stats_truncated"] = len(names) > 12
        summary["channels"] = {name: {key: value for key, value in info.items() if key != "stats"} for name, info in channels.items()}
        return {
            "source_name": self.dataset.source_name,
            "source_sha256": self.dataset.sha256,
            **summary,
            "event_count": len(self.analysis["events"]),
            "event_aggregate_summary": self._aggregate_summary(self.analysis["events"]),
            "event_groups": self.analysis["event_aggregates"][:12],
            "event_groups_returned": min(12, len(self.analysis["event_aggregates"])),
            "event_groups_total": len(self.analysis["event_aggregates"]),
            "event_groups_truncated": len(self.analysis["event_aggregates"]) > 12,
            "units": {"timestamp_s": "s", **{name: info["unit"] for name, info in self.analysis["summary"].get("channels", {}).items()}},
            "current_convention": getattr(self.dataset, "current_convention", "positive_discharge"),
        }

    def _rules(self, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        rules = self.analysis["rules"]
        integer(offset, "offset", 0, len(rules))
        integer(limit, "limit", 1, 20)
        page = rules[offset:offset + limit]
        return {"scope": "illustrative software analysis, not calibrated battery protection", "rules": page,
                "total": len(rules), "next_offset": offset + len(page) if offset + len(page) < len(rules) else None,
                "quality_configuration": self.analysis.get("quality_configuration"),
                "not_evaluated_rules": self.analysis["summary"].get("not_evaluated_rules", [])}

    @staticmethod
    def _aggregate_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "total_events": len(events),
            "by_code": dict(sorted(Counter(event["code"] for event in events).items())),
            "by_status": dict(sorted(Counter(event["status"] for event in events).items())),
            "scan_scope": "entire_selected_log",
            "individual_evidence_coverage": "Counts are deterministic full-scope aggregates, not model inspection of each event.",
        }

    def _faults(self, code: str = "all", offset: int = 0, limit: int = 20) -> dict[str, Any]:
        integer(limit, "limit", 1, 40)
        if not isinstance(code, str) or len(code) > 64:
            raise ValueError("Invalid fault code")
        events = self.analysis["events"]
        if code != "all":
            codes = {event["code"] for event in events} | {rule.code for rule in self.rules}
            if code not in codes:
                raise ValueError("Unknown fault code")
            events = [event for event in events if event["code"] == code]
        integer(offset, "offset", 0, len(events))
        page = events[offset : offset + limit]
        return {
            "total": len(events), "events": page,
            "facts": [canonical_event_fact(event) for event in page],
            "next_offset": offset + len(page) if offset + len(page) < len(events) else None,
            "aggregate_summary": self._aggregate_summary(events),
            "filter_code": code,
        }

    def _window(self, start_s: float, end_s: float, max_samples: int = 20) -> dict[str, Any]:
        start_s, end_s = number(start_s, "start_s"), number(end_s, "end_s")
        integer(max_samples, "max_samples", 1, 50)
        if start_s > end_s:
            raise ValueError("Window is reversed")
        selected = [sample for sample in self.dataset.samples if start_s <= sample.timestamp_s <= end_s]
        from . import domain
        readings = getattr(domain, "sample_readings", lambda sample: {
            name: getattr(sample, name) for name in domain.SENSOR_FIELDS
        })
        critical: list[int] = []
        reasons: dict[int, set[str]] = {}
        def add(index: int, reason: str) -> None:
            if index not in reasons:
                critical.append(index)
                reasons[index] = set()
            reasons[index].add(reason)
        if selected:
            add(0, "first")
            add(len(selected) - 1, "last")
            values = [readings(sample) for sample in selected]
            channels = sorted({name for value in values for name in value})
            for name in channels:
                valid = [(index, value[name]) for index, value in enumerate(values) if value.get(name) is not None]
                if valid:
                    add(min(valid, key=lambda item: item[1])[0], f"{name}:min")
                    add(max(valid, key=lambda item: item[1])[0], f"{name}:max")
            positions = {sample.timestamp_s: index for index, sample in enumerate(selected)}
            for event in self.analysis["events"]:
                for key in ("start_s", "confirmed_s", "recovered_s"):
                    if event[key] in positions:
                        add(positions[event[key]], f"event:{key}")
        chosen = set(critical[:max_samples])
        if len(chosen) < min(max_samples, len(selected)):
            while len(chosen) < min(max_samples, len(selected)):
                candidate = max((index for index in range(len(selected)) if index not in chosen),
                                key=lambda index: (min(abs(index - present) for present in chosen), -index))
                chosen.add(candidate)
        shown = [selected[index] for index in sorted(chosen)]
        missing = [index for index in critical if index not in chosen]
        return {
            "matched_samples": len(selected), "returned_samples": len(shown),
            "downsampled": len(shown) < len(selected), "samples": [asdict(sample) for sample in shown],
            "sampling": {"method": "endpoints_extrema_event_boundaries_then_gap_fill",
                         "critical_points_total": len(critical), "critical_points_retained": len(critical) - len(missing),
                         "critical_points_complete": not missing, "critical_points_missing": len(missing),
                         "missing_point_examples": [{"timestamp_s": selected[index].timestamp_s,
                                                     "reasons": sorted(reasons[index])} for index in missing[:5]],
                         "not_a_full_resolution_waveform": len(shown) < len(selected)},
        }

    def _evidence(self, evidence_id: str) -> dict[str, Any]:
        if not isinstance(evidence_id, str) or evidence_id not in self._events:
            raise ValueError("Unknown evidence ID")
        event = self._events[evidence_id]
        return {"event": event, "fact": canonical_event_fact(event), "source_sha256": self.dataset.sha256}

    def _simulate(self, rule_id: str, values: list[float], step_s: float = 1.0) -> dict[str, Any]:
        if not isinstance(values, list) or not 1 <= len(values) <= 128:
            raise ValueError("Simulation requires 1..128 samples")
        step_s = number(step_s, "step_s")
        if not 0.01 <= step_s <= 10.0:
            raise ValueError("Invalid simulation step")
        rule = next((item for item in self.rules if item.rule_id == rule_id), None)
        if rule is None:
            raise ValueError("Unknown rule ID")
        clean = [number(value, "value") for value in values]
        if rule.field in {"cell_spread_v", "temperature_spread_c"} and any(value < 0 for value in clean):
            raise ValueError("Synthetic spread values must be nonnegative")
        samples = []
        for i, value in enumerate(clean):
            fields = {"cell_voltage_v": 3.7, "current_a": 10.0, "temperature_c": 25.0}
            cells, probes = {}, {}
            if rule.field == "cell_spread_v":
                fields["cell_voltage_v"] = None
                cells = {"C01": 3.7, "C02": 3.7 + value}
            elif rule.field == "temperature_spread_c":
                fields["temperature_c"] = None
                probes = {"T01": 25.0, "T02": 25.0 + value}
            elif rule.channel_id is not None or rule.field not in fields:
                from .domain import channel_metadata
                metadata = channel_metadata(rule.field)
                kind, channel_id = metadata["kind"], rule.channel_id or metadata["channel_id"]
                fields[kind] = None
                (cells if kind == "cell_voltage_v" else probes)[channel_id] = value
            else:
                fields[rule.field] = value
            samples.append(Sample(timestamp_s=i * step_s, row_number=i + 2, **fields,
                                  cell_voltages_v=cells, temperatures_c=probes))
        digest = hashlib.sha256(json.dumps(clean).encode()).hexdigest()
        synthetic = Dataset(samples=samples, quality_issues=[], source_name="synthetic_rule_probe", sha256=digest,
                            current_convention=getattr(self.dataset, "current_convention", "positive_discharge"))
        result = analyze(synthetic, [rule])
        events = [{**event, "evidence_id": "SIM-" + event["evidence_id"]} for event in result["events"]]
        return {"synthetic": True, "rule_id": rule_id, "events": events, "note": "Synthetic rule probe is not evidence of a fault in the input log."}
