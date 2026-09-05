from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from jsonschema import Draft7Validator
from referencing import Registry, Resource

from scripts.supply_chain.common import HERE, canonical, digest, load, require, sha_file


def validate_cyclonedx(document: dict, tools: Path) -> None:
    pins = load(HERE / "tools.json")["schema"]
    registry = Registry()
    schemas = {}
    for name, sha in pins["files"].items():
        path = tools / name
        require(sha_file(path) == sha, "schema-hash-drift")
        schema = load(path)
        schemas[name] = schema
        resource = Resource.from_contents(schema)
        for url in {schema["$id"], "http://cyclonedx.org/schema/" + name, "https://cyclonedx.org/schema/" + name}:
            registry = registry.with_resource(url, resource)
    require(document.get("specVersion") == "1.6", "wrong-cyclonedx-version")
    validator = Draft7Validator(schemas["bom-1.6.schema.json"], registry=registry)
    error = next(validator.iter_errors(document), None)
    if error:
        # Report the official schema location, never the rejected package-controlled value.
        location = "-".join(str(p) for p in error.absolute_schema_path)
        require(False, "invalid-cyclonedx-" + location)
    components = document.get("components", [])
    refs = [c["bom-ref"] for c in components]
    refs.append(document["metadata"]["component"]["bom-ref"])
    require(len(refs) == len(set(refs)), "duplicate-component-reference")
    for edge in document.get("dependencies", []):
        require(edge["ref"] in refs, "dangling-dependency")
        require(set(edge.get("dependsOn", [])) <= set(refs), "dangling-dependency")


def normalize(document: dict) -> dict:
    result = copy.deepcopy(document)
    result.pop("serialNumber", None)
    result.get("metadata", {}).pop("timestamp", None)
    for key in ("components", "dependencies"):
        if key in result:
            result[key] = sorted(result[key], key=lambda v: canonical(v))
    for component in result.get("components", []):
        for key in ("properties", "hashes", "externalReferences"):
            if key in component:
                component[key] = sorted(component[key], key=lambda v: canonical(v))
    for dependency in result.get("dependencies", []):
        if "dependsOn" in dependency:
            dependency["dependsOn"] = sorted(dependency["dependsOn"])
    # Content-derived serials do not imply signatures or supplier attestations.
    result["serialNumber"] = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, digest(canonical(result))))
    return result


def package_ref(component: dict) -> str:
    return component["bom-ref"].rsplit("package-id=", 1)[-1].split("&", 1)[0]


def bind_evidence(document: dict, inventory: dict) -> dict:
    result = copy.deepcopy(document)
    facts = {c["id"]: c for c in inventory["components"]}
    require(len(facts) == len(inventory["components"]), "duplicate-inventory-component")
    seen = set()
    for component in result.get("components", []):
        identity = package_ref(component)
        require(identity in facts, "unmapped-cyclonedx-component")
        fact = facts[identity]
        require(component.get("purl", "") == fact["purl"] and component.get("version", "") == fact["version"], "component-identity-conflict")
        seen.add(identity)
        component.setdefault("properties", []).extend([
            {"name": "nexus:evidence-sha256", "value": fact["evidence_sha256"]},
            {"name": "nexus:distribution-scope", "value": fact["scope"]},
        ])
        for archive in fact["archives"]:
            if archive.get("url"):
                component.setdefault("externalReferences", []).append({
                    "type": "distribution", "url": archive["url"],
                    "hashes": [{"alg": "SHA-256", "content": archive["sha256"]}],
                    "comment": archive["verification"],
                })
    require(seen == set(facts), "missing-cyclonedx-component")
    result["metadata"].setdefault("properties", []).extend([
        {"name": "nexus:source-revision", "value": inventory["source_revision"]},
        {"name": "nexus:subject-sha256", "value": inventory["subject"]["image_id"].removeprefix("sha256:")},
        {"name": "nexus:inventory-sha256", "value": digest(canonical(inventory))},
    ])
    return normalize(result)
