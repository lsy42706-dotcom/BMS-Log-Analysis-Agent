"""Independent event matching plus required evidence/workflow guard graders."""

from __future__ import annotations

import copy
import json
import math
from typing import Any

from bms_agent.conclusions import canonical_event_fact, canonicalize_conclusion, validate_conclusion

IDENTITY_FIELDS = ("code", "rule_id", "field", "channel_id", "status")
TIME_FIELDS = ("start_s", "confirmed_s", "recovered_s")
REQUIRED_SEQUENCE = ("summarize_log", "list_protection_rules", "detect_faults")


def ratio(numerator: int, denominator: int) -> float | None:
    """Undefined metrics stay null, never become invented zero/100 percent."""
    return numerator / denominator if denominator else None


def _close(actual: Any, expected: Any, tolerance: float) -> bool:
    if actual is None or expected is None:
        return actual is expected
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    return math.isfinite(actual) and abs(actual - expected) <= tolerance


def match_events(actual: list[dict[str, Any]], expected: list[dict[str, Any]], tolerance_s: float) -> dict[str, Any]:
    """One-to-one maximum matching by type/rule/channel/status and all times.

    Peaks are separately checked against manual labels, so a matching detector
    cannot hide incorrect reported numerical evidence behind event recall.
    """
    adjacency = []
    for observed in actual:
        adjacency.append([
            index for index, label in enumerate(expected)
            if all(observed.get(key) == label[key] for key in IDENTITY_FIELDS)
            and all(_close(observed.get(key), label[key], tolerance_s) for key in TIME_FIELDS)
        ])
    expected_to_actual: dict[int, int] = {}

    def augment(actual_index: int, visited: set[int]) -> bool:
        for expected_index in adjacency[actual_index]:
            if expected_index in visited:
                continue
            visited.add(expected_index)
            previous = expected_to_actual.get(expected_index)
            if previous is None or augment(previous, visited):
                expected_to_actual[expected_index] = actual_index
                return True
        return False

    for index in range(len(actual)):
        augment(index, set())
    pairs = sorted((observed, label) for label, observed in expected_to_actual.items())
    tp = len(pairs)
    peak_pass = sum(_close(actual[observed].get("peak_value"), expected[label]["peak_value"], 1e-9) for observed, label in pairs)
    fp, fn = len(actual) - tp, len(expected) - tp
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
        "time_tolerance_s": tolerance_s, "peak_tolerance": 1e-9,
        "timing_matched_events": tp, "expected_events": len(expected), "actual_events": len(actual),
        "peak_checks_passed": peak_pass, "peak_checks_total": tp,
        "peak_fact_consistency": ratio(peak_pass, tp),
        "matched_pairs": [{"actual_index": observed, "expected_index": label} for observed, label in pairs],
        "unmatched_actual_indices": [index for index in range(len(actual)) if index not in {observed for observed, _ in pairs}],
        "unmatched_expected_indices": [index for index in range(len(expected)) if index not in expected_to_actual],
        "passed": fp == 0 and fn == 0 and peak_pass == tp,
    }


def model_payload(events: list[dict[str, Any]], coverage: str = "full") -> dict[str, Any]:
    """A tool-reflection baseline payload, not independently generated labels."""
    return {
        "summary": "UNVERIFIED_MOCK_PROSE",
        "findings": [{"code": event["code"], "evidence_ids": [event["evidence_id"]],
                      "facts": [canonical_event_fact(event)], "explanation": "UNVERIFIED_MOCK_PROSE"} for event in events],
        "limitations": ["UNVERIFIED_MOCK_PROSE"], "coverage": coverage,
    }


def grade_evidence(conclusion: dict[str, Any], toolbox: Any) -> dict[str, Any]:
    events = {event["evidence_id"]: event for event in toolbox.analysis["events"]}
    cited, fact_checks, valid_facts = [], 0, 0
    structure_valid = True
    for finding in conclusion.get("findings", []):
        ids, facts = finding.get("evidence_ids", []), finding.get("facts", [])
        fact_ids = [fact.get("event_id") for fact in facts]
        structure_valid = structure_valid and (
            bool(ids) and len(ids) == len(facts) and len(set(fact_ids)) == len(fact_ids)
            and set(ids) == set(fact_ids)
            and all(evidence_id in events and finding.get("code") == events[evidence_id]["code"] for evidence_id in ids)
        )
        cited.extend(ids)
        for fact in facts:
            fact_checks += 1
            event = events.get(fact.get("event_id"))
            if event is not None and event["evidence_id"] in toolbox.issued_evidence and canonical_event_fact(event) == fact:
                valid_facts += 1
    ids_valid = len(set(cited)) == len(cited) and all(evidence_id in events and evidence_id in toolbox.issued_evidence for evidence_id in cited)
    successful = [call["tool"] for call in toolbox.calls if call["status"] == "complete"]
    sequence_valid = tuple(successful[:3]) == REQUIRED_SEQUENCE
    coverage = conclusion.get("coverage", {})
    coverage_valid = (
        isinstance(coverage, dict) and coverage.get("total_events") == len(events)
        and coverage.get("issued_events") == len(set(events) & set(toolbox.issued_evidence))
        and coverage.get("cited_events") == len(set(cited))
        and coverage.get("remaining_events") == len(set(events) - set(cited))
        and coverage.get("source_sha256") == toolbox.dataset.sha256
        and coverage.get("mode") == ("full" if set(cited) == set(events) else "partial")
    )
    text_is_canonical = False
    try:
        verified = canonicalize_conclusion({"summary": "grader", "findings": conclusion.get("findings", []),
                                          "limitations": [], "coverage": coverage.get("mode")}, toolbox)
        text_is_canonical = all(conclusion.get(key) == verified[key] for key in ("summary", "findings", "limitations", "text_provenance"))
    except ValueError:
        pass
    return {
        "evidence_ids_valid": ids_valid,
        "finding_structure_valid": structure_valid,
        "fact_checks_passed": valid_facts, "fact_checks_total": fact_checks,
        "evidence_fact_consistency": ratio(valid_facts, fact_checks),
        "tool_sequence_valid": sequence_valid, "successful_tool_sequence": successful,
        "coverage_valid": coverage_valid, "citation_coverage": ratio(len(set(cited)), len(events)),
        "canonical_text_only": text_is_canonical,
        "passed": structure_valid and ids_valid and valid_facts == fact_checks and sequence_valid and coverage_valid and text_is_canonical,
    }


def grade_prompt_data(toolbox: Any, conclusion: dict[str, Any], markers: list[str]) -> dict[str, Any]:
    retained = json.dumps({"analysis": toolbox.analysis, "tool_calls": toolbox.calls, "conclusion": conclusion}, ensure_ascii=False)
    exposed = [marker for marker in markers if marker in retained]
    allowed = {"summarize_log", "list_protection_rules", "detect_faults", "inspect_window", "get_evidence", "simulate_rule"}
    safe_tools = all(call["tool"] in allowed for call in toolbox.calls)
    return {
        "tested": bool(markers), "instruction_markers_retained": len(exposed), "only_allowed_tools": safe_tools,
        "scope": "CSV parsing and offline tool workflow only; not a real-model prompt-injection robustness claim.",
        "passed": not exposed and safe_tools,
    }


def grade_guard_rejections(toolbox: Any) -> dict[str, Any]:
    """Deliberate invalid payloads must fail, not silently become verified facts."""
    events = toolbox.analysis["events"]
    if not events:
        return {"tested": False, "checks": {}, "passed": True}
    original = model_payload(events)
    corruptions: dict[str, dict[str, Any]] = {}
    fabricated = copy.deepcopy(original)
    fabricated["findings"][0]["facts"][0]["peak_value"] = 999_999.0
    corruptions["fabricated_peak_rejected"] = fabricated
    fabricated_time = copy.deepcopy(original)
    fabricated_time["findings"][0]["facts"][0]["confirmed_s"] += 1000
    corruptions["fabricated_time_rejected"] = fabricated_time
    unissued = copy.deepcopy(original)
    unissued["findings"][0]["evidence_ids"] = ["E_NOT_ISSUED"]
    unissued["findings"][0]["facts"][0]["event_id"] = "E_NOT_ISSUED"
    corruptions["unissued_citation_rejected"] = unissued
    duplicate = copy.deepcopy(original)
    duplicate["findings"].append(copy.deepcopy(duplicate["findings"][0]))
    corruptions["duplicate_citation_rejected"] = duplicate
    if len(events) > 1:
        incomplete = copy.deepcopy(original)
        incomplete["findings"] = incomplete["findings"][:1]
        corruptions["false_full_coverage_rejected"] = incomplete
    checks = {}
    for name, bad_payload in corruptions.items():
        try:
            validate_conclusion(bad_payload, toolbox)
        except ValueError:
            checks[name] = True
        else:
            checks[name] = False
    return {"tested": True, "checks": checks, "passed": all(checks.values())}
