"""BYOK is local to the user running this command; there is no shared backend key."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path

from .domain import load_csv, load_profile
from .reporting import redact, write_bundle
from .tools import Toolbox
from .service import make_toolbox, offline_analysis
from .run_control import checkpoint_state, failure_details, load_checkpoint, save_checkpoint


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="BMS日志分析：默认离线；显式选择agent模式才调用自己的API账户。")
    result.add_argument("csv", type=Path, help="输入CSV路径")
    result.add_argument("--mode", choices=["offline", "agent"], default="offline")
    result.add_argument("--question", default="分析日志中的保护事件与数据质量，引用证据并解释确认和恢复过程。")
    result.add_argument("--rules", type=Path, help="可选保护规则JSON")
    result.add_argument("--output", type=Path, default=Path("reports"), help="报告根目录；每次创建独立子目录")
    result.add_argument("--model", help="使用者有权限调用的模型ID；或设置OPENAI_MODEL")
    result.add_argument("--prompt-key", action="store_true", help="通过隐藏输入读取本次运行的Key，不保存")
    result.add_argument("--max-turns", type=int, default=8, help="最多模型轮次，4..20")
    result.add_argument("--timeout", type=float, default=120.0, help="整次运行超时秒数，10..300")
    result.add_argument("--max-output-tokens", type=int, default=1200, help="单轮输出token上限，256..4000")
    result.add_argument("--max-total-tokens", type=int, default=24000, help="累计已返回用量+下一轮预估的软件预算，2000..200000；不是绝对费用上限")
    result.add_argument("--mapping", type=Path, help="导入映射JSON，含column_map和units，canonical列名→源列名")
    result.add_argument("--current-convention", choices=["positive_discharge", "positive_charge"], default="positive_discharge")
    result.add_argument("--resume", type=Path, help="从旧报告的checkpoint.json恢复本地证据，仍需自行提供Key")
    return result


def _offline(toolbox: Toolbox) -> dict:
    return offline_analysis(toolbox)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    key = ""
    environment_key = os.environ.get("OPENAI_API_KEY", "")
    if not args.question.strip() or len(args.question) > 4000 or (args.resume and args.mode != "agent"):
        print("问题须为1..4000字符；检查点恢复仅用于agent模式。", file=sys.stderr)
        return 2
    if args.mode == "offline" and args.prompt_key:
        print("--prompt-key 仅用于 agent 模式。", file=sys.stderr)
        return 2
    if args.mode == "agent":
        if not 4 <= args.max_turns <= 20 or not 10 <= args.timeout <= 300 or not 256 <= args.max_output_tokens <= 4000 or not 2000 <= args.max_total_tokens <= 200000:
            print("调用限制参数超出允许范围。", file=sys.stderr)
            return 2
        model_id = args.model or os.environ.get("OPENAI_MODEL", "")
        if not model_id.strip():
            print("请用 --model 指定自己有权限调用的模型，或设置 OPENAI_MODEL。", file=sys.stderr)
            return 2
    try:
        mapping = json.loads(args.mapping.read_text(encoding="utf-8")) if args.mapping else {}
        if not isinstance(mapping, dict) or set(mapping) - {"column_map", "units"}:
            raise ValueError("Invalid import mapping")
        dataset = load_csv(args.csv, mapping.get("column_map"), mapping.get("units"), current_convention=args.current_convention)
        rules, quality = load_profile(args.rules)
        toolbox = make_toolbox(dataset, rules, quality)
        resume = load_checkpoint(args.resume) if args.resume else None
    except (OSError, ValueError) as exc:
        print(f"输入检查失败（{type(exc).__name__}）；请检查路径、CSV格式和规则JSON。", file=sys.stderr)
        return 2

    if args.mode == "agent":
        try:
            if args.prompt_key:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    key = getpass.getpass("本次运行的 OpenAI API Key（隐藏输入，不保存）：").strip()
            else:
                key = environment_key.strip()
        except getpass.GetPassWarning:
            print("当前终端无法隐藏输入；请换用交互式终端或自行配置进程环境变量。", file=sys.stderr)
            return 2
        except (EOFError, KeyboardInterrupt):
            print("密钥输入已取消。", file=sys.stderr)
            return 2
        if not key:
            print("缺少使用者自己的 OPENAI_API_KEY。使用 --prompt-key 隐藏输入；不会回退到作者或共享密钥。", file=sys.stderr)
            return 2

    secrets = (key, environment_key)
    question = redact(args.question, secrets)
    payload = {
        "mode": args.mode,
        "status": "complete",
        "source_name": dataset.source_name,
        "source_sha256": dataset.sha256,
        "question": question,
        "analysis": toolbox.analysis,
        "analysis_fingerprint": toolbox.fingerprint,
        "cache_hit": toolbox.cache_hit,
        "analysis_version": "bms-rules-v2",
    }
    return_code = 0
    try:
        if args.mode == "offline":
            outcome = _offline(toolbox)
        else:
            from .runtime import run_agent
            outcome = asyncio.run(run_agent(toolbox, question, model_id, key, args.max_turns, args.timeout, args.max_output_tokens,
                                           max_total_tokens=args.max_total_tokens, resume_state=resume))
        payload.update(outcome)
    except (Exception, KeyboardInterrupt, asyncio.CancelledError) as exc:
        payload.update(status="failed", **failure_details(exc))
        return_code = 1
    payload["tool_calls"] = toolbox.calls
    payload["billing"] = "API usage belongs to the key/project of the user running this local command"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = args.output / f"{stamp}-{uuid.uuid4().hex[:8]}"
    try:
        write_bundle(directory, payload, secrets)
        if args.mode == "agent":
            state = checkpoint_state(toolbox, question, model_id, payload["usage"], status=payload["status"], secrets=secrets)
            save_checkpoint(directory / "checkpoint.json", state)
    except (OSError, ValueError):
        print("报告写入失败；请检查输出目录及写入权限。", file=sys.stderr)
        return 1
    print(f"模式：{args.mode}；状态：{payload['status']}")
    print(f"报告：{directory.resolve() / 'report.md'}")
    print("用量：" + json.dumps(payload.get("usage", {}), ensure_ascii=False))
    if return_code:
        print(payload["error"], file=sys.stderr)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
