from __future__ import annotations

import argparse
import copy
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from semantic_eval.cli import dimension_gate
from semantic_eval.evaluator import evaluate_bundle, load_document
from semantic_eval.reference import load_static_candidate
from semantic_eval.reporting import console_summary, write_json, write_junit


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "evals" / "fixtures" / "v0"


def test_console_json_and_junit_reports(tmp_path: Path) -> None:
    report = evaluate_bundle(
        load_document(FIXTURES / "golden_cases.json"),
        load_static_candidate(FIXTURES / "candidates" / "failing.json"),
    )
    text = console_summary(report)
    assert text.startswith("FAIL semantic evaluation:")
    assert "semantic/semantic.metrics" in text

    json_path = tmp_path / "report.json"
    junit_path = tmp_path / "report.xml"
    write_json(report, json_path)
    write_junit(report, junit_path)
    assert '"passed": false' in json_path.read_text(encoding="utf-8")
    root = ET.parse(junit_path).getroot()
    assert int(root.attrib["failures"]) > 0


def test_dimension_gate_validation() -> None:
    assert dimension_gate("governance=100") == ("governance", 100.0)
    with pytest.raises(argparse.ArgumentTypeError, match="dimension must be one of"):
        dimension_gate("unknown=90")


def test_bundle_validation_errors_reach_all_reports(tmp_path: Path) -> None:
    golden_case = copy.deepcopy(load_document(FIXTURES / "golden_cases.json")["cases"][0])
    case_id = golden_case["id"]
    actual = copy.deepcopy(
        load_static_candidate(FIXTURES / "candidates" / "passing.json")["cases"][
            case_id
        ]
    )
    actual["id"] = case_id
    report = evaluate_bundle(
        {"suite_version": "test", "cases": [golden_case]},
        {"cases": [actual, copy.deepcopy(actual)]},
    )
    assert "[INVALID] candidate bundle:" in console_summary(report)

    json_path = tmp_path / "invalid-bundle.json"
    junit_path = tmp_path / "invalid-bundle.xml"
    write_json(report, json_path)
    write_junit(report, junit_path)
    assert "duplicate ID" in json_path.read_text(encoding="utf-8")
    root = ET.parse(junit_path).getroot()
    assert root.find("./testcase[@name='candidate-bundle-validation']") is not None


def test_junit_filters_xml_illegal_validation_characters(tmp_path: Path) -> None:
    candidate_path = tmp_path / "control-character.yaml"
    candidate_path.write_text(
        "artifact_version: candidate-v0\n"
        "bad:\n"
        "  \"\\0\": !!set\n"
        "    value: null\n"
        "cases: {}\n",
        encoding="utf-8",
    )
    golden_case = copy.deepcopy(load_document(FIXTURES / "golden_cases.json")["cases"][0])
    report = evaluate_bundle(
        {"suite_version": "test", "cases": [golden_case]},
        load_document(candidate_path),
    )
    assert report.validation_errors
    junit_path = tmp_path / "control-character.xml"
    write_junit(report, junit_path)
    ET.parse(junit_path)
