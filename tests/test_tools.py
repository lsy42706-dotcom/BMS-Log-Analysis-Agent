from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bms_agent.conclusions import canonical_event_fact, canonicalize_conclusion
from bms_agent.domain import Dataset, Sample, Rule, default_rules
from bms_agent.reporting import redact, write_bundle
from bms_agent.samples import generate_samples
from bms_agent.tools import Toolbox, validate_conclusion


ROOT = Path(__file__).resolve().parents[1]


def toolbox(scenario="mixed", budget=20):
    dataset = Dataset(generate_samples(scenario), [], "synthetic.csv", "f" * 64)
    return Toolbox(dataset, default_rules(), budget)


class ToolTests(unittest.TestCase):
    def test_pages_cover_all_evidence(self):
        box = toolbox()
        page = box.invoke("detect_faults", {"limit": 2})
        self.assertEqual(page["total"], 4)
        self.assertEqual(page["next_offset"], 2)
        last = box.invoke("detect_faults", {"offset": 2, "limit": 2})
        self.assertIsNone(last["next_offset"])
        self.assertEqual(len(box.issued_evidence), 4)

    def test_filter_and_evidence_match(self):
        box = toolbox()
        result = box.invoke("detect_faults", {"code": "UNDERVOLTAGE"})
        self.assertEqual(result["total"], 1)
        event = result["events"][0]
        self.assertEqual(box.invoke("get_evidence", {"evidence_id": event["evidence_id"]})["event"], event)

    def test_30000_events_use_event_total_not_sample_limit(self):
        # Each odd row starts three missing-sensor events. This valid 20k-sample
        # dataset therefore has 30k events, not merely 20k possible events.
        samples = [Sample(float(i), *(None if i % 2 else value for value in (3.7, 10.0, 25.0)), i + 2) for i in range(20_000)]
        box = Toolbox(Dataset(samples, [], "many_events.csv", "f" * 64), default_rules())
        summary = box.invoke("summarize_log")
        self.assertEqual(summary["event_count"], 30_000)
        self.assertEqual(summary["event_aggregate_summary"]["by_code"], {"SENSOR_INVALID": 30_000})
        self.assertEqual(sum(group["count"] for group in summary["event_groups"]), 30_000)
        for offset in (20_000, 20_040, 29_960):
            with self.subTest(offset=offset):
                page = box.invoke("detect_faults", {"offset": offset, "limit": 40})
                self.assertEqual(len(page["events"]), 40)
                self.assertEqual(page["total"], 30_000)
                self.assertEqual(page["aggregate_summary"]["total_events"], 30_000)
                self.assertEqual(page["next_offset"], None if offset == 29_960 else offset + 40)
        terminal = box.invoke("detect_faults", {"offset": 30_000})
        self.assertEqual(terminal["events"], [])
        self.assertIsNone(terminal["next_offset"])
        with self.assertRaises(ValueError):
            box.invoke("detect_faults", {"offset": 30_001})

    def test_filtered_offset_is_bounded_by_filtered_event_count(self):
        box = toolbox()
        page = box.invoke("detect_faults", {"code": "OVERCURRENT", "offset": 1})
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["events"], [])
        with self.assertRaises(ValueError):
            box.invoke("detect_faults", {"code": "OVERCURRENT", "offset": 2})

    def test_window_limit_retains_endpoints(self):
        result = toolbox().invoke("inspect_window", {"start_s": 0, "end_s": 39, "max_samples": 3})
        self.assertEqual(result["matched_samples"], 40)
        self.assertTrue(result["downsampled"])
        self.assertEqual(result["samples"][0]["timestamp_s"], 0)
        self.assertEqual(result["samples"][-1]["timestamp_s"], 39)

    def test_window_preserves_peak_and_discloses_missing_critical_points(self):
        box = toolbox()
        result = box.invoke("inspect_window", {"start_s": 0, "end_s": 39, "max_samples": 20})
        self.assertEqual(max(sample["current_a"] for sample in result["samples"] if sample["current_a"] is not None), 55.0)
        self.assertTrue(result["sampling"]["critical_points_complete"])
        retained = {sample["timestamp_s"] for sample in result["samples"]}
        for event in box.analysis["events"]:
            self.assertTrue({event["start_s"], event["confirmed_s"], event["recovered_s"]} - {None} <= retained)
        sparse = box.invoke("inspect_window", {"start_s": 0, "end_s": 39, "max_samples": 3})
        self.assertFalse(sparse["sampling"]["critical_points_complete"])
        self.assertGreater(sparse["sampling"]["critical_points_missing"], 0)

    def test_empty_and_single_point_windows_are_safe(self):
        box = toolbox()
        empty = box.invoke("inspect_window", {"start_s": 99, "end_s": 100})
        self.assertEqual(empty["samples"], [])
        self.assertTrue(empty["sampling"]["critical_points_complete"])
        single = box.invoke("inspect_window", {"start_s": 0, "end_s": 39, "max_samples": 1})
        self.assertEqual(len(single["samples"]), 1)
        self.assertFalse(single["sampling"]["critical_points_complete"])

    def test_unknown_tools_and_bad_windows_fail_safely(self):
        for name, args in [("read_file", {"path": "private.txt"}), ("inspect_window", {"start_s": 10, "end_s": 1}), ("detect_faults", {"limit": True}), ("inspect_window", {"start_s": float("nan"), "end_s": 1}), ("get_evidence", {"evidence_id": "E999"})]:
            with self.subTest(name=name):
                box = toolbox()
                with self.assertRaises(ValueError):
                    box.invoke(name, args)
                self.assertEqual(box.calls[-1]["status"], "failed")

    def test_call_budget_is_enforced(self):
        box = toolbox(budget=1)
        box.invoke("summarize_log")
        with self.assertRaises(ValueError):
            box.invoke("summarize_log")

    def test_failed_nonfinite_arguments_leave_json_safe_audit(self):
        box = toolbox()
        with self.assertRaises(ValueError):
            box.invoke("inspect_window", {"start_s": float("nan"), "end_s": 2})
        json.dumps(box.calls, allow_nan=False)

    def test_synthetic_probe_not_log_evidence(self):
        box = toolbox()
        result = box.invoke("simulate_rule", {"rule_id": "overcurrent", "values": [10, 50, 50, 50, 10, 10, 10]})
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["events"][0]["confirmed_s"], 3)
        self.assertTrue(result["events"][0]["evidence_id"].startswith("SIM-"))
        self.assertFalse(box.issued_evidence)

    def test_wide_directional_and_spread_probes_are_still_synthetic(self):
        wide = Dataset([Sample(float(i), None, -55, None, i + 2, {"C01": 3.7}, {"T01": 25}) for i in range(3)], [], "wide.csv", "f" * 64)
        rules = [Rule("uv", "UNDERVOLTAGE", "cell_voltage_v", "low", 3, 3.2, 2, 2, channel_id="C01"),
                 Rule("charge", "CHARGE_OC", "current_a", "abs_high", 50, 45, 2, 2, current_mode="charge"),
                 Rule("spread", "IMBALANCE", "cell_spread_v", "high", .2, .1, 2, 2)]
        box = Toolbox(wide, rules)
        for rule_id, values in (("uv", [2.8] * 3), ("charge", [-55] * 3), ("spread", [.3] * 3)):
            result = box.invoke("simulate_rule", {"rule_id": rule_id, "values": values})
            self.assertEqual(len(result["events"]), 1)
            self.assertTrue(result["events"][0]["evidence_id"].startswith("SIM-"))
        self.assertFalse(box.issued_evidence)

    def test_wide_waveform_retains_channel_extrema(self):
        wide = Dataset([Sample(float(i), None, 10, None, i + 2, {"C01": 4.3 if i == 6 else 3.7}, {"T01": 70 if i == 8 else 25}) for i in range(20)], [], "wide.csv", "f" * 64)
        result = Toolbox(wide, default_rules()).invoke("inspect_window", {"start_s": 0, "end_s": 19, "max_samples": 8})
        self.assertEqual(max(sample["cell_voltages_v"]["C01"] for sample in result["samples"]), 4.3)
        self.assertEqual(max(sample["temperatures_c"]["T01"] for sample in result["samples"]), 70)

    def test_mandatory_summary_and_rules_remain_bounded_for_wide_logs(self):
        samples = [Sample(float(i), None, 60.00000000000001, None, i + 2,
                          {f"C{j:02}": 2.9000000000000004 for j in range(31)},
                          {f"T{j:02}": 61.00000000000001 for j in range(32)}) for i in range(4)]
        box = Toolbox(Dataset(samples, [], "wide.csv", "f" * 64), default_rules())
        summary = box.invoke("summarize_log")
        self.assertEqual(summary["stats_total_channels"], 64)
        self.assertTrue(summary["stats_truncated"])
        self.assertEqual(len(summary["channels"]), 64)
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False)), 32000)
        self.assertEqual(len(box.analysis["summary"]["stats"]), 64)
        rules = [Rule(f"oc{i}", "OC", "current_a", "abs_high", 50, 45, 2, 2) for i in range(100)]
        box = Toolbox(Dataset([Sample(0, 3.7, 10, 25, 2)], [], "many.csv", "f" * 64), rules)
        first = box.invoke("list_protection_rules")
        self.assertEqual(len(first["rules"]), 20)
        self.assertEqual(first["next_offset"], 20)
        self.assertIsNone(box.invoke("list_protection_rules", {"offset": 80})["next_offset"])

    def test_probe_size_and_finite_input_limits(self):
        for values in ([], [1] * 129, [float("inf")], [True]):
            with self.subTest(values=len(values)):
                with self.assertRaises(ValueError):
                    toolbox().invoke("simulate_rule", {"rule_id": "overcurrent", "values": values})

    def test_numeric_tool_result_serializes_without_nan(self):
        box = toolbox("sensor_fault")
        for name, args in [("summarize_log", {}), ("detect_faults", {}), ("inspect_window", {"start_s": 7, "end_s": 9})]:
            json.dumps(box.invoke(name, args), allow_nan=False)


class CitationTests(unittest.TestCase):
    def prepared(self):
        box = toolbox()
        box.invoke("summarize_log")
        box.invoke("list_protection_rules")
        events = box.invoke("detect_faults")["events"]
        return box, events[0]

    def conclusion(self, event):
        return {"summary": "Sampled fault", "findings": [{"code": event["code"], "evidence_ids": [event["evidence_id"]], "facts": [canonical_event_fact(event)], "explanation": "Observed"}], "limitations": [], "coverage": "partial"}

    def test_valid_citation(self):
        box, event = self.prepared()
        validate_conclusion(self.conclusion(event), box)

    def test_requires_actual_tool_use(self):
        box = toolbox()
        with self.assertRaises(ValueError):
            validate_conclusion({"summary": "guess", "findings": [], "limitations": []}, box)

    def test_rejects_fabricated_or_synthetic_citation(self):
        box, event = self.prepared()
        for bad_id in ("E999", "SIM-E001"):
            payload = self.conclusion(event)
            payload["findings"][0]["evidence_ids"] = [bad_id]
            with self.assertRaises(ValueError):
                validate_conclusion(payload, box)

    def test_rejects_wrong_fault_code(self):
        box, event = self.prepared()
        payload = self.conclusion(event)
        payload["findings"][0]["code"] = "WRONG_CODE"
        with self.assertRaises(ValueError):
            validate_conclusion(payload, box)

    def test_no_fault_log_permits_empty_findings(self):
        box = toolbox("normal")
        for name in ("summarize_log", "list_protection_rules", "detect_faults"):
            box.invoke(name)
        validate_conclusion({"summary": "No configured rule matched", "findings": [], "limitations": [], "coverage": "full"}, box)

    def test_rejects_fabricated_numerical_and_temporal_facts(self):
        box, event = self.prepared()
        for key, bad in (("peak_value", 999.0), ("start_s", 99.0), ("confirmed_s", True), ("recovered_s", None), ("rule_id", "invented"), ("channel_id", "invented"), ("status", "active")):
            with self.subTest(key=key):
                payload = self.conclusion(event)
                payload["findings"][0]["facts"][0][key] = bad
                with self.assertRaises(ValueError):
                    validate_conclusion(payload, box)

    def test_rejects_duplicate_findings_or_fact_ids(self):
        box, event = self.prepared()
        payload = self.conclusion(event)
        payload["findings"].append(payload["findings"][0])
        with self.assertRaises(ValueError):
            validate_conclusion(payload, box)
        payload = self.conclusion(event)
        payload["findings"][0]["evidence_ids"].append(event["evidence_id"])
        payload["findings"][0]["facts"].append(canonical_event_fact(event))
        with self.assertRaises(ValueError):
            validate_conclusion(payload, box)

    def test_partial_is_explicit_and_false_full_is_rejected(self):
        box, event = self.prepared()
        payload = self.conclusion(event)
        validate_conclusion(payload, box)
        result = canonicalize_conclusion(payload, box)
        self.assertEqual(result["coverage"]["mode"], "partial")
        self.assertEqual(result["coverage"]["total_events"], 4)
        self.assertEqual(result["coverage"]["cited_events"], 1)
        self.assertEqual(result["coverage"]["remaining_events"], 3)
        self.assertTrue(result["coverage"]["deterministic_scan_complete"])
        payload["coverage"] = "full"
        with self.assertRaises(ValueError):
            validate_conclusion(payload, box)

    def test_full_requires_every_event_issued_and_cited(self):
        box, _ = self.prepared()
        events = box.analysis["events"]
        findings = [self.conclusion(event)["findings"][0] for event in events]
        payload = {"summary": "all", "findings": findings, "limitations": [], "coverage": "full"}
        result = canonicalize_conclusion(payload, box)
        self.assertEqual(result["coverage"]["mode"], "full")
        self.assertEqual(result["coverage"]["remaining_events"], 0)

    def test_canonical_output_discards_unverified_prose(self):
        box, event = self.prepared()
        payload = self.conclusion(event)
        invented = "All events checked; peak was 9999A due to an internal short circuit."
        payload["summary"] = invented
        payload["findings"][0]["explanation"] = invented
        payload["limitations"] = [invented]
        result = canonicalize_conclusion(payload, box)
        self.assertNotIn(invented, json.dumps(result))
        self.assertEqual(result["findings"][0]["facts"], [canonical_event_fact(event)])
        self.assertEqual(result["text_provenance"], "application_generated_from_verified_event_facts")


class ReportingTests(unittest.TestCase):
    def test_nested_redaction(self):
        sentinel = "private-key-for-a-unit-test-only"
        value = {sentinel: ["prefix " + sentinel, {"error": "sk-example_test_credentials"}]}
        result = json.dumps(redact(value, (sentinel,)))
        self.assertNotIn(sentinel, result)
        self.assertNotIn("sk-example_test_credentials", result)

    def test_report_artifacts_are_redacted_and_not_overwritten(self):
        box = toolbox()
        sentinel = "unit-test-only-secret-value"
        payload = {"mode": "offline", "status": "complete", "source_name": sentinel, "source_sha256": "f" * 64, "analysis": box.analysis, "question": sentinel, "tool_calls": [{"result": sentinel}], "usage": {"requests": 0}}
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".report-test-") as temp:
            directory = Path(temp) / "run"
            write_bundle(directory, payload, (sentinel,))
            for path in directory.iterdir():
                self.assertNotIn(sentinel, path.read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                write_bundle(directory, payload)


if __name__ == "__main__":
    unittest.main()
