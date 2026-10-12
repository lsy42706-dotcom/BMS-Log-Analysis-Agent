"""SDK-independent error classification and credential-free local checkpoints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .reporting import redact


class TokenBudgetExceeded(ValueError):
    pass


def classify_error(exc: BaseException) -> tuple[str, str, bool]:
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else {}
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(exc, TokenBudgetExceeded):
        return "token_budget", "已停止：下一轮可能超过软件 Token 预算。可查看检查点；增加预算后再继续。", False
    if name in ("TimeoutError", "APITimeoutError"):
        return "timeout", "调用超时。未返回的请求用量可能未知；确认后可从本地证据检查点继续。", True
    if name in ("CancelledError", "KeyboardInterrupt"):
        return "cancelled", "运行已取消；已返回的用量和本地证据保留，未完成请求可能计费。", False
    if name == "AuthenticationError" or status == 401:
        return "authentication", "当前使用者的 API Key 无效或已失效；请自行检查，不要发送密钥。", False
    if name == "PermissionDeniedError" or status == 403:
        return "permission", "当前使用者的项目或模型权限不足。", False
    if code in ("insufficient_quota", "billing_hard_limit_reached"):
        return "quota", "当前使用者的 API 余额或项目配额不足。", False
    if name == "RateLimitError" or status == 429:
        return "rate_limit", "请求限速。等待后可手动继续；本程序不自动重试付费请求。", True
    if name == "APIConnectionError":
        return "connection", "网络连接失败；检查网络后可手动继续。请求是否计费可能未知。", True
    if status == 404:
        return "model_not_found", "模型不可用或当前使用者无访问权限；请检查模型 ID。", False
    if isinstance(status, int) and status >= 500:
        return "provider", "模型服务暂时失败；本程序不自动重试，避免重复计费。", True
    if name == "MaxTurnsExceeded":
        return "turn_limit", "已达到模型轮次上限；如需继续，请显式增加上限。", False
    if isinstance(exc, ImportError):
        return "dependency", "尚未安装 Agent 依赖；请执行 python -m pip install -e '.[agent]'。", False
    if isinstance(exc, ValueError):
        return "validation", "结构化结论或运行参数未通过校验；未把未核验模型文本写入结论。", False
    return "unexpected", "模型分析失败；已保留本地规则结果。内部异常和请求内容不回显。", False


def failure_details(exc: BaseException) -> dict[str, Any]:
    code, message, retryable = classify_error(exc)
    usage = getattr(exc, "bms_usage", None)
    if usage is None:
        usage = {"requests": None, "input_tokens": None, "output_tokens": None, "total_tokens": None,
                 "complete": False, "unresolved_requests": None,
                 "note": "没有最终用量；失败调用可能已计费，未知不等于零。"}
    return {"error": message, "error_kind": type(exc).__name__, "error_code": code,
            "retryable": retryable, "automatic_retry": False, "usage": usage}


def checkpoint_state(toolbox: Any, question: str, model_id: str, usage: dict,
                     *, status: str, secrets: tuple[str, ...] = ()) -> dict:
    from .service import analysis_fingerprint
    return redact({"schema_version": 1, "resume_mode": "verified_tool_replay_no_model_reasoning",
                   "analysis_fingerprint": analysis_fingerprint(toolbox.dataset, toolbox.rules, toolbox.quality),
                   "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
                   "model_id": model_id, "status": status, "usage": usage,
                   # Save local audited arguments, never raw model messages, secrets or clients.
                   "tool_calls": [{"tool": call["tool"], "arguments": call["arguments"],
                                   "status": call["status"]} for call in toolbox.calls]}, secrets)


def save_checkpoint(path: Path, state: dict) -> None:
    """Create a new file only; callers use a fresh report directory each run."""
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)


def load_checkpoint(path: Path) -> dict:
    target = Path(path)
    if target.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Checkpoint exceeds its size limit")
    state = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValueError("Unsupported checkpoint schema")
    return state


def validate_checkpoint_usage(usage: object) -> dict:
    if not isinstance(usage, dict):
        raise ValueError("Invalid checkpoint usage")
    for key in ("requests", "input_tokens", "output_tokens", "total_tokens", "unresolved_requests"):
        if isinstance(usage.get(key), bool) or not isinstance(usage.get(key), int) or usage[key] < 0:
            raise ValueError("Checkpoint needs known returned usage and a valid unresolved count")
    if usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]:
        raise ValueError("Inconsistent checkpoint usage")
    if not isinstance(usage.get("complete"), bool) or usage["complete"] != (usage["unresolved_requests"] == 0):
        raise ValueError("Inconsistent checkpoint completeness")
    return dict(usage)


def replay_checkpoint(state: dict, toolbox: Any, question: str, model_id: str) -> tuple[list[dict], dict]:
    """Recompute every recorded local read; checkpoint text is never evidence.

    This resumes application evidence collection, not hidden model reasoning or
    an in-flight provider request. All model calls still require a new BYOK key.
    """
    from .service import analysis_fingerprint
    if state.get("schema_version") != 1 or state.get("resume_mode") != "verified_tool_replay_no_model_reasoning":
        raise ValueError("Unsupported checkpoint schema")
    if state.get("analysis_fingerprint") != analysis_fingerprint(toolbox.dataset, toolbox.rules, toolbox.quality):
        raise ValueError("Checkpoint input, import mapping, rules or analysis version changed")
    if state.get("question_sha256") != hashlib.sha256(question.encode()).hexdigest() or state.get("model_id") != model_id:
        raise ValueError("Checkpoint question or model changed")
    calls = state.get("tool_calls")
    usage = state.get("usage")
    if not isinstance(calls, list) or len(calls) > toolbox.max_calls:
        raise ValueError("Invalid checkpoint state")
    usage = validate_checkpoint_usage(usage)
    history = [{"role": "user", "content": question}]
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or call.get("status") not in ("complete", "failed") or not isinstance(call.get("arguments"), dict):
            raise ValueError("Invalid checkpoint tool audit")
        if call["status"] == "failed":
            toolbox.calls.append({"tool": "checkpoint_failed_call", "arguments": {}, "status": "failed", "error": "restored_call_budget"})
            continue
        result = toolbox.invoke(call["tool"], call["arguments"])
        call_id = f"bms-resume-{index}"
        history.extend([{"type": "function_call", "name": call["tool"], "arguments": json.dumps(call["arguments"], allow_nan=False), "call_id": call_id},
                        {"type": "function_call_output", "call_id": call_id, "output": json.dumps(result, ensure_ascii=False, allow_nan=False)}])
    return history, usage
