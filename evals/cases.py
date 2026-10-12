"""Load versioned, manual labels independently of the analysis implementation."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EVENT_FIELDS = {"code", "rule_id", "field", "channel_id", "start_s", "confirmed_s", "recovered_s", "peak_value", "status"}


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    case_version: str
    csv_path: Path
    csv_sha256: str
    description: str
    time_tolerance_s: float
    expected_events: list[dict[str, Any]]
    config: dict[str, Any]


def load_cases(manifest: Path | None = None) -> tuple[dict[str, Any], list[EvalCase]]:
    manifest = Path(manifest or FIXTURES / "cases.json").resolve()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("Evaluation manifest requires versioned cases")
    if not all(isinstance(payload.get(name), str) and payload[name] for name in ("suite_id", "suite_version", "label_origin", "scope")):
        raise ValueError("Evaluation suite metadata is missing")
    seen, cases = set(), []
    for item in payload["cases"]:
        if not isinstance(item, dict) or not all(isinstance(item.get(name), str) and item[name] for name in ("case_id", "case_version", "csv", "description")):
            raise ValueError("Each case requires an ID, version, CSV and description")
        if item["case_id"] in seen:
            raise ValueError("Duplicate evaluation case ID")
        seen.add(item["case_id"])
        path = (manifest.parent / item["csv"]).resolve()
        if path.parent != manifest.parent or path.suffix != ".csv":
            raise ValueError("Case CSV must be a direct fixture file")
        tolerance = item.get("time_tolerance_s")
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("Case timing tolerance must be finite and nonnegative")
        expected = item.get("expected_events")
        if not isinstance(expected, list):
            raise ValueError("Case requires independent expected events")
        for event in expected:
            if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
                raise ValueError("Expected event fields do not match the evaluation contract")
            if event["status"] not in ("active", "recovered"):
                raise ValueError("Invalid expected status")
            for key in ("start_s", "confirmed_s", "recovered_s", "peak_value"):
                value = event[key]
                if value is None and key in ("recovered_s", "peak_value"):
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError("Expected numerical facts must be finite")
        config = {name: value for name, value in item.items() if name not in {"expected_events", "case_id", "case_version", "csv", "description", "time_tolerance_s"}}
        cases.append(EvalCase(item["case_id"], item["case_version"], path, hashlib.sha256(path.read_bytes()).hexdigest(), item["description"], float(tolerance), expected, config))
    if not cases:
        raise ValueError("Evaluation suite cannot be empty")
    metadata = {name: value for name, value in payload.items() if name != "cases"}
    metadata["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    return metadata, cases
