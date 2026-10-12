"""Run manual fixtures offline: python -m evals.runner --output <new-dir>."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bms_agent.conclusions import canonicalize_conclusion
from bms_agent.domain import QualityConfig, Rule, default_rules, load_csv
from bms_agent.tools import Toolbox

from .cases import EvalCase, load_cases
from .graders import grade_evidence, grade_guard_rejections, grade_prompt_data, match_events, model_payload
from .results import aggregate_metrics, write_results


def prepare_case(case: EvalCase) -> Toolbox:
    config = case.config
    dataset = load_csv(case.csv_path, config.get("column_map"), config.get("units"),
                       current_convention=config.get("current_convention", "positive_discharge"),
                       pack_id=config.get("pack_id", "eval_pack"))
    if dataset.sha256 != case.csv_sha256:
        raise ValueError("Fixture changed after labels were loaded")
    rules = [Rule(**rule) for rule in config["rules"]] if "rules" in config else default_rules()
    quality = QualityConfig(**config["quality"]) if "quality" in config else None
    return Toolbox(dataset, rules, quality=quality)


def baseline_conclusion(toolbox: Toolbox) -> dict[str, Any]:
    toolbox.invoke("summarize_log")
    toolbox.invoke("list_protection_rules")
    issued, offset = [], 0
    while True:
        page = toolbox.invoke("detect_faults", {"offset": offset, "limit": 20})
        issued.extend(page["events"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    return canonicalize_conclusion(model_payload(issued), toolbox)


async def sdk_mock_conclusion(toolbox: Toolbox) -> tuple[dict[str, Any], dict[str, Any]]:
    from unittest.mock import patch
    from bms_agent.runtime import run_agent
    from .sdk_mock import make_mock_model
    model = make_mock_model()
    with patch("bms_agent.runtime.AsyncOpenAI", side_effect=AssertionError("Offline evaluation must not create an API client")), \
            patch("socket.create_connection", side_effect=AssertionError("Offline evaluation must not connect to a server")):
        result = await run_agent(toolbox, "Analyze this manually labelled fixture using log evidence only.", "", "", model_override=model)
    if not model.saw_sdk_tool_output:
        raise ValueError("SDK mock did not consume actual Runner tool output")
    usage = {**result["usage"], "simulated": True, "tokens_measured": False, "api_calls": 0, "charged_cost": 0}
    return result["conclusion"], usage


def grade_case(case: EvalCase, toolbox: Toolbox, conclusion: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    events = match_events(toolbox.analysis["events"], case.expected_events, case.time_tolerance_s)
    evidence = grade_evidence(conclusion, toolbox)
    guards = grade_guard_rejections(toolbox)
    prompt = grade_prompt_data(toolbox, conclusion, case.config.get("prompt_markers", []))
    return {
        "case_id": case.case_id, "case_version": case.case_version,
        "csv_file": case.csv_path.name, "csv_sha256": case.csv_sha256,
        "description": case.description, "time_tolerance_s": case.time_tolerance_s,
        "events": events, "evidence": evidence, "guards": guards, "prompt_data": prompt,
        "coverage": conclusion["coverage"], "usage": usage,
        "passed": all(grader["passed"] for grader in (events, evidence, guards, prompt)),
    }


def failed_case(case: EvalCase, error: Exception) -> dict[str, Any]:
    # Provider/raw CSV exception text is never echoed into artifacts.
    return {"case_id": case.case_id, "case_version": case.case_version,
            "csv_file": case.csv_path.name, "csv_sha256": case.csv_sha256,
            "description": case.description, "time_tolerance_s": case.time_tolerance_s,
            "passed": False, "error_type": type(error).__name__}


def run_suite(manifest: Path | None = None, *, sdk_mock: bool = False) -> dict[str, Any]:
    metadata, cases = load_cases(manifest)
    results = []
    for case in cases:
        try:
            toolbox = prepare_case(case)
            if sdk_mock:
                conclusion, usage = asyncio.run(sdk_mock_conclusion(toolbox))
            else:
                conclusion = baseline_conclusion(toolbox)
                usage = {"api_calls": 0, "requests": 0, "input_tokens": 0, "output_tokens": 0,
                         "total_tokens": 0, "charged_cost": 0, "simulated": False, "tokens_measured": False}
            results.append(grade_case(case, toolbox, conclusion, usage))
        except Exception as error:
            results.append(failed_case(case, error))
    return {
        "schema_version": 1, "suite": metadata,
        "mode": "sdk_mock_tool_reflection" if sdk_mock else "deterministic_rule_baseline",
        "real_model": "not_measured", "api_calls": 0, "charged_api_cost": 0,
        "usage_interpretation": "SDK mock turns are simulated; tokens, model latency and model quality are not measured.",
        "status": "pass" if all(result["passed"] for result in results) else "fail",
        "metrics": aggregate_metrics(results), "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independently labelled offline BMS regression evaluation (no API or keys)")
    parser.add_argument("--output", type=Path, default=None, help="New nonexisting output directory")
    parser.add_argument("--manifest", type=Path, default=None, help="Versioned manual fixture manifest")
    parser.add_argument("--sdk-mock", action="store_true", help="Use optional installed SDK Runner with a local scripted model; no API")
    args = parser.parse_args(argv)
    output = args.output or Path("reports") / ("eval-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    try:
        payload = run_suite(args.manifest, sdk_mock=args.sdk_mock)
        write_results(output, payload)
    except (ValueError, OSError, TypeError, KeyError, json.JSONDecodeError):
        print("Evaluation setup or report write failed; check manifest and use a new output directory.")
        return 2
    print(f"{payload['status']}: {payload['metrics']['cases_passed']}/{payload['metrics']['cases_total']} required cases; real model not measured; API calls 0.")
    print(f"Report: {(output / 'report.md').resolve()}")
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
