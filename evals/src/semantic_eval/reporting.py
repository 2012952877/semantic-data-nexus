"""Console and machine-readable evaluation reports."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .evaluator import EvaluationReport


def console_summary(report: EvaluationReport) -> str:
    status = "PASS" if report.passed else "FAIL"
    lines = [f"{status} semantic evaluation: {report.score:.2f}/100"]
    lines.append(
        "Dimensions: "
        + ", ".join(
            f"{name}={score:.2f}" for name, score in report.dimension_scores.items()
        )
    )
    for case in report.cases:
        marker = "PASS" if case.passed else "FAIL"
        lines.append(f"[{marker}] {case.case_id}: {case.score:.2f}")
        for difference in case.differences:
            lines.append(
                f"  - {difference.dimension}/{difference.path}: {difference.message}"
            )
    return "\n".join(lines)


def write_json(report: EvaluationReport, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_junit(report: EvaluationReport, path: str | Path) -> None:
    suite = ET.Element(
        "testsuite",
        {
            "name": f"semantic-eval-{report.suite_version}",
            "tests": str(len(report.cases)),
            "failures": str(sum(not case.passed for case in report.cases)),
        },
    )
    for case in report.cases:
        test = ET.SubElement(
            suite,
            "testcase",
            {"classname": "semantic_eval", "name": case.case_id},
        )
        if not case.passed:
            failure = ET.SubElement(
                test,
                "failure",
                {"message": f"semantic score {case.score:.2f}"},
            )
            failure.text = "\n".join(
                f"{item.dimension}/{item.path}: {item.message}"
                for item in case.differences
            )
    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
