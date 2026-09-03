from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from semantic_core.models import Ontology, SemanticQueryGraph
from semantic_core.validation import SemanticValidationError, validate_sqg


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sqg-validate")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate an SQG document")
    validate.add_argument("sqg", type=Path, help="path to an SQG JSON document")
    validate.add_argument(
        "--ontology", type=Path, required=True, help="path to an ontology JSON document"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sqg = SemanticQueryGraph.model_validate(_load_json(args.sqg))
        ontology = Ontology.model_validate(_load_json(args.ontology))
        validate_sqg(sqg, ontology)
    except ValidationError as exc:
        print("INVALID: contract validation failed")
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"]) or "<document>"
            print(f"- {location}: {error['msg']}")
        return 1
    except (SemanticValidationError, ValueError) as exc:
        print(f"INVALID: {exc}")
        return 1

    print(
        f"VALID: {args.sqg} ({len(sqg.nodes)} nodes, root={sqg.root}, "
        f"contract={sqg.contract_version})"
    )
    return 0

