"""Local report artifacts; credentials and exception internals are never recorded."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
    if isinstance(value, dict):
        return {redact(str(key), secrets): redact(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    return value


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def markdown_report(payload: dict[str, Any]) -> str:
    analysis = payload["analysis"]
    summary = analysis["summary"]
    conclusion = payload.get("conclusion") or {}
    lines = [
        "# BMS 日志分析报告",
        "",
        f"- 模式：{payload['mode']}；状态：{payload['status']}",
        f"- 来源：{_cell(payload['source_name'])}",
        f"- 输入 SHA-256：`{payload['source_sha256']}`",
        f"- 样本数：{summary['sample_count']}；时间：{summary['start_s']}–{summary['end_s']} s",
        f"- API 模型调用数：{payload.get('usage', {}).get('requests', 0)}",
        "",
        "阈值为演示配置。报告描述观测和规则匹配，不证明物理故障根因，也不是实物电池安全验证。",
        "",
        "## 分析结论",
        "",
        _cell(conclusion.get("summary", "模型分析未完成；下方保留确定性规则检查结果。")),
        "",
    ]
    for finding in conclusion.get("findings", []):
        lines.append(f"- {_cell(finding['code'])} [{', '.join(finding['evidence_ids'])}]：{_cell(finding['explanation'])}")
    coverage = conclusion.get("coverage")
    if isinstance(coverage, dict):
        lines.extend(["", "## 证据覆盖", "", f"- 程序全量扫描：{coverage.get('deterministic_scan_complete')}；逐项引用：{coverage.get('mode')}",
                      f"- 总事件 {coverage.get('total_events')}；已取证 {coverage.get('issued_events')}；已引用 {coverage.get('cited_events')}；未引用 {coverage.get('remaining_events')}",
                      "- 确定性全量聚合与模型逐项取证是不同指标。"])
    lines.extend(["", "## 观测与规则证据", "", "| 证据 | 类型 / 通道 | 开始 / 确认 / 恢复 (s) | 状态 | 原始 CSV 行 |", "|---|---|---|---|---|"])
    for event in analysis["events"]:
        lines.append(f"| {_cell(event['evidence_id'])} | {_cell(event['code'])} / {_cell(event.get('channel_id') or event['field'])} | {event['start_s']} / {event['confirmed_s']} / {_cell(event.get('recovered_s'))} | {_cell(event['status'])} | {event['row_start']} / {event['row_confirm']} / {_cell(event.get('row_recover'))} |")
    if not analysis["events"]:
        lines.append("| — | 当前规则未检出事件 | — | — | — |")
    usage_note = "离线模式未调用模型，API用量为零。" if payload["mode"] == "offline" else "Token 用量为已返回 SDK 用量；未返回请求另记为未知，不假报零。软件预算含下一轮输入预估，不是绝对 Token 或金额上限；不自动重试。"
    lines.extend(["", "## 配置与可复现性", "", f"- 分析版本：{_cell(payload.get('analysis_version', 'unknown'))}",
                  f"- 配置/导入指纹：{_cell(payload.get('analysis_fingerprint', 'unknown'))}",
                  f"- 电流约定：{_cell(summary.get('current_convention', 'positive_discharge'))}",
                  f"- 未评估规则：{_cell(summary.get('not_evaluated_rules', []))}",
                  "", "```json", json.dumps({"rules": analysis["rules"], "quality": analysis.get("quality_configuration")}, ensure_ascii=False, indent=2), "```"])
    lines.extend(["", "## 用量", "", "```json", json.dumps(payload.get("usage", {}), ensure_ascii=False, indent=2), "```", "", usage_note, "", "## 局限", ""])
    for limitation in conclusion.get("limitations", []):
        lines.append(f"- {_cell(limitation)}")
    if payload.get("error"):
        lines.append(f"- {_cell(payload['error'])}")
    return "\n".join(lines) + "\n"


def write_bundle(directory: Path, payload: dict[str, Any], secrets: tuple[str, ...] = ()) -> Path:
    # Do not overwrite an earlier report or a user file.
    directory.mkdir(parents=True, exist_ok=False)
    safe = redact(payload, secrets)
    (directory / "report.json").write_text(json.dumps(safe, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    (directory / "report.md").write_text(markdown_report(safe), encoding="utf-8")
    (directory / "tool_calls.json").write_text(json.dumps(safe.get("tool_calls", []), ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    return directory
