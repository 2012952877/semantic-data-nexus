from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
    ref_set = set(refs)
    require(len(refs) == len(ref_set), "duplicate-component-reference")
    for edge in document.get("dependencies", []):
        require(edge["ref"] in ref_set, "dangling-dependency")
        require(set(edge.get("dependsOn", [])) <= ref_set, "dangling-dependency")


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
    reference = component["bom-ref"]
    ids = parse_qs(urlsplit(reference).query).get("package-id")
    return ids[0] if ids and len(ids) == 1 else reference


def validate_non_package(component: dict, inventory: dict, file_index: dict | None = None) -> None:
    reference = component["bom-ref"]
    if component["type"] == "file":
        files = file_index if file_index is not None else {f["id"]: f for f in inventory.get("file_components", [])}
        require(reference in files, "unmapped-file-component")
        fact = files[reference]
        require(component["name"] == fact["location"]["path"], "file-component-path-drift")
        expected = [{"alg": "SHA-256", "content": d["value"]}
                    for d in fact.get("digests", []) if d["algorithm"] == "sha256"]
        require(component.get("hashes", []) == expected, "file-component-hash-drift")
    elif component["type"] == "operating-system":
        distro = inventory.get("distro", {})
        require(component["name"] == distro.get("id")
                and component["version"] == distro.get("versionID")
                and reference == "os:" + distro["id"] + "@" + distro["versionID"],
                "operating-system-identity-drift")
    else:
        require(False, "unmapped-cyclonedx-component")


def resolved_component_edges(inventory: dict) -> set[tuple[str, str]]:
    labels = {}
    for resolved in inventory["coverage"].get("resolved", []):
        ecosystem = resolved["ecosystem"]
        name, version = resolved["name"], resolved["version"]
        if ecosystem == "pypi":
            label = name
        elif ecosystem == "npm":
            label = name + "@" + version
        elif ecosystem == "nuget":
            label = (name + "/" + version).lower()
        else:
            continue
        labels[label] = resolved["ids"]
    edges = set()
    for parent, child in inventory.get("resolved_edges", []):
        if parent not in labels and parent.lower() in labels:
            parent, child = parent.lower(), child.lower()
        require(parent in labels and child in labels, "unmapped-resolved-edge")
        edges.update((a, b) for a in labels[parent] for b in labels[child])
    return edges


def bind_evidence(document: dict, inventory: dict) -> dict:
    result = copy.deepcopy(document)
    facts = {c["id"]: c for c in inventory["components"]}
    file_index = {f["id"]: f for f in inventory.get("file_components", [])}
    require(len(facts) == len(inventory["components"]), "duplicate-inventory-component")
    seen = set()
    for component in result.get("components", []):
        identity = package_ref(component)
        if identity not in facts:
            validate_non_package(component, inventory, file_index)
            continue
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
    refs = {package_ref(c): c["bom-ref"] for c in result.get("components", [])}
    dependencies = {edge["ref"]: set(edge.get("dependsOn", [])) for edge in result.get("dependencies", [])}
    for parent, child in resolved_component_edges(inventory):
        dependencies.setdefault(refs[parent], set()).add(refs[child])
    result["dependencies"] = [{"ref": ref, "dependsOn": sorted(children)} for ref, children in sorted(dependencies.items())]
    result["metadata"].setdefault("properties", []).extend([
        {"name": "nexus:source-revision", "value": inventory["source_revision"]},
        {"name": "nexus:subject-sha256", "value": inventory["subject"]["image_id"].removeprefix("sha256:")},
        {"name": "nexus:inventory-sha256", "value": digest(canonical(inventory))},
    ])
    return normalize(result)
