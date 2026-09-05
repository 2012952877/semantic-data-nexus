from __future__ import annotations

import datetime as dt
import re
from urllib.parse import unquote
from pathlib import Path

from scripts.supply_chain.common import canonical, digest, require, sha_file
from scripts.supply_chain.standards import (
    package_ref, resolved_component_edges, validate_cyclonedx, validate_non_package,
)


def validate_policy(policy: dict, today: dt.date) -> dict:
    require(set(policy) == {"version", "max_evidence_age_days", "reviews"}, "invalid-review-policy")
    require(policy["version"] == 1, "invalid-review-policy")
    require(type(policy["max_evidence_age_days"]) is int and 0 < policy["max_evidence_age_days"] <= 90, "invalid-review-policy")
    require(isinstance(policy["reviews"], list), "invalid-review-policy")
    reviews = {}
    for entry in policy["reviews"]:
        require(set(entry) == {
            "purl", "version", "evidence_sha256", "decision", "reviewer", "rationale",
            "expires", "license_conclusion", "notice", "notice_blobs",
        }, "invalid-review-entry")
        for field in ("purl", "version", "reviewer", "rationale", "license_conclusion"):
            require(isinstance(entry[field], str) and entry[field].strip(), "invalid-review-entry")
        require(entry["purl"].startswith("pkg:") and "*" not in entry["purl"] + entry["version"], "wildcard-review")
        encoded_version = entry["purl"].split("?", 1)[0].split("#", 1)[0].rpartition("@")[2]
        require(unquote(encoded_version) == entry["version"] and "*" not in unquote(entry["purl"]), "unversioned-review")
        require(re.fullmatch("[0-9a-f]{64}", entry["evidence_sha256"]), "invalid-review-hash")
        require(entry["decision"] in {"approved", "exception"}, "invalid-review-decision")
        require(entry["notice"] in {"required", "not-required"}, "invalid-notice-decision")
        require(isinstance(entry["notice_blobs"], list), "invalid-notice-evidence")
        require(entry["notice"] != "required" or entry["notice_blobs"], "missing-notice-evidence")
        require(all(isinstance(s, str) and re.fullmatch("[0-9a-f]{64}", s) for s in entry["notice_blobs"]), "invalid-notice-evidence")
        expires = dt.date.fromisoformat(entry["expires"])
        require(today <= expires <= today + dt.timedelta(days=365), "expired-review")
        key = (entry["purl"], entry["version"], entry["evidence_sha256"])
        require(key not in reviews, "conflicting-review")
        reviews[key] = entry
    return reviews


def evidence_digest(component: dict) -> str:
    return digest(canonical({k: v for k, v in component.items() if k != "evidence_sha256"}))


def license_state(component: dict) -> str:
    declared = {s.strip() for s in component["declared"] if s.strip() and s.strip() not in {"UNKNOWN", "NOASSERTION"}}
    detected = {s["value"] for s in component["detected"] if s.get("value") and s["value"] not in {"UNKNOWN", "NOASSERTION"}}
    if not declared and not detected:
        return "unknown"
    # Different text is a review signal, not an automated legal equivalence judgment.
    if len(declared) > 1 or (declared and detected and not declared & detected):
        return "conflicting"
    return "unreviewed"


def review_inventory(inventory: dict, document: dict, policy: dict, directory: Path,
                     tools: Path, expected_revision: str, today: dt.date) -> dict:
    validate_cyclonedx(document, tools)
    reviews = validate_policy(policy, today)
    require(inventory["source_revision"] == expected_revision, "stale-source-revision")
    require(re.fullmatch("[0-9a-f]{40}", expected_revision), "invalid-source-revision")
    observed = dt.date.fromisoformat(inventory["observed_on"])
    require(0 <= (today - observed).days <= policy["max_evidence_age_days"], "stale-evidence")
    require(inventory["coverage"]["status"] == "complete", "incomplete-resolved-coverage")
    require(inventory["components"], "empty-component-inventory")
    subject = inventory["subject"]
    require(re.fullmatch("sha256:[0-9a-f]{64}", subject["image_id"]), "missing-artifact-identity")
    require(inventory["inputs"] and inventory["tool"]["version"], "missing-provenance")
    properties = {p["name"]: p["value"] for p in document["metadata"]["properties"]}
    require(properties.get("nexus:inventory-sha256") == digest(canonical(inventory)), "inventory-document-drift")
    require(properties.get("nexus:source-revision") == expected_revision, "document-source-drift")
    require(properties.get("nexus:subject-sha256") == subject["image_id"][7:], "document-subject-drift")
    ids = {c["id"] for c in inventory["components"]}
    file_index = {f["id"]: f for f in inventory.get("file_components", [])}
    components = {}
    for component in document["components"]:
        key = package_ref(component)
        if key in ids:
            components[key] = component
        else:
            validate_non_package(component, inventory, file_index)
    require(len(components) == len(inventory["components"]), "component-coverage-drift")
    dependencies = {d["ref"]: set(d.get("dependsOn", [])) for d in document.get("dependencies", [])}
    for parent, child in resolved_component_edges(inventory):
        require(parent in components and child in components, "missing-resolved-component")
        require(components[child]["bom-ref"] in dependencies.get(components[parent]["bom-ref"], set()), "missing-resolved-dependency-edge")
    rows = []
    for component in inventory["components"]:
        require(evidence_digest(component) == component["evidence_sha256"], "component-evidence-drift")
        require(component["id"] in components, "missing-component")
        cdx = components[component["id"]]
        require(cdx.get("purl", "") == component["purl"] and cdx.get("version", "") == component["version"], "component-identity-conflict")
        bindings = {p["name"]: p["value"] for p in cdx.get("properties", [])}
        require(bindings.get("nexus:evidence-sha256") == component["evidence_sha256"], "component-document-drift")
        for fact in component["files"] + component["license_files"]:
            require(re.fullmatch("[0-9a-f]{64}", fact["sha256"]), "invalid-artifact-hash")
            if "blob" in fact:
                require(fact["blob"] == fact["sha256"], "evidence-blob-conflict")
                require(sha_file(directory / "blobs" / fact["blob"]) == fact["sha256"], "evidence-blob-drift")
        state = license_state(component)
        issues = []
        if not component["files"]:
            issues.append("missing-component-artifact-hashes")
        if not component["purl"] or not component["version"]:
            issues.append("missing-package-identity")
        if not component["license_files"]:
            issues.append("missing-license-notice-evidence")
        if not component["upstream"] and not component["archives"]:
            issues.append("missing-upstream-provenance")
        key = (component["purl"], component["version"], component["evidence_sha256"])
        approval = reviews.get(key)
        if approval:
            available = {f["blob"] for f in component["license_files"] if "blob" in f}
            require(set(approval["notice_blobs"]) <= available, "unbound-notice-evidence")
            if state in {"unknown", "conflicting"} and approval["decision"] != "exception":
                issues.append("explicit-exception-required")
            elif not issues:
                state = "reviewed" if approval["decision"] == "approved" else "reviewed-exception"
        else:
            issues.append(state)
        rows.append({
            "id": component["id"], "purl": component["purl"], "version": component["version"],
            "evidence_sha256": component["evidence_sha256"], "state": state,
            "issues": sorted(issues),
        })
    gaps = inventory.get("gaps", [])
    return {
        "status": "BLOCKED" if gaps or any(r["issues"] for r in rows) else "ACCEPTED",
        "source_revision": expected_revision, "subject": subject["name"],
        "policy_sha256": digest(canonical(policy)), "evaluated_on": today.isoformat(),
        "gaps": gaps, "components": rows,
        "legal_clearance": False,
    }
