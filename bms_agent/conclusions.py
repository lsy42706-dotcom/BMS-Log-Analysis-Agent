"""Canonical conclusions derived from trusted, deterministic event evidence.

This module deliberately has no SDK or Pydantic dependency. Free-form model
prose is never promoted to verified report text: the application regenerates
the summary, explanations, limitations and coverage from the input evidence.
"""

from __future__ import annotations

import math
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .tools import Toolbox


FACT_FIELDS = (
    "event_id", "rule_id", "field", "channel_id", "start_s", "confirmed_s",
    "recovered_s", "peak_value", "status",
)
NUMERIC_FACT_FIELDS = {"start_s", "confirmed_s", "recovered_s", "peak_value"}


def canonical_event_fact(event: dict[str, Any]) -> dict[str, Any]:
    """Return the exact fact contract that a model must copy, not infer."""
    return {
        "event_id": event["evidence_id"],
        "rule_id": event["rule_id"],
        "field": event["field"],
        "channel_id": event.get("channel_id"),
        "start_s": event["start_s"],
        "confirmed_s": event["confirmed_s"],
        "recovered_s": event["recovered_s"],
        "peak_value": event["peak_value"],
        "status": event["status"],
    }


def event_aggregates(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize every event locally; aggregates do not imply model inspection."""
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in events:
        key = (event["code"], event["rule_id"], event["field"], event.get("channel_id"), event["status"])
        if key not in groups:
            groups[key] = {
                "code": key[0], "rule_id": key[1], "field": key[2], "channel_id": key[3],
                "status": key[4], "count": 0, "first_start_s": event["start_s"],
                "last_confirmed_s": event["confirmed_s"], "last_recovered_s": None,
                "peak_min": None, "peak_max": None, "peak_abs_max": None,
                "representative_evidence_ids": [],
            }
        group = groups[key]
        group["count"] += 1
        group["first_start_s"] = min(group["first_start_s"], event["start_s"])
        group["last_confirmed_s"] = max(group["last_confirmed_s"], event["confirmed_s"])
        recovered = event.get("recovered_s")
        if recovered is not None:
            group["last_recovered_s"] = recovered if group["last_recovered_s"] is None else max(group["last_recovered_s"], recovered)
        peak = event.get("peak_value")
        if peak is not None:
            group["peak_min"] = peak if group["peak_min"] is None else min(group["peak_min"], peak)
            group["peak_max"] = peak if group["peak_max"] is None else max(group["peak_max"], peak)
            group["peak_abs_max"] = abs(peak) if group["peak_abs_max"] is None else max(group["peak_abs_max"], abs(peak))
        if len(group["representative_evidence_ids"]) < 3:
            group["representative_evidence_ids"].append(event["evidence_id"])
    return sorted(groups.values(), key=lambda item: (item["code"], item["rule_id"], item["field"], str(item["channel_id"]), item["status"]))


def _exact_fact(supplied: dict[str, Any], expected: dict[str, Any]) -> bool:
    if set(supplied) != set(FACT_FIELDS):
        return False
    for key in FACT_FIELDS:
        actual, trusted = supplied[key], expected[key]
        if key in NUMERIC_FACT_FIELDS and actual is not None:
            if isinstance(actual, bool) or not isinstance(actual, (float, int)):
                return False
            try:
                if not math.isfinite(actual):
                    return False
            except OverflowError:
                return False
        elif actual is not None and not isinstance(actual, str):
            return False
        if actual != trusted:
            return False
    return True


def validate_conclusion(conclusion: dict[str, Any], toolbox: Toolbox) -> None:
    """Reject incorrect copied facts, duplicate citations and false full coverage.

    Free prose is not parsed with regular expressions and is not evidence.
    Consumers must call ``canonicalize_conclusion`` before presenting a result.
    The application scope is the entire selected dataset, never model-inferred.
    """
    successful = {call["tool"] for call in toolbox.calls if call["status"] == "complete"}
    if not {"summarize_log", "list_protection_rules", "detect_faults"} <= successful:
        raise ValueError("Agent must inspect the log, rules and detected faults before concluding")
    findings = conclusion.get("findings")
    if not isinstance(findings, list) or not isinstance(conclusion.get("summary"), str):
        raise ValueError("Invalid conclusion structure")
    declared_coverage = conclusion.get("coverage")
    if declared_coverage not in ("full", "partial"):
        raise ValueError("Conclusion must explicitly declare full or partial evidence coverage")
    cited: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("Invalid finding")
        ids, facts = finding.get("evidence_ids"), finding.get("facts")
        if not isinstance(ids, list) or not ids or not isinstance(facts, list) or len(ids) != len(facts):
            raise ValueError("Each finding must provide exact typed facts for every citation")
        if not all(isinstance(evidence_id, str) for evidence_id in ids):
            raise ValueError("Evidence IDs must be strings")
        fact_ids = []
        for fact in facts:
            if not isinstance(fact, dict) or not isinstance(fact.get("event_id"), str):
                raise ValueError("Invalid event fact")
            fact_ids.append(fact["event_id"])
        if len(set(fact_ids)) != len(fact_ids) or set(fact_ids) != set(ids):
            raise ValueError("Event facts must correspond exactly to unique evidence IDs")
        for evidence_id in ids:
            if not isinstance(evidence_id, str) or evidence_id not in toolbox.issued_evidence:
                raise ValueError("Conclusion cites evidence that was not returned by a log tool")
            if evidence_id in cited:
                raise ValueError("Duplicate evidence citations are not allowed")
            event = toolbox.issued_evidence[evidence_id]
            if event["code"] != finding.get("code"):
                raise ValueError("Finding code does not match its evidence")
            supplied = facts[fact_ids.index(evidence_id)]
            if not _exact_fact(supplied, canonical_event_fact(event)):
                raise ValueError("Finding facts do not match trusted event evidence")
            cited.add(evidence_id)
    events = toolbox.analysis["events"]
    if events and not cited:
        raise ValueError("Faults were found but the conclusion omitted every finding")
    scope = {event["evidence_id"] for event in events}
    if declared_coverage == "full" and not (scope <= set(toolbox.issued_evidence) and scope == cited):
        raise ValueError("Full coverage requires every in-scope event to be issued and cited")


def canonicalize_conclusion(conclusion: dict[str, Any], toolbox: Toolbox) -> dict[str, Any]:
    """Validate facts, then return exclusively application-authored report text."""
    validate_conclusion(conclusion, toolbox)
    events = toolbox.analysis["events"]
    scope = {event["evidence_id"] for event in events}
    cited = {evidence_id for finding in conclusion["findings"] for evidence_id in finding["evidence_ids"]}
    full = cited == scope and scope <= set(toolbox.issued_evidence)
    mode = "full" if full else "partial"
    coverage = {
        "mode": mode,
        "scope": "entire_selected_log",
        "source_sha256": toolbox.dataset.sha256,
        "deterministic_scan_complete": True,
        "total_events": len(scope),
        "issued_events": len(scope & set(toolbox.issued_evidence)),
        "cited_events": len(cited),
        "remaining_events": len(scope - cited),
    }
    findings = []
    for finding in conclusion["findings"]:
        ids = finding["evidence_ids"]
        trusted = [toolbox.issued_evidence[evidence_id] for evidence_id in ids]
        explanations = []
        for event in trusted:
            channel = f"，通道 {event['channel_id']}" if event.get("channel_id") is not None else ""
            recovered = "尚未观测到确认恢复" if event["recovered_s"] is None else f"恢复时刻 {event['recovered_s']:g} s"
            peak = "无有效峰值" if event["peak_value"] is None else f"记录极值 {event['peak_value']:g}"
            explanations.append(
                f"{event['evidence_id']}：{event['field']}{channel}，规则 {event['rule_id']}；"
                f"起始 {event['start_s']:g} s，确认 {event['confirmed_s']:g} s，{recovered}；{peak}，状态 {event['status']}。"
            )
        findings.append({
            "code": finding["code"], "evidence_ids": ids,
            "facts": [canonical_event_fact(event) for event in trusted],
            "explanation": " ".join(explanations),
        })
    summary = (
        f"程序已扫描所选日志的全部采样，检测到 {len(events)} 个规则或数据质量事件；"
        f"结论已核验引用 {len(cited)} 个事件，证据覆盖为{'完整' if full else '部分'}。"
    )
    limitations = [
        "结论仅描述已观测采样与配置规则匹配，不证明物理故障根因，不执行电池控制。",
        "当前阈值和规则须按电池体系与实际设备验证；无事件不代表真实电池安全。",
        "模型生成的自由摘要、解释与限制说明已丢弃；这里的数值和覆盖声明由程序生成。",
    ]
    if not full:
        limitations.append(f"还有 {len(scope - cited)} 个事件未在本次模型结论中逐项引用；全量规则扫描聚合不等于全量模型取证。")
    return {
        "summary": summary, "findings": findings, "limitations": limitations,
        "coverage": coverage, "event_aggregates": event_aggregates(events),
        "text_provenance": "application_generated_from_verified_event_facts",
    }
