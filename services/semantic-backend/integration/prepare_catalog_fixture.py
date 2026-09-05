"""Generate explicit synthetic catalog fixtures for the identity/browser test stack."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from query_runtime.domain import BoundSource
from semantic_api.catalog_v1.catalog import pin_for
from semantic_api.catalog_v1.models import CatalogDocument

from semantic_backend.catalog_compilation import TYPES, CatalogBindings, EntityBinding, FieldBinding

ROOT = Path(__file__).resolve().parents[3]


def prepare(output: Path) -> None:
    cases = json.loads((ROOT / "contracts/compiler/v1/examples.json").read_text())["cases"]
    entries, model, browser = [], {}, []
    for case in cases:
        case["catalog"]["scope"] = {"tenant_id": "tenant-a", "workspace_id": "workspace-a"}
        document = CatalogDocument.model_validate(case["catalog"])
        pin = pin_for(document)
        fields = {f.id: f for f in document.fields}
        bindings = CatalogBindings(
            contract_version="catalog-bindings/v1",
            catalog=pin,
            entities=tuple(
                EntityBinding(
                    entity_id=item["entity_id"],
                    source=BoundSource(
                        alias=item["alias"],
                        source_type=item["source_type"],
                        object_name=item["object_name"],
                    ),
                    fields=tuple(
                        FieldBinding(
                            field_id=f, column_name=c, data_type=TYPES[fields[f].data_type]
                        )
                        for f, c in item["columns"].items()
                    ),
                )
                for item in case["bindings"]
            ),
        )
        candidate = case["candidate"]
        candidate["graph"]["catalog"] = pin.model_dump(mode="json")
        entries.append(
            {
                "document": document.model_dump(mode="json"),
                "bindings": bindings.model_dump(mode="json"),
                "rows": case["rows"],
            }
        )
        model[pin.content_sha256] = candidate
        browser.append(
            {
                "name": case["name"],
                "request": {
                    "contract_version": "catalog-compile/v1",
                    "request_id": "replace-per-test",
                    "catalog": pin.model_dump(mode="json"),
                    "question": case["question"],
                },
                "expected": case["expected"],
                "grant": {
                    "requestId": "replace-per-test",
                    "grantId": "catalog-" + case["name"],
                    "membershipId": "membership-alice-workspace-a",
                    "groupId": None,
                    "resourceKind": "ontology",
                    "resourceId": document.resource_id,
                    "revision": document.revision,
                    "contentSha256": pin.content_sha256,
                    "permission": "compiler:query",
                    "active": True,
                    "allowedIds": {
                        "entityIds": [e.id for e in document.entities],
                        "fieldIds": [f.id for f in document.fields],
                        "metricIds": [m.id for m in document.metrics],
                        "relationIds": [r.id for r in document.relations],
                        "memberIds": [m.id for f in document.fields for m in f.members],
                    },
                },
            }
        )
    output.mkdir(parents=True, exist_ok=True)
    observation = output / "catalog-observations"
    observation.mkdir(exist_ok=False)
    # Only value-free test events are writable by the non-root test container.
    os.chmod(observation, 0o777)
    for name, value in [
        ("catalog-server.json", {"contract_version": "catalog-server/v1", "entries": entries}),
        ("catalog-model.json", model),
        ("catalog-browser.json", browser),
    ]:
        path = output / name
        if path.exists():
            raise FileExistsError(path)
        path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "ops/identity/.generated")
    prepare(parser.parse_args().output)
