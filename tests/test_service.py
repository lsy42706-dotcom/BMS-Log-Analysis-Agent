"""Application-level tests, with no SDK dependency or model requests."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bms_agent import service
from bms_agent.domain import load_csv_bytes, default_rules, Rule
from bms_agent.run_control import (TokenBudgetExceeded, checkpoint_state, classify_error,
                                  failure_details, load_checkpoint, replay_checkpoint, save_checkpoint)


CSV = "timestamp_s,cell_voltage_v,current_a,temperature_c\n0,3.7,55,25\n1,3.7,55,25\n2,3.7,55,25\n3,3.7,10,25\n4,3.7,10,25\n5,3.7,10,25\n"
ZERO_USAGE = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
              "complete": True, "unresolved_requests": 0}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        service._CACHE.clear()

    def test_offline_upload_canonical_coverage_and_export(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_OTHER_USER"}):
            response = service.analyze_request({"csv_text": CSV})
        report = response["report"]
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["usage"]["total_tokens"], 0)
        self.assertEqual(report["conclusion"]["coverage"]["mode"], "full")
        self.assertEqual(report["conclusion"]["findings"][0]["facts"][0]["peak_value"], 55)
        self.assertIn("证据覆盖", response["report_markdown"])
        self.assertNotIn("TEST_ONLY_OTHER_USER", json.dumps(response))

    def test_no_environment_key_fallback_for_agent(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_OTHER_USER"}), self.assertRaises(ValueError):
            service.analyze_request({"csv_text": CSV, "mode": "agent", "model": "test-model"})

    def test_cache_is_bounded_copy_isolated_and_config_sensitive(self):
        dataset = load_csv_bytes(CSV.encode())
        first = service.make_toolbox(dataset, default_rules())
        second = service.make_toolbox(dataset, default_rules())
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        second.analysis["events"].clear()
        self.assertTrue(service.make_toolbox(dataset, default_rules()).analysis["events"])
        changed = [Rule("oc", "OVERCURRENT", "current_a", "abs_high", 80, 70, 2, 2)]
        self.assertFalse(service.make_toolbox(dataset, changed).cache_hit)
        opposite = load_csv_bytes(CSV.encode(), current_convention="positive_charge")
        self.assertNotEqual(service.analysis_fingerprint(dataset, default_rules()), service.analysis_fingerprint(opposite, default_rules()))
        for threshold in range(81, 88):
            service.make_toolbox(dataset, [Rule("oc", "OVERCURRENT", "current_a", "abs_high", threshold, 70, 2, 2)])
        self.assertLessEqual(len(service._CACHE), service._CACHE_LIMIT)

    def test_cache_mapping_swap_same_bytes_changes_fingerprint(self):
        content = b"timestamp_s,current_a,left,right,temperature_c\n0,10,2.8,3.7,25\n"
        first = load_csv_bytes(content, column_map={"cell_voltage_v_C01": "left", "cell_voltage_v_C02": "right"})
        swapped = load_csv_bytes(content, column_map={"cell_voltage_v_C01": "right", "cell_voltage_v_C02": "left"})
        self.assertEqual(first.sha256, swapped.sha256)
        self.assertNotEqual(service.analysis_fingerprint(first, default_rules()), service.analysis_fingerprint(swapped, default_rules()))
        first_box = service.make_toolbox(first, default_rules())
        swapped_box = service.make_toolbox(swapped, default_rules())
        self.assertFalse(swapped_box.cache_hit)
        self.assertNotEqual(first_box.analysis["summary"]["stats"]["cell_voltage_v_C01"], swapped_box.analysis["summary"]["stats"]["cell_voltage_v_C01"])

    def test_window_is_offline_and_rejects_credentials(self):
        result = service.inspect_request({"csv_text": CSV, "start_s": 0, "end_s": 5})
        self.assertEqual(result["matched_samples"], 6)
        with self.assertRaises(ValueError):
            service.inspect_request({"csv_text": CSV, "api_key": "TEST_ONLY"})


class RunControlTests(unittest.TestCase):
    def prepared(self):
        box = service.make_toolbox(load_csv_bytes(CSV.encode()), default_rules())
        for tool in ("summarize_log", "list_protection_rules", "detect_faults"):
            box.invoke(tool)
        return box

    def test_checkpoint_recomputes_evidence_and_contains_no_key(self):
        box = self.prepared()
        state = checkpoint_state(box, "Check faults", "test-model", ZERO_USAGE, status="failed", secrets=("TEST_ONLY_PRIVATE_KEY",))
        state["tool_calls"][0]["result"] = {"events": [{"peak_value": 999}]}
        restored = service.make_toolbox(box.dataset, box.rules)
        history, usage = replay_checkpoint(state, restored, "Check faults", "test-model")
        self.assertEqual(restored.issued_evidence["E001"]["peak_value"], 55)
        self.assertEqual(len(history), 7)
        self.assertNotIn("999", json.dumps(history))
        self.assertEqual(usage["total_tokens"], 0)
        with tempfile.TemporaryDirectory(prefix=".service-test-", dir=Path(__file__).resolve().parents[1]) as temp:
            path = Path(temp) / "checkpoint.json"
            save_checkpoint(path, state)
            self.assertEqual(load_checkpoint(path), state)
            with self.assertRaises(FileExistsError):
                save_checkpoint(path, state)

    def test_checkpoint_rejects_input_rules_question_model_and_unknown_usage(self):
        box = self.prepared()
        state = checkpoint_state(box, "Check faults", "test-model", ZERO_USAGE, status="failed")
        for field, value in (("analysis_fingerprint", "tampered"), ("question_sha256", "tampered"), ("model_id", "other-model")):
            broken = {**state, field: value}
            with self.assertRaises(ValueError):
                replay_checkpoint(broken, service.make_toolbox(box.dataset, box.rules), "Check faults", "test-model")
        broken = copy.deepcopy(state)
        broken["usage"]["total_tokens"] = None
        with self.assertRaises(ValueError):
            replay_checkpoint(broken, service.make_toolbox(box.dataset, box.rules), "Check faults", "test-model")
        for complete, unresolved in ((False, None), (False, "x"), (True, 1), (False, True), (False, -1)):
            broken = copy.deepcopy(state)
            broken["usage"].update(complete=complete, unresolved_requests=unresolved)
            with self.assertRaises(ValueError):
                replay_checkpoint(broken, service.make_toolbox(box.dataset, box.rules), "Check faults", "test-model")

    def test_error_classification_never_echoes_provider_material(self):
        class ProviderError(Exception):
            status_code = 429
            body = {"error": {"code": "insufficient_quota", "message": "TEST_ONLY_PRIVATE_KEY"}}
        cases = [(TokenBudgetExceeded(), "token_budget"), (TimeoutError(), "timeout"),
                 (ProviderError(), "quota"), (ValueError(), "validation"), (RuntimeError(), "unexpected")]
        for exc, expected in cases:
            self.assertEqual(classify_error(exc)[0], expected)
            details = failure_details(exc)
            self.assertNotIn("TEST_ONLY_PRIVATE_KEY", json.dumps(details))
            self.assertFalse(details["automatic_retry"])
            self.assertIsNone(details["usage"]["total_tokens"])


if __name__ == "__main__":
    unittest.main()
