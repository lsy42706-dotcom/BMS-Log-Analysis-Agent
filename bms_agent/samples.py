"""Generate synthetic BMS logs without sensors, hardware, or APIs."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .domain import CSV_FIELDS, Sample


SCENARIOS = ("normal", "overcurrent", "overtemperature", "undervoltage", "sensor_fault", "mixed")


def generate_samples(scenario: str) -> list[Sample]:
    if scenario not in SCENARIOS:
        raise ValueError("Unsupported synthetic scenario.")
    samples = []
    for second in range(40):
        voltage = 3.7 - second * 0.002
        current: float | None = 10.0
        temperature = 25.0 + second * 0.05
        if scenario in ("overcurrent", "mixed"):
            if 5 <= second <= 10:
                current = 50.0 + (second - 5)
            elif 11 <= second <= 12:
                current = 47.0
        if scenario in ("overtemperature", "mixed") and 16 <= second <= 22:
            temperature = 60.0 + (second - 16)
        if scenario in ("undervoltage", "mixed") and 27 <= second <= 31:
            voltage = 3.0 - (second - 27) * 0.02
        if scenario == "sensor_fault" and 7 <= second <= 9:
            current = None
        samples.append(Sample(float(second), voltage, current, temperature, second + 2))
    return samples


def write_sample(path: Path, scenario: str) -> Path:
    path = Path(path)
    samples = generate_samples(scenario)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for sample in samples:
            writer.writerow({field: getattr(sample, field) for field in CSV_FIELDS})
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic BMS demonstration logs.")
    parser.add_argument("--output", type=Path, default=Path("samples"))
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    args = parser.parse_args(argv)
    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    for scenario in scenarios:
        path = write_sample(args.output / f"{scenario}.csv", scenario)
        print(f"Synthetic scenario {scenario}: {path}")
    if args.scenario == "all":
        path = write_sample(args.output / "demo.csv", "mixed")
        print(f"Synthetic demonstration (mixed): {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
