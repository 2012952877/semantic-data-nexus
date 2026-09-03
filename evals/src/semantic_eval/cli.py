"""Command-line entry point for semantic golden evaluation."""

from __future__ import annotations

import argparse
from pathlib import Path

from .evaluator import DEFAULT_WEIGHTS, evaluate_bundle, load_document
from .reference import load_static_candidate
from .reporting import console_summary, write_json, write_junit


def default_golden_path() -> Path:
    source_fixture = Path(__file__).parents[2] / "fixtures" / "v0" / "golden_cases.json"
    if source_fixture.exists():
        return source_fixture
    return Path(__file__).parent / "fixtures" / "v0" / "golden_cases.json"


def dimension_gate(value: str) -> tuple[str, float]:
    try:
        name, threshold_text = value.split("=", 1)
        threshold = float(threshold_text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("use DIMENSION=SCORE") from error
    if name not in DEFAULT_WEIGHTS:
        raise argparse.ArgumentTypeError(
            f"dimension must be one of: {', '.join(DEFAULT_WEIGHTS)}"
        )
    if not 0 <= threshold <= 100:
        raise argparse.ArgumentTypeError("dimension score must be between 0 and 100")
    return name, threshold


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path, help="candidate artifact JSON/YAML")
    parser.add_argument(
        "--golden",
        type=Path,
        default=default_golden_path(),
    )
    parser.add_argument("--json-report", type=Path)
    parser.add_argument("--junit-report", type=Path)
    parser.add_argument(
        "--minimum-score",
        type=float,
        default=100.0,
        help="exit successfully when the weighted score reaches this threshold",
    )
    parser.add_argument(
        "--minimum-dimension",
        action="append",
        type=dimension_gate,
        default=[],
        metavar="DIMENSION=SCORE",
        help="repeatable per-dimension quality gate",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = evaluate_bundle(
        load_document(args.golden),
        load_static_candidate(args.candidate),
    )
    print(console_summary(report))
    if args.json_report:
        write_json(report, args.json_report)
    if args.junit_report:
        write_junit(report, args.junit_report)
    dimensions_pass = all(
        report.dimension_scores[name] >= threshold
        for name, threshold in args.minimum_dimension
    )
    raise SystemExit(0 if report.score >= args.minimum_score and dimensions_pass else 1)


if __name__ == "__main__":
    main()
