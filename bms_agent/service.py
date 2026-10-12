"""Local application boundary shared by the upload UI and offline CLI.

CSV content stays in this process. Credentials are explicit per request;
neither this module nor the web service reads an environment or shared key.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any

from . import domain
from .conclusions import canonical_event_fact, canonicalize_conclusion
from .reporting import markdown_report, redact
from .tools import Toolbox, integer, number


ANALYSIS_VERSION = "bms-rules-v2"
_CACHE: OrderedDict[str, dict] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_LIMIT = 4
_CACHE_MAX_ENTRY_BYTES = 4 * 1024 * 1024


def analysis_fingerprint(dataset: domain.Dataset, rules: list[domain.Rule], quality: Any = None) -> str:
    from dataclasses import asdict
    data = {"version": ANALYSIS_VERSION, "source_sha256": dataset.sha256,
            "import_metadata": getattr(dataset, "import_metadata", {}),
            "current_convention": getattr(dataset, "current_convention", "positive_discharge"),
            "pack_id": getattr(dataset, "pack_id", "pack_1"),
            "rules": [asdict(rule) for rule in rules],
            "quality": asdict(quality) if quality is not None else None}
    return hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest()


def make_toolbox(dataset: domain.Dataset, rules: list[domain.Rule], quality: Any = None, *, use_cache: bool = True) -> Toolbox:
    fingerprint = analysis_fingerprint(dataset, rules, quality)
    with _CACHE_LOCK:
        cached = copy.deepcopy(_CACHE.get(fingerprint)) if use_cache else None
        if cached is not None:
            _CACHE.move_to_end(fingerprint)
    box = Toolbox(dataset, rules, quality=quality, cached_analysis=cached)
    box.fingerprint = fingerprint
    box.cache_hit = cached is not None
    if use_cache and cached is None and len(json.dumps(box.analysis, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= _CACHE_MAX_ENTRY_BYTES:
        with _CACHE_LOCK:
            _CACHE[fingerprint] = copy.deepcopy(box.analysis)
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.popitem(last=False)
    return box


def offline_analysis(toolbox: Toolbox) -> dict:
    for name in ("summarize_log", "list_protection_rules", "detect_faults"):
        toolbox.invoke(name)
    events = toolbox.analysis["events"]
    toolbox.issued_evidence.update({event["evidence_id"]: event for event in events})
    proposal = {"summary": "", "coverage": "full", "limitations": [],
                "findings": [{"code": event["code"], "evidence_ids": [event["evidence_id"]],
                              "facts": [canonical_event_fact(event)], "explanation": ""} for event in events]}
    conclusion = canonicalize_conclusion(proposal, toolbox)
    conclusion["coverage"]["consumer"] = "deterministic_offline_application_not_model"
    conclusion["summary"] = f"离线规则分析完成，完整扫描并引用 {len(events)} 个规则或数据质量事件。未调用语言模型。"
    conclusion["limitations"][-1] = "这是程序按规则和日志事实生成的报告，不是模型诊断；未进行实物电池安全验证。"
    return {"conclusion": conclusion,
            "usage": {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                      "simulated": False, "complete": True, "unresolved_requests": 0}}


def _inputs(payload: dict) -> tuple[domain.Dataset, list[domain.Rule], Any]:
    if not isinstance(payload, dict):
        raise ValueError("Request must be an object")
    text = payload.get("csv_text")
    if not isinstance(text, str):
        raise ValueError("A UTF-8 CSV upload is required")
    content = text.encode("utf-8")
    dataset = domain.load_csv_bytes(content, payload.get("source_name", "upload.csv"),
        payload.get("column_map"), payload.get("units"),
        current_convention=payload.get("current_convention", "positive_discharge"),
        pack_id=payload.get("pack_id", "pack_1"))
    profile = payload.get("profile")
    rules, quality = domain.profile_from_dict(profile) if profile is not None else (domain.default_rules(), None)
    return dataset, rules, quality


def _limits(payload: dict) -> dict:
    return {"max_turns": integer(payload.get("max_turns", 8), "max_turns", 4, 20),
            "timeout_s": number(payload.get("timeout_s", 120), "timeout_s"),
            "max_output_tokens": integer(payload.get("max_output_tokens", 1200), "max_output_tokens", 256, 4000),
            "max_total_tokens": integer(payload.get("max_total_tokens", 24000), "max_total_tokens", 2000, 200000)}


def analyze_request(payload: dict) -> dict:
    dataset, rules, quality = _inputs(payload)
    mode = payload.get("mode", "offline")
    if mode not in ("offline", "agent"):
        raise ValueError("Unsupported analysis mode")
    key = payload.get("api_key", "")
    model = payload.get("model", "")
    if not isinstance(key, str) or not isinstance(model, str):
        raise ValueError("Invalid credentials or model selection")
    question = payload.get("question", "分析保护事件与数据质量，引用证据。")
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        raise ValueError("Question must contain 1..4000 characters")
    question = redact(question, (key,))
    limits = _limits(payload)
    if not 10 <= limits["timeout_s"] <= 300:
        raise ValueError("Timeout must be within 10..300 seconds")
    if mode == "agent" and (not key.strip() or not model.strip()):
        raise ValueError("Agent mode requires this request's own key and model")
    toolbox = make_toolbox(dataset, rules, quality)
    report = {"mode": mode, "status": "complete", "source_name": dataset.source_name,
              "source_sha256": dataset.sha256, "analysis_fingerprint": toolbox.fingerprint,
              "analysis_version": ANALYSIS_VERSION, "cache_hit": toolbox.cache_hit,
              "question": question, "analysis": toolbox.analysis,
              "billing": "Current request's user-provided key only; no shared or author key"}
    try:
        if mode == "offline":
            result = offline_analysis(toolbox)
        else:
            from .runtime import run_agent
            result = asyncio.run(run_agent(toolbox, question, model.strip(), key.strip(), **limits))
        report.update(result)
    except Exception as exc:
        from .run_control import failure_details
        report.update(status="failed", **failure_details(exc))
    report["tool_calls"] = toolbox.calls
    waveform = toolbox._window(dataset.samples[0].timestamp_s, dataset.samples[-1].timestamp_s, 50)
    report = redact(report, (key,))
    return {"report": report, "waveform": waveform, "report_markdown": markdown_report(report)}


def inspect_request(payload: dict) -> dict:
    if "api_key" in payload:
        raise ValueError("Window inspection must not contain credentials")
    dataset, rules, quality = _inputs(payload)
    toolbox = make_toolbox(dataset, rules, quality)
    return toolbox._window(payload.get("start_s", dataset.samples[0].timestamp_s),
                           payload.get("end_s", dataset.samples[-1].timestamp_s),
                           payload.get("max_samples", 50))
