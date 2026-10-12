"""Manual-fixture detection grading plus offline SDK integration regression."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.cases import FIXTURES, load_cases
from evals.graders import grade_evidence, match_events, ratio
from evals.results import aggregate_metrics, markdown_report, write_results
from evals.runner import baseline_conclusion, grade_case, main, prepare_case, run_suite, sdk_mock_conclusion
from evals.sdk_mock import parse_tool_output

ROOT = Path(__file__).resolve().parents[1]
SDK_AVAILABLE = importlib.util.find_spec("agents") is not None


class CaseTests(unittest.TestCase):
    def test_loading_labels_does_not_call_detector_or_sample_generator(self):
        with patch("bms_agent.domain.analyze", side_effect=AssertionError("Labels cannot use detector")), \
                patch("bms_agent.samples.generate_samples", side_effect=AssertionError("Labels cannot use generated samples")):
            metadata, cases = load_cases()
        self.assertEqual(metadata["suite_version"], "1.0.0")
        self.assertEqual(len(cases), 17)
        self.assertEqual(len({case.case_id for case in cases}), len(cases))
        self.assertTrue(all(case.case_version == "1.0.0" and len(case.csv_sha256) == 64 for case in cases))
        self.assertTrue(all(case.time_tolerance_s == 0 for case in cases))

    def test_fixture_suite_passes_independent_manual_labels(self):
        payload = run_suite()
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["metrics"]["cases_passed"], 17)
        self.assertEqual(payload["metrics"]["tp"], 22)
        self.assertEqual(payload["metrics"]["fp"], 0)
        self.assertEqual(payload["metrics"]["fn"], 0)
        self.assertEqual(payload["metrics"]["precision"], 1)
        self.assertEqual(payload["metrics"]["recall"], 1)
        self.assertEqual(payload["real_model"], "not_measured")
        self.assertEqual(payload["api_calls"], 0)
        self.assertEqual(payload["charged_api_cost"], 0)
        self.assertTrue(all(result["usage"]["api_calls"] == 0 for result in payload["results"]))
        self.assertTrue(all(result["guards"]["passed"] for result in payload["results"]))

    def test_csv_prompt_like_strings_are_data_not_authority(self):
        _, cases = load_cases()
        case = next(case for case in cases if case.case_id == "prompt-like-cell-v1")
        box = prepare_case(case)
        conclusion = baseline_conclusion(box)
        graded = grade_case(case, box, conclusion, {"api_calls": 0})
        self.assertTrue(graded["prompt_data"]["tested"])
        self.assertTrue(graded["prompt_data"]["passed"])
        self.assertEqual(graded["prompt_data"]["instruction_markers_retained"], 0)
        self.assertEqual(box.analysis["events"][0]["code"], "SENSOR_INVALID")

    def test_modified_labels_are_not_replaced_with_detector_output(self):
        payload = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
        payload["cases"] = [next(case for case in payload["cases"] if case["case_id"] == "current-inclusive-v1")]
        payload["cases"][0]["expected_events"][0]["peak_value"] = 999.0
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".eval-test-") as temp:
            directory = Path(temp)
            shutil.copyfile(FIXTURES / "current_boundary.csv", directory / "current_boundary.csv")
            manifest = directory / "cases.json"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            result = run_suite(manifest)
            self.assertEqual(result["status"], "fail")
            self.assertFalse(result["results"][0]["events"]["passed"])
            self.assertEqual(main(["--manifest", str(manifest), "--output", str(directory / "result")]), 1)

    def test_fixture_paths_cannot_escape_manifest_directory(self):
        payload = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
        payload["cases"] = [payload["cases"][0]]
        payload["cases"][0]["csv"] = "../normal.csv"
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".eval-test-") as temp:
            manifest = Path(temp) / "cases.json"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(manifest)


class GraderTests(unittest.TestCase):
    def setUp(self):
        _, cases = load_cases()
        self.case = next(case for case in cases if case.case_id == "same-code-two-episodes-v1")
        self.expected = copy.deepcopy(self.case.expected_events)

    def test_one_to_one_matching_counts_duplicates_as_false_positives(self):
        actual = copy.deepcopy(self.expected) + [copy.deepcopy(self.expected[0])]
        grade = match_events(actual, self.expected, 0)
        self.assertEqual((grade["tp"], grade["fp"], grade["fn"]), (2, 1, 0))
        self.assertAlmostEqual(grade["precision"], 2 / 3)
        self.assertFalse(grade["passed"])

    def test_wrong_time_or_channel_is_not_a_true_positive(self):
        for key, value in (("confirmed_s", 4), ("recovered_s", None), ("channel_id", "wrong"), ("status", "active")):
            actual = copy.deepcopy(self.expected)
            actual[0][key] = value
            with self.subTest(key=key):
                grade = match_events(actual, self.expected, 0)
                self.assertEqual((grade["tp"], grade["fp"], grade["fn"]), (1, 1, 1))

    def test_timing_tolerance_is_explicit(self):
        actual = copy.deepcopy(self.expected)
        actual[0]["confirmed_s"] += 0.1
        self.assertFalse(match_events(actual, self.expected, 0)["passed"])
        self.assertTrue(match_events(actual, self.expected, 0.11)["passed"])

    def test_numerical_fact_error_fails_even_when_events_match(self):
        actual = copy.deepcopy(self.expected)
        actual[0]["peak_value"] = 999
        grade = match_events(actual, self.expected, 0)
        self.assertEqual(grade["tp"], 2)
        self.assertEqual(grade["peak_checks_passed"], 1)
        self.assertFalse(grade["passed"])

    def test_missing_or_unsupported_fault_counts_are_not_hidden(self):
        self.assertEqual(match_events([], self.expected, 0)["fn"], 2)
        self.assertEqual(match_events(self.expected, [], 0)["fp"], 2)
        empty = match_events([], [], 0)
        self.assertIsNone(empty["precision"])
        self.assertIsNone(empty["recall"])
        self.assertTrue(empty["passed"])
        self.assertIsNone(ratio(0, 0))

    def test_evidence_and_tool_sequence_grader_detects_tampering(self):
        box = prepare_case(self.case)
        conclusion = baseline_conclusion(box)
        self.assertTrue(grade_evidence(conclusion, box)["passed"])
        tampered = copy.deepcopy(conclusion)
        tampered["findings"][0]["facts"][0]["peak_value"] = 999
        self.assertFalse(grade_evidence(tampered, box)["passed"])
        tampered = copy.deepcopy(conclusion)
        tampered["coverage"]["cited_events"] = 999
        self.assertFalse(grade_evidence(tampered, box)["passed"])
        tampered = copy.deepcopy(conclusion)
        tampered["findings"][0]["facts"] = []
        self.assertFalse(grade_evidence(tampered, box)["passed"])
        tampered = copy.deepcopy(conclusion)
        tampered["findings"][0]["code"] = "INVENTED"
        self.assertFalse(grade_evidence(tampered, box)["passed"])
        tampered = copy.deepcopy(conclusion)
        tampered["summary"] = "All events prove a physical short circuit."
        self.assertFalse(grade_evidence(tampered, box)["canonical_text_only"])
        box.calls[0], box.calls[1] = box.calls[1], box.calls[0]
        self.assertFalse(grade_evidence(conclusion, box)["tool_sequence_valid"])

    def test_required_rejection_guards_are_actually_exercised(self):
        box = prepare_case(self.case)
        result = grade_case(self.case, box, baseline_conclusion(box), {"api_calls": 0})
        checks = result["guards"]["checks"]
        self.assertEqual(set(checks), {"fabricated_peak_rejected", "fabricated_time_rejected", "unissued_citation_rejected", "duplicate_citation_rejected", "false_full_coverage_rejected"})
        self.assertTrue(all(checks.values()))

    def test_mock_output_parser_requires_explicit_json_or_dict(self):
        output = {"total": 0, "events": [], "next_offset": None}
        self.assertEqual(parse_tool_output(output), output)
        self.assertEqual(parse_tool_output(json.dumps(output)), output)
        self.assertIsNone(parse_tool_output(str(output)))
        self.assertIsNone(parse_tool_output("Fixed safe tool failure"))
        self.assertIsNone(parse_tool_output("[]"))


class ResultTests(unittest.TestCase):
    def test_outputs_do_not_overwrite_and_distinguish_real_model(self):
        payload = run_suite()
        with tempfile.TemporaryDirectory(dir=ROOT, prefix=".eval-test-") as temp:
            output = Path(temp) / "new"
            write_results(output, payload)
            loaded = json.loads((output / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(loaded["real_model"], "not_measured")
            text = (output / "report.md").read_text(encoding="utf-8")
            self.assertIn("not measured", text)
            self.assertIn("API calls and charged API cost: 0", text)
            self.assertIn("not a real-battery", text)
            with self.assertRaises(FileExistsError):
                write_results(output, payload)

    def test_unexecuted_cases_fail_without_invented_event_metrics(self):
        metrics = aggregate_metrics([{"passed": False, "error_type": "ImportError"}])
        self.assertEqual(metrics["cases_passed"], 0)
        self.assertEqual(metrics["cases_total"], 1)
        self.assertEqual(metrics["cases_graded_for_event_metrics"], 0)
        self.assertIsNone(metrics["precision"])
        self.assertIsNone(metrics["recall"])


@unittest.skipUnless(SDK_AVAILABLE, "Install optional .[agent] for local SDK-mock evaluation")
class EvalSDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_sdk_runner_uses_mock_tools_and_exact_structured_facts(self):
        _, cases = load_cases()
        case = next(case for case in cases if case.case_id == "same-code-two-episodes-v1")
        box = prepare_case(case)
        conclusion, usage = await sdk_mock_conclusion(box)
        result = grade_case(case, box, conclusion, usage)
        self.assertTrue(result["passed"])
        self.assertTrue(usage["simulated"])
        self.assertFalse(usage["tokens_measured"])
        self.assertEqual(usage["api_calls"], 0)
        self.assertEqual(usage["requests"], 4)
        self.assertEqual(usage["charged_cost"], 0)
        self.assertEqual(conclusion["coverage"]["cited_events"], 2)


if __name__ == "__main__":
    unittest.main()
