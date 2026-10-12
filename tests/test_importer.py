"""Offline wide-schema import, column/unit mapping and defensive input checks."""

import json
import math
import unittest

from bms_agent.domain import DatasetError, MAX_SENSOR_CHANNELS, analyze, load_csv_bytes, sample_readings, sensor_channels


LEGACY = b"timestamp_s,cell_voltage_v,current_a,temperature_c\n0,3.7,10,25\n1,3.8,11,26\n"
WIDE = b"timestamp_s,current_a,cell_voltage_v_C01,cell_voltage_v_C02,temperature_c_T01,temperature_c_T02\n0,10,3.7,3.8,25,26\n1,11,3.6,3.9,26,27\n"


class ImporterTests(unittest.TestCase):
    def test_legacy_compatibility_and_metadata(self):
        data = load_csv_bytes(LEGACY, "legacy.csv")
        self.assertEqual(data.samples[0].cell_voltage_v, 3.7)
        self.assertEqual(data.samples[0].cell_voltages_v, {})
        self.assertEqual(sensor_channels(data), ["cell_voltage_v", "current_a", "temperature_c"])
        self.assertEqual(data.import_metadata["source_units"]["temperature_c"], "degC")

    def test_wide_channels_do_not_invent_legacy_aliases(self):
        data = load_csv_bytes(WIDE, "wide.csv", pack_id="pack_demo")
        self.assertIsNone(data.samples[0].cell_voltage_v)
        self.assertIsNone(data.samples[0].temperature_c)
        self.assertEqual(data.samples[0].cell_voltages_v, {"C01": 3.7, "C02": 3.8})
        self.assertNotIn("cell_voltage_v", sample_readings(data.samples[0]))
        result = analyze(data)
        self.assertEqual(result["summary"]["quality_issue_count"], 0)
        self.assertEqual(result["summary"]["channels"]["cell_voltage_v_C02"]["unit"], "V")
        self.assertEqual(result["summary"]["pack_id"], "pack_demo")

    def test_mapping_and_unit_normalization(self):
        content = b"Time,U1,U2,I,T1,T2,note\n0,3700,3800,-10000,298.15,299.15,ignored\n1000,3600,3900,0,299.15,300.15,ignored\n"
        mapping = {"timestamp_s": "Time", "cell_voltage_v_C01": "U1", "cell_voltage_v_C02": "U2",
                   "current_a": "I", "temperature_c_T01": "T1", "temperature_c_T02": "T2"}
        units = {"timestamp_s": "ms", "cell_voltage_v": "mV", "current_a": "mA", "temperature_c": "K"}
        data = load_csv_bytes(content, "mapped.csv", mapping, units, current_convention="positive_charge")
        self.assertEqual(data.samples[1].timestamp_s, 1.0)
        self.assertAlmostEqual(data.samples[0].cell_voltages_v["C01"], 3.7)
        self.assertEqual(data.samples[0].current_a, -10.0)
        self.assertAlmostEqual(data.samples[0].temperatures_c["T01"], 25.0)
        self.assertEqual(analyze(data)["summary"]["phase_sample_counts"]["discharge"], 1)

    def test_units_c_alias_and_channel_override(self):
        data = load_csv_bytes(WIDE, units={"temperature_c": "C", "cell_voltage_v": "mV", "cell_voltage_v_C02": "V"})
        self.assertAlmostEqual(data.samples[0].cell_voltages_v["C01"], 0.0037)
        self.assertEqual(data.samples[0].cell_voltages_v["C02"], 3.8)
        self.assertEqual(data.samples[0].temperatures_c["T01"], 25.0)

    def test_source_bindings_remain_distinct_for_identical_bytes(self):
        content = b"Time,V1,V2,I,T\n0,3.6,3.9,10,25\n"
        mapping = {"timestamp_s": "Time", "cell_voltage_v_C01": "V1", "cell_voltage_v_C02": "V2",
                   "current_a": "I", "temperature_c": "T"}
        first = load_csv_bytes(content, column_map=mapping)
        second = load_csv_bytes(content, column_map={**mapping, "cell_voltage_v_C01": "V2", "cell_voltage_v_C02": "V1"})
        self.assertEqual(first.sha256, second.sha256)
        self.assertNotEqual(first.import_metadata, second.import_metadata)
        self.assertEqual(first.import_metadata["column_map"], mapping)
        self.assertNotEqual(first.samples[0].cell_voltages_v, second.samples[0].cell_voltages_v)

    def test_missing_invalid_wide_cells_are_safe(self):
        secret = "RAW_SENSOR_SECRET_DO_NOT_RETAIN"
        content = WIDE.decode().splitlines()[0] + f"\n0,10,{secret},3.7,NaN,25\n1,10,,3.7,Inf,25\n"
        data = load_csv_bytes(content.encode())
        self.assertEqual(len(data.quality_issues), 4)
        self.assertNotIn(secret, repr(data))
        self.assertNotIn(secret, json.dumps(analyze(data), allow_nan=False))
        self.assertTrue(all(sample.cell_voltages_v["C01"] is None for sample in data.samples))

    def test_missing_legacy_column_remains_present_with_wide_columns(self):
        content = b"timestamp_s,current_a,cell_voltage_v,cell_voltage_v_C01,temperature_c\n0,10,,3.7,25\n"
        data = load_csv_bytes(content)
        self.assertIn("cell_voltage_v", sensor_channels(data))
        self.assertEqual(analyze(data)["summary"]["quality_issue_count"], 1)

    def test_conversion_nonfinite_and_timestamp_collisions(self):
        for content in (LEGACY.replace(b"0,3.7", b"NaN,3.7"),
                        LEGACY.replace(b"1,3.8", b"0,3.8")):
            with self.subTest(content=content), self.assertRaises(DatasetError):
                load_csv_bytes(content, units={"timestamp_s": "ms"})

    def test_invalid_mappings_units_and_schema_are_rejected(self):
        cases = [
            {"column_map": {"cell_voltage_v": "missing"}},
            {"column_map": {"not_a_field": "current_a"}},
            {"column_map": {"temperature_c_T01": "temperature_c"}},
            {"units": {"cell_voltage_v": "A"}},
            {"units": {"temperature_c": "F"}},
            {"units": {"cell_voltage_v_missing": "V"}},
            {"current_convention": "unknown"},
            {"pack_id": "unsafe pack id"},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(DatasetError):
                load_csv_bytes(LEGACY, **kwargs)
        with self.assertRaises(DatasetError):
            load_csv_bytes(b"timestamp_s,current_a,cell_voltage_v_C01\n0,10,3.7\n")

    def test_duplicate_headers_surplus_cells_and_invalid_utf8(self):
        for content in (LEGACY.replace(b"current_a", b"temperature_c"),
                        LEGACY.replace(b"0,3.7,10,25", b"0,3.7,10,25,extra"), b"\xff\xfe"):
            with self.subTest(content=content), self.assertRaises(DatasetError):
                load_csv_bytes(content)

    def test_content_limits_and_sanitized_source_basename(self):
        self.assertEqual(load_csv_bytes(LEGACY, "C:\\private\\log.csv").source_name, "log.csv")
        with self.assertRaises(DatasetError):
            load_csv_bytes("not bytes")
        with self.assertRaises(DatasetError):
            load_csv_bytes(b"x" * (5 * 1024 * 1024 + 1))
        content = b"timestamp_s,current_a,cell_voltage_v_C01,temperature_c_T01\n" + b"".join(
            f"{index},10,3.7,25\n".encode() for index in range(20001))
        with self.assertRaises(DatasetError):
            load_csv_bytes(content)

    def test_channel_limit(self):
        headers = ["timestamp_s", "current_a", "temperature_c_T01"] + [f"cell_voltage_v_C{index}" for index in range(MAX_SENSOR_CHANNELS)]
        content = ",".join(headers) + "\n" + ",".join(["0", "10", "25"] + ["3.7"] * MAX_SENSOR_CHANNELS) + "\n"
        with self.assertRaises(DatasetError):
            load_csv_bytes(content.encode())


if __name__ == "__main__":
    unittest.main()
