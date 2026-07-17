#!/usr/bin/env python3
"""Fail when any measured function or method has low statement coverage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _minimum_percentage(value: str) -> float:
    minimum = float(value)
    if not 0 <= minimum <= 100:
        raise argparse.ArgumentTypeError("minimum must be between 0 and 100")
    return minimum


def evaluate_report(
    report: dict[str, Any],
    minimum: float,
) -> tuple[list[str], int]:
    failures = []
    inspected = 0
    for filename, file_data in sorted(report.get("files", {}).items()):
        functions = file_data.get("functions")
        if functions is None:
            raise ValueError(
                "coverage JSON has no per-function data; use coverage.py 7.10.5 or newer"
            )
        for name, region in sorted(functions.items()):
            if not name:
                continue
            summary = region["summary"]
            statements = summary["num_statements"]
            if statements == 0:
                continue
            inspected += 1
            covered = summary["covered_lines"]
            percentage = covered * 100 / statements
            if percentage >= minimum:
                continue
            missing = ",".join(str(line) for line in region["missing_lines"])
            failures.append(
                f"{filename}:{region['start_line']} {name}: "
                f"{percentage:.2f}% ({covered}/{statements}); missing {missing}"
            )
    return failures, inspected


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enforce minimum statement coverage for every function and method."
    )
    parser.add_argument("report", type=Path, help="coverage.py JSON report")
    parser.add_argument(
        "--minimum",
        type=_minimum_percentage,
        default=80.0,
        help="minimum percentage per function/method (default: 80)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    failures, inspected = evaluate_report(report, args.minimum)
    if failures:
        print(
            f"{len(failures)} of {inspected} functions/methods below "
            f"{args.minimum:.2f}% statement coverage:",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(
        f"PASS: {inspected} functions/methods meet "
        f"{args.minimum:.2f}% statement coverage."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
