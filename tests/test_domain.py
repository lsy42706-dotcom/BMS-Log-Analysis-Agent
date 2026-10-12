"""Offline verification of timing, input quality, and evidence provenance."""

import json
import math
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from bms_agent.domain import (Dataset, DatasetError, QualityConfig, Rule, RulesError, Sample,
                              analyze, default_rules, load_csv, load_profile, load_rules,
                              profile_from_dict, sample_channels, sample_readings, sensor_channels)
from bms_agent.samples import SCENARIOS, generate_samples, main as samples_main, write_sample


def dataset(currents, timestamps=None, temperatures=None, voltages=None):
    timestamps = list(range(len(currents))) if timestamps is None else timestamps
    temperatures = [25.0] * len(currents) if temperatures is None else temperatures
    voltages = [3.7] * len(currents) if voltages is None else voltages
    return Dataset([Sample(float(t), v, c, temp, index + 2)
                    for index, (t, v, c, temp) in enumerate(zip(timestamps, voltages, currents, temperatures))],
                   [], "synthetic.csv", "0" * 64)


def faults(result, code="OVERCURRENT"):
    return [event for event in result["events"] if event["code"] == code]


class TimingTests(unittest.TestCase):
    def test_threshold_equality_and_first_last_samples(self):
        result = analyze(dataset([50, 50, 50]))
        event = faults(result)[0]
        self.assertEqual((event["start_s"], event["confirmed_s"]), (0.0, 2.0))
        self.assertEqual((event["row_start"], event["row_confirm"]), (2, 4))
        self.assertEqual(event["status"], "active")
        self.assertIsNone(event["recovered_s"])
        self.assertEqual(result["summary"]["sample_count"], 3)

    def test_confirmation_is_elapsed_time_not_count(self):
        self.assertFalse(faults(analyze(dataset([51] * 4, [0, 0.1, 0.2, 0.3]))))
        event = faults(analyze(dataset([51] * 4, [0, 0.5, 1.0, 2.0])))[0]
        self.assertEqual(event["confirmed_s"], 2.0)

    def test_negative_current_uses_magnitude_preserving_raw_evidence(self):
        event = faults(analyze(dataset([-50, -55, -51])))[0]
        self.assertEqual(event["trigger_value"], -50.0)
        self.assertEqual(event["peak_value"], -55.0)

    def test_hysteresis_and_recovery_equality(self):
        event = faults(analyze(dataset([50, 52, 51, 47, 45, 45, 45])))[0]
        self.assertEqual(event["recovered_s"], 6.0)
        self.assertEqual(event["row_recover"], 8)
        self.assertEqual(event["peak_value"], 52.0)

    def test_recovery_timer_restarts(self):
        event = faults(analyze(dataset([50, 50, 50, 45, 47, 45, 45, 45])))[0]
        self.assertEqual(event["recovered_s"], 7.0)

    def test_gap_resets_pending_confirmation(self):
        result = analyze(dataset([50, 50, 50, 50, 50], [0, 1, 4, 5, 6]))
        event = faults(result)[0]
        self.assertEqual((event["start_s"], event["confirmed_s"]), (4.0, 6.0))
        gap = faults(result, "SAMPLE_GAP")[0]
        self.assertEqual((gap["start_s"], gap["confirmed_s"], gap["trigger_value"]), (1.0, 4.0, 3.0))

    def test_gap_resets_recovery_but_keeps_fault_latched(self):
        event = faults(analyze(dataset([50, 50, 50, 45, 45, 45, 45], [0, 1, 2, 3, 6, 7, 8])))[0]
        self.assertEqual(event["recovered_s"], 8.0)
        self.assertEqual(event["start_s"], 0.0)

    def test_invalid_resets_pending_and_recovery(self):
        result = analyze(dataset([50, 50, None, 50, 50, 50, 45, None, 45, 45, 45]))
        event = faults(result)[0]
        self.assertEqual((event["start_s"], event["confirmed_s"], event["recovered_s"]), (3.0, 5.0, 10.0))
        self.assertEqual(len(faults(result, "SENSOR_INVALID")), 2)

    def test_voltage_and_temperature_thresholds(self):
        result = analyze(dataset([10] * 4, temperatures=[60] * 4, voltages=[3.0] * 4))
        self.assertEqual(faults(result, "OVERTEMPERATURE")[0]["confirmed_s"], 3.0)
        self.assertEqual(faults(result, "UNDERVOLTAGE")[0]["confirmed_s"], 2.0)
        result = analyze(dataset([10] * 3, voltages=[4.25] * 3))
        self.assertEqual(faults(result, "OVERVOLTAGE")[0]["confirmed_s"], 2.0)

    def test_custom_zero_confirmation_and_recovery(self):
        rule = Rule("instant", "INSTANT", "current_a", "high", 20, 15, 0, 0)
        event = faults(analyze(dataset([20, 15]), [rule]), "INSTANT")[0]
        self.assertEqual((event["confirmed_s"], event["recovered_s"]), (0.0, 1.0))

    def test_repeated_faults_and_no_confirmation_after_last_sample(self):
        result = analyze(dataset([50, 50, 50, 10, 10, 10, 50, 50, 50, 10, 10, 10, 50]))
        self.assertEqual(len(faults(result)), 2)
        self.assertTrue(all(event["status"] == "recovered" for event in faults(result)))


class QualityTests(unittest.TestCase):
    def test_nonfinite_sensor_values_never_reach_json(self):
        result = analyze(dataset([math.nan, math.inf, -math.inf, None]))
        json.dumps(result, allow_nan=False)
        event = faults(result, "SENSOR_INVALID")[0]
        self.assertEqual(event["status"], "active")
        self.assertIsNone(result["summary"]["stats"]["current_a"]["mean"])
        self.assertEqual(result["summary"]["quality_issue_count"], 4)

    def test_unsorted_duplicate_or_nonfinite_timestamps_rejected(self):
        for timestamps in ([0, 0], [1, 0], [0, math.nan], [0, math.inf], [-1e308, 1e308]):
            with self.subTest(timestamps=timestamps), self.assertRaises(DatasetError):
                analyze(dataset([10, 10], timestamps))

    def test_sensor_event_recovers_on_first_valid_reading(self):
        event = faults(analyze(dataset([None, None, 10])), "SENSOR_INVALID")[0]
        self.assertEqual((event["confirmed_s"], event["recovered_s"]), (0.0, 2.0))

    def test_evidence_ids_timeline_and_determinism(self):
        data = Dataset(generate_samples("mixed"), [], "mixed.csv", "0" * 64)
        first = analyze(data)
        self.assertEqual(first, analyze(data))
        self.assertEqual([event["start_s"] for event in first["events"]], sorted(event["start_s"] for event in first["events"]))
        self.assertEqual([event["evidence_id"] for event in first["events"]], [f"E{index:03d}" for index in range(1, len(first["events"]) + 1)])
        self.assertEqual(first["summary"]["fault_event_count"], 3)
        self.assertEqual(first["summary"]["active_fault_count"], 0)

    def test_large_finite_values_have_finite_statistics(self):
        for values in ([1e308, 1e308, -1e308], [sys.float_info.max] * 3):
            with self.subTest(values=values):
                result = analyze(dataset(values))
                json.dumps(result, allow_nan=False)
                self.assertTrue(math.isfinite(result["summary"]["stats"]["current_a"]["mean"]))

    def test_single_sample_normal_and_invalid(self):
        self.assertEqual(analyze(dataset([10]))["summary"]["duration_s"], 0.0)
        self.assertFalse(faults(analyze(dataset([50]))))
        self.assertEqual(len(faults(analyze(dataset([None])), "SENSOR_INVALID")), 1)


class CsvAndRuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".domain-test-", dir=Path(__file__).resolve().parents[1])
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def csv(self, text):
        path = self.folder / "data.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_csv_missing_required_column_or_no_data(self):
        for text in ("timestamp_s,current_a\n0,10\n", "timestamp_s,cell_voltage_v,current_a,temperature_c\n", ""):
            with self.subTest(text=text), self.assertRaises(DatasetError):
                load_csv(self.csv(text))

    def test_extra_columns_are_ignored_and_raw_invalid_cells_not_retained(self):
        secret = "UNTRUSTED_STRING_DO_NOT_ECHO"
        data = load_csv(self.csv("timestamp_s,cell_voltage_v,current_a,temperature_c,note\n"
                                 f"0,3.7,{secret},25,{secret}\n1,3.7,NaN,25,test\n2,3.7,Infinity,25,test\n"))
        self.assertTrue(all(sample.current_a is None for sample in data.samples))
        self.assertEqual(len(data.quality_issues), 3)
        self.assertEqual(len(data.sha256), 64)
        self.assertNotIn(secret, repr(data))
        self.assertNotIn(secret, json.dumps(analyze(data), allow_nan=False))

    def test_invalid_timestamp_safe_error(self):
        secret = "UNTRUSTED_STRING_DO_NOT_ECHO"
        with self.assertRaises(DatasetError) as caught:
            load_csv(self.csv(f"timestamp_s,cell_voltage_v,current_a,temperature_c\n{secret},3.7,10,25\n"))
        self.assertIn("Row 2", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))

    def test_csv_out_of_order(self):
        with self.assertRaises(DatasetError):
            load_csv(self.csv("timestamp_s,cell_voltage_v,current_a,temperature_c\n1,3.7,10,25\n0,3.7,10,25\n"))

    def test_duplicate_header(self):
        with self.assertRaises(DatasetError):
            load_csv(self.csv("timestamp_s,cell_voltage_v,current_a,temperature_c,current_a\n0,3.7,10,25,10\n"))

    def test_byte_and_sample_limits(self):
        with self.assertRaises(DatasetError):
            load_csv(self.csv("x" * (5 * 1024 * 1024 + 1)))
        body = "timestamp_s,cell_voltage_v,current_a,temperature_c\n" + "".join(f"{index},3.7,10,25\n" for index in range(20001))
        with self.assertRaises(DatasetError):
            load_csv(self.csv(body))

    def test_rule_configuration_round_trip(self):
        payload = analyze(dataset([10]))["rules"]
        path = self.folder / "rules.json"
        path.write_text(json.dumps({"rules": payload}), encoding="utf-8")
        self.assertEqual(load_rules(path), default_rules())
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(load_rules(path), default_rules())

    def test_unsafe_or_invalid_rules_are_rejected(self):
        variants = [{"threshold": math.nan}, {"confirm_s": -1}, {"max_gap_s": 0},
                    {"recovery_threshold": 60}, {"direction": "bad"},
                    {"field": "note"}, {"code": "untrusted instruction"}]
        payload = analyze(dataset([10]))["rules"][0]
        path = self.folder / "rules.json"
        for variant in variants:
            with self.subTest(variant=variant):
                path.write_text(json.dumps([{**payload, **variant}]), encoding="utf-8")
                with self.assertRaises(RulesError):
                    load_rules(path)

    def test_generated_scenarios_and_cli(self):
        for scenario in SCENARIOS:
            path = write_sample(self.folder / f"{scenario}.csv", scenario)
            data = load_csv(path)
            self.assertEqual(len(data.samples), 40)
            json.dumps(analyze(data), allow_nan=False)
        self.assertEqual(samples_main(["--output", str(self.folder / "all"), "--scenario", "all"]), 0)
        self.assertEqual(len(list((self.folder / "all").glob("*.csv"))), 7)
        self.assertTrue((self.folder / "all" / "demo.csv").exists())


def wide_dataset(currents, cells=None, probes=None, timestamps=None, convention="positive_discharge"):
    count = len(currents)
    cells = [{"C01": 3.7, "C02": 3.8}] * count if cells is None else cells
    probes = [{"T01": 25.0, "T02": 26.0}] * count if probes is None else probes
    timestamps = list(range(count)) if timestamps is None else timestamps
    return Dataset([Sample(float(t), None, current, None, index + 2, cell, probe)
                    for index, (t, current, cell, probe) in enumerate(zip(timestamps, currents, cells, probes))],
                   [], "wide.csv", "0" * 64, "pack_demo", convention)


class MultiChannelTests(unittest.TestCase):
    def test_legacy_constructors_remain_compatible(self):
        sample = Sample(0, 3.7, 10, 25, 2)
        self.assertEqual(sample.cell_voltages_v, {})
        self.assertEqual(Dataset([sample], [], "legacy.csv", "0" * 64).pack_id, "pack_1")
        self.assertIsNone(default_rules()[0].channel_id)
        self.assertEqual(default_rules()[0].current_mode, "any")

    def test_per_channel_faults_with_shared_pack_current_once(self):
        cells = [{"C01": 2.8, "C02": 2.9}] * 4
        probes = [{"T01": 61, "T02": 62}] * 4
        result = analyze(wide_dataset([60] * 4, cells, probes))
        self.assertEqual(len(faults(result)), 1)
        self.assertEqual({event["cell_id"] for event in faults(result, "UNDERVOLTAGE")}, {"C01", "C02"})
        self.assertEqual({event["probe_id"] for event in faults(result, "OVERTEMPERATURE")}, {"T01", "T02"})
        self.assertTrue(all(event["pack_id"] == "pack_demo" for event in result["events"]))
        self.assertIsNone(faults(result)[0]["channel_id"])
        self.assertEqual(result["summary"]["quality_issue_count"], 0)
        self.assertEqual(len(result["summary"]["channels"]), 5)

    def test_channel_helpers_and_missing_channel_union(self):
        data = wide_dataset([10, 10], cells=[{"C01": 3.7}, {"C01": 3.8, "C02": 3.9}])
        self.assertEqual(sample_channels(data.samples[0]), ["cell_voltage_v_C01", "current_a", "temperature_c_T01", "temperature_c_T02"])
        self.assertEqual(sample_readings(data.samples[0])["cell_voltage_v_C01"], 3.7)
        self.assertIn("cell_voltage_v_C02", sensor_channels(data))
        result = analyze(data)
        missing = faults(result, "SENSOR_INVALID")[0]
        self.assertEqual(missing["cell_id"], "C02")
        self.assertEqual(missing["recovered_s"], 1.0)

    def test_channel_specific_and_canonical_rules(self):
        data = wide_dataset([10] * 3, cells=[{"C01": 2.8, "C02": 2.9}] * 3)
        rule = Rule("targeted", "TARGETED", "cell_voltage_v", "low", 3, 3.2, 2, 0, channel_id="C02")
        result = analyze(data, [rule])
        self.assertEqual([event["cell_id"] for event in faults(result, "TARGETED")], ["C02"])
        direct = Rule("direct", "DIRECT", "cell_voltage_v_C01", "low", 3, 3.2, 2, 0)
        self.assertEqual(faults(analyze(data, [direct]), "DIRECT")[0]["field"], "cell_voltage_v_C01")
        with self.assertRaises(RulesError):
            analyze(data, [Rule("missing", "MISSING", "cell_voltage_v", "low", 3, 3.2, 0, 0, channel_id="C03")])

    def test_charge_discharge_rules_and_sign_convention(self):
        charge = Rule("charge", "CHARGE_OVERCURRENT", "current_a", "abs_high", 50, 40, 0, 0, current_mode="charge")
        discharge = Rule("discharge", "DISCHARGE_OVERCURRENT", "current_a", "abs_high", 80, 70, 0, 0, current_mode="discharge")
        result = analyze(wide_dataset([0, -20, -60, -30, 100]), [charge, discharge])
        charged = faults(result, "CHARGE_OVERCURRENT")[0]
        self.assertEqual((charged["start_s"], charged["recovered_s"], charged["peak_value"]), (2.0, 3.0, -60.0))
        self.assertEqual(faults(result, "DISCHARGE_OVERCURRENT")[0]["peak_value"], 100)
        counts = result["summary"]["phase_sample_counts"]
        self.assertEqual(counts, {"charge": 3, "discharge": 1, "idle": 1, "unknown": 0})
        reversed_sign = analyze(wide_dataset([0, 60, -100], convention="positive_charge"), [charge, discharge])
        self.assertEqual(faults(reversed_sign, "CHARGE_OVERCURRENT")[0]["peak_value"], 60)
        self.assertEqual(faults(reversed_sign, "DISCHARGE_OVERCURRENT")[0]["peak_value"], -100)

    def test_opposite_phase_cannot_change_peak_or_trigger_zero_threshold(self):
        charge = Rule("charge", "CHARGE", "current_a", "abs_high", 50, 40, 0, 0, current_mode="charge")
        event = faults(analyze(dataset([-60, 1000]), [charge]), "CHARGE")[0]
        self.assertEqual(event["peak_value"], -60)
        self.assertEqual(event["recovered_s"], 1)
        zero = Rule("zero", "ZERO", "current_a", "abs_high", 0, 0, 0, 0, current_mode="charge")
        self.assertFalse(faults(analyze(dataset([10, 0]), [zero]), "ZERO"))

    def test_spread_timers_and_missing_channel_prevent_false_recovery(self):
        rule = Rule("spread", "CELL_IMBALANCE", "cell_spread_v", "high", 0.2, 0.1, 1, 0)
        cells = [{"C01": 3.5, "C02": 3.8}, {"C01": 3.5, "C02": 3.8},
                 {"C01": 3.7, "C02": None}, {"C01": 3.7, "C02": 3.75}]
        event = faults(analyze(wide_dataset([10] * 4, cells), [rule]), "CELL_IMBALANCE")[0]
        self.assertEqual((event["start_s"], event["confirmed_s"], event["recovered_s"]), (0, 1, 3))
        self.assertAlmostEqual(event["peak_value"], 0.3)
        self.assertIsNone(event["cell_id"])
        thermal = Rule("temp_spread", "TEMP_SPREAD", "temperature_spread_c", "high", 5, 3, 0, 0)
        event = faults(analyze(wide_dataset([10], probes=[{"T01": 25, "T02": 35}]), [thermal]), "TEMP_SPREAD")[0]
        self.assertEqual(event["peak_value"], 10)

    def test_single_channel_spread_not_silently_evaluated(self):
        rule = Rule("spread", "SPREAD", "cell_spread_v", "high", 0.2, 0.1, 0, 0)
        result = analyze(dataset([10]), [rule])
        self.assertEqual(result["summary"]["not_evaluated_rules"], ["spread"])

    def test_quality_disabled_unless_explicitly_configured(self):
        data = dataset([10] * 5, voltages=[7] * 5)
        result = analyze(data)
        self.assertFalse(faults(result, "SENSOR_IMPLAUSIBLE"))
        self.assertFalse(faults(result, "SENSOR_STUCK"))
        self.assertFalse(faults(result, "SENSOR_SPIKE"))

    def test_bounds_keep_raw_evidence_and_recover(self):
        quality = QualityConfig(bounds={"cell_voltage_v": (0, 5)})
        result = analyze(dataset([10] * 3, voltages=[6, 7, 3.7]), quality=quality)
        event = faults(result, "SENSOR_IMPLAUSIBLE")[0]
        self.assertEqual((event["trigger_value"], event["peak_value"], event["recovered_s"]), (6, 7, 2))
        self.assertEqual(result["summary"]["quality_event_count"], 1)
        self.assertEqual(result["summary"]["quality_issue_count"], 0)

    def test_stuck_is_sampled_candidate_and_recovers_on_change(self):
        quality = QualityConfig(stuck_s={"cell_voltage_v": 2}, stuck_epsilon={"cell_voltage_v": 0.01})
        event = faults(analyze(dataset([10] * 5, voltages=[3.7, 3.705, 3.7, 3.7, 3.8]), quality=quality), "SENSOR_STUCK")[0]
        self.assertEqual((event["start_s"], event["confirmed_s"], event["recovered_s"]), (0, 2, 4))
        self.assertIn("stable real conditions", event["message"])

    def test_stuck_pending_resets_on_gap_or_missing(self):
        quality = QualityConfig(stuck_s={"cell_voltage_v": 2})
        event = faults(analyze(dataset([10] * 5, timestamps=[0, 1, 10, 11, 12]), quality=quality), "SENSOR_STUCK")[0]
        self.assertEqual((event["start_s"], event["confirmed_s"]), (10, 12))
        event = faults(analyze(dataset([10] * 6, voltages=[3.7, 3.7, None, 3.7, 3.7, 3.7]), quality=quality), "SENSOR_STUCK")[0]
        self.assertEqual((event["start_s"], event["confirmed_s"]), (3, 5))

    def test_spikes_ignore_gaps_and_preserve_finite_json(self):
        quality = QualityConfig(max_rate={"cell_voltage_v": 0.5})
        result = analyze(dataset([10] * 3, voltages=[3.7, 4.7, 3.7]), quality=quality)
        self.assertEqual(len(faults(result, "SENSOR_SPIKE")), 2)
        self.assertAlmostEqual(faults(result, "SENSOR_SPIKE")[0]["rate_per_s"], 1)
        result = analyze(dataset([10, 10], timestamps=[0, 5], voltages=[3.7, 6]), quality=quality)
        self.assertFalse(faults(result, "SENSOR_SPIKE"))
        result = analyze(dataset([1e308, -1e308]), quality=QualityConfig(max_rate={"current_a": 1}))
        event = faults(result, "SENSOR_SPIKE")[0]
        self.assertIsNone(event["rate_per_s"])
        self.assertTrue(event["rate_exceeds_numeric_range"])
        json.dumps(result, allow_nan=False)

    def test_channel_quality_overrides_generic_config(self):
        quality = QualityConfig(bounds={"cell_voltage_v": (0, 5), "cell_voltage_v_C02": (0, 4)})
        result = analyze(wide_dataset([10], cells=[{"C01": 4.5, "C02": 4.5}]), quality=quality)
        self.assertEqual([event["cell_id"] for event in faults(result, "SENSOR_IMPLAUSIBLE")], ["C02"])

    def test_invalid_channel_quality_and_modes_rejected(self):
        invalid = [QualityConfig(bounds={"current_a": (5, 0)}), QualityConfig(max_rate={"note": 5}),
                   QualityConfig(stuck_s={"current_a": 0}), QualityConfig(stuck_epsilon={"current_a": -1}),
                   QualityConfig(max_gap_s=0), QualityConfig(max_rate={"cell_voltage_v_C99": 1})]
        for quality in invalid:
            with self.subTest(quality=quality), self.assertRaises(RulesError):
                analyze(wide_dataset([10]), quality=quality)
        with self.assertRaises(RulesError):
            analyze(dataset([10]), [Rule("bad", "BAD", "cell_voltage_v", "abs_high", 5, 4, 0, 0, current_mode="charge")])
        with self.assertRaises(RulesError):
            profile_from_dict({"rules": [default_rules()[0].__dict__], "quailty": {}})

    def test_analysis_work_and_event_limits(self):
        data = wide_dataset([10], cells=[{f"C{index}": 3.7 for index in range(40)}])
        rules = [Rule(f"rule{index}", "TEST", "cell_voltage_v", "high", 4, 3, 0, 0) for index in range(7)]
        with self.assertRaises(RulesError):
            analyze(data, rules)
        with patch("bms_agent.domain.MAX_EVENTS", 2), self.assertRaises(DatasetError):
            analyze(dataset([None, 10, None, 10, None]))
        with self.assertRaises(DatasetError):
            analyze(wide_dataset([10], cells=[{f"C{index}": 3.7 for index in range(64)}]))
        samples = [Sample(index, None, 10, None, index + 2, {f"C{index}": 3.7}, {"T01": 25})
                   for index in range(64)]
        with self.assertRaises(DatasetError):
            analyze(Dataset(samples, [], "changing_channels.csv", "0" * 64))

    def test_profile_loading_roundtrip_and_illustrative_fixture(self):
        root = Path(__file__).resolve().parents[1]
        rules, quality = load_profile(root / "rules" / "multichannel-example.json")
        self.assertEqual(load_rules(root / "rules" / "multichannel-example.json"), rules)
        self.assertTrue(quality.bounds)
        copied = profile_from_dict({"rules": [rule.__dict__ for rule in rules], "quality": quality.__dict__})
        self.assertEqual(copied, (rules, quality))
        result = analyze(load_csv(root / "samples" / "multichannel.csv"), rules, quality)
        self.assertTrue(faults(result, "CELL_IMBALANCE"))
        self.assertTrue(faults(result, "TEMPERATURE_SPREAD"))
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
