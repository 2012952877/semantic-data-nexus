"""Console and machine-readable evaluation reports."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .evaluator import EvaluationReport


def _xml_safe(value: str) -> str:
    return "".join(
        character
        for character in value
        if character in "\t\n\r"
        or "\u0020" <= character <= "\ud7ff"
        or "\ue000" <= character <= "\ufffd"
        or "\U00010000" <= character <= "\U0010ffff"
    )


def console_summary(report: EvaluationReport) -> str:
    status = "PASS" if report.passed else "FAIL"
    lines = [f"{status} semantic evaluation: {report.score:.2f}/100"]
    lines.append(
        "Dimensions: "
        + ", ".join(
            f"{name}={score:.2f}" for name, score in report.dimension_scores.items()
        )
    )
    for error in report.validation_errors:
        lines.append(f"[INVALID] candidate bundle: {error}")
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
    bundle_failure = bool(report.validation_errors)
    suite = ET.Element(
        "testsuite",
        {
            "name": _xml_safe(f"semantic-eval-{report.suite_version}"),
            "tests": str(len(report.cases) + int(bundle_failure)),
            "failures": str(
                sum(not case.passed for case in report.cases) + int(bundle_failure)
            ),
        },
    )
    if bundle_failure:
        test = ET.SubElement(
            suite,
            "testcase",
            {"classname": "semantic_eval", "name": "candidate-bundle-validation"},
        )
        failure = ET.SubElement(
            test,
            "failure",
            {"message": "candidate bundle is invalid"},
        )
        failure.text = _xml_safe("\n".join(report.validation_errors))
    for case in report.cases:
        test = ET.SubElement(
            suite,
            "testcase",
            {"classname": "semantic_eval", "name": _xml_safe(case.case_id)},
        )
        if not case.passed:
            failure = ET.SubElement(
                test,
                "failure",
                {"message": f"semantic score {case.score:.2f}"},
            )
            failure.text = _xml_safe(
                "\n".join(
                    f"{item.dimension}/{item.path}: {item.message}"
                    for item in case.differences
                )
            )
    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
