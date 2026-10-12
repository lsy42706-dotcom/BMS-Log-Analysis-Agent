"""Strict, bounded CSV import and explicit column/unit normalization."""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re

from .domain import (
    Dataset, DatasetError, MAX_BYTES, MAX_SAMPLES, MAX_SENSOR_CHANNELS,
    Sample, _CHANNEL, _TOKEN, _is_sensor_field, _quality_issue, channel_metadata,
)


def _unit_transform(name: str, unit: object) -> tuple[float, float]:
    kind = channel_metadata(name)["kind"]
    options = {
        "timestamp_s": {"s": (1.0, 0.0), "ms": (0.001, 0.0)},
        "cell_voltage_v": {"V": (1.0, 0.0), "mV": (0.001, 0.0)},
        "current_a": {"A": (1.0, 0.0), "mA": (0.001, 0.0)},
        "temperature_c": {"degC": (1.0, 0.0), "C": (1.0, 0.0), "K": (1.0, -273.15)},
    }
    if not isinstance(unit, str) or unit not in options.get(kind, {}):
        raise DatasetError("Unsupported unit for a selected column.")
    return options[kind][unit]


def _finite_value(raw: object, transform: tuple[float, float]) -> float | None:
    try:
        value = float(raw) * transform[0] + transform[1]
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def parse_csv_bytes(content: bytes, source_name: str = "upload.csv",
                    column_map: dict[str, str] | None = None,
                    units: dict[str, str] | None = None, *,
                    current_convention: str = "positive_discharge", pack_id: str = "pack_1") -> Dataset:
    if not isinstance(content, bytes):
        raise DatasetError("CSV content must be bytes.")
    if len(content) > MAX_BYTES:
        raise DatasetError("CSV exceeds the 5 MiB size limit.")
    if current_convention not in ("positive_discharge", "positive_charge"):
        raise DatasetError("Current convention must be positive_discharge or positive_charge.")
    if not isinstance(pack_id, str) or not _TOKEN.fullmatch(pack_id):
        raise DatasetError("Pack identifier must be a short ASCII identifier.")
    if not isinstance(source_name, str):
        raise DatasetError("Source name must be text.")
    # The basename is a label, not an executable or a path opened by this importer.
    safe_name = re.split(r"[/\\]", source_name)[-1][:160] or "upload.csv"
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeError as exc:
        raise DatasetError("CSV must be UTF-8 encoded.") from exc
    if column_map is not None and not isinstance(column_map, dict):
        raise DatasetError("Column mapping must be an object.")
    if units is not None and not isinstance(units, dict):
        raise DatasetError("Unit mapping must be an object.")
    reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
    samples, issues = [], []
    try:
        headers = reader.fieldnames
        if not headers:
            raise DatasetError("CSV requires timestamp_s, current_a, voltage and temperature columns.")
        if len(headers) != len(set(headers)):
            raise DatasetError("CSV contains duplicate column names.")
        mapping = {name: name for name in headers if name == "timestamp_s" or _is_sensor_field(name)}
        for name, source in (column_map or {}).items():
            if name != "timestamp_s" and not _is_sensor_field(name):
                raise DatasetError("Column mapping contains an unsupported canonical name.")
            if not isinstance(source, str) or source not in headers:
                raise DatasetError("Column mapping references a missing source column.")
            mapping[name] = source
        if len(set(mapping.values())) != len(mapping):
            raise DatasetError("Each selected source column must map to exactly one canonical field.")
        channels = sorted(name for name in mapping if name != "timestamp_s")
        if len(channels) > MAX_SENSOR_CHANNELS:
            raise DatasetError("CSV exceeds the 64-sensor-channel limit.")
        kinds = {channel_metadata(name)["kind"] for name in channels}
        if "timestamp_s" not in mapping or not {"cell_voltage_v", "temperature_c", "current_a"} <= kinds:
            raise DatasetError("CSV requires timestamp_s, current_a, voltage and temperature columns.")
        defaults = {"timestamp_s": "s", "cell_voltage_v": "V", "current_a": "A", "temperature_c": "degC"}
        supplied_units = units or {}
        if any(name not in mapping and name not in ("cell_voltage_v", "current_a", "temperature_c") for name in supplied_units):
            raise DatasetError("Unit mapping references an unselected canonical field.")
        for name, unit in supplied_units.items():
            _unit_transform(name, unit)
        selected_units = {name: supplied_units.get(name, supplied_units.get(channel_metadata(name)["kind"],
                                                   defaults[channel_metadata(name)["kind"]])) for name in mapping}
        transforms = {name: _unit_transform(name, unit) for name, unit in selected_units.items()}
        for record in reader:
            row = reader.line_num
            if len(samples) >= MAX_SAMPLES:
                raise DatasetError("CSV exceeds the 20000-sample limit.")
            if None in record:
                raise DatasetError(f"Row {row}: CSV has more cells than declared columns.")
            timestamp = _finite_value(record.get(mapping["timestamp_s"]), transforms["timestamp_s"])
            if timestamp is None:
                raise DatasetError(f"Row {row}: timestamp_s must be finite and numeric.")
            if samples and timestamp <= samples[-1].timestamp_s:
                raise DatasetError(f"Row {row}: timestamp_s must strictly increase.")
            if samples and not math.isfinite(timestamp - samples[0].timestamp_s):
                raise DatasetError(f"Row {row}: timestamp_s range is too large.")
            scalar = {"cell_voltage_v": None, "current_a": None, "temperature_c": None}
            cells, probes = {}, {}
            for name in channels:
                value = _finite_value(record.get(mapping[name]), transforms[name])
                if value is None:
                    issues.append(_quality_issue(row, timestamp, name))
                match = _CHANNEL.fullmatch(name)
                if match:
                    kind, identifier = match.groups()
                    (cells if kind == "cell_voltage_v" else probes)[identifier] = value
                else:
                    scalar[name] = value
            samples.append(Sample(timestamp, **scalar, row_number=row,
                                  cell_voltages_v=cells, temperatures_c=probes))
    except csv.Error as exc:
        raise DatasetError("CSV syntax is invalid.") from exc
    if not samples:
        raise DatasetError("CSV must contain at least one sample.")
    return Dataset(samples, issues, safe_name, hashlib.sha256(content).hexdigest(), pack_id,
                   current_convention, {"schema_version": 2, "channels": channels,
                                        "column_map": mapping.copy(),
                                        "source_units": selected_units, "normalized_units": defaults.copy()})
