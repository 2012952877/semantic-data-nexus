from __future__ import annotations

import datetime as dt
import posixpath

from packaging.utils import canonicalize_name

from scripts.supply_chain.adapters import Blobs, ImageFiles, file_fact, license_path
from scripts.supply_chain.common import canonical, digest, require
from scripts.supply_chain.review import evidence_digest


def identity(ecosystem: str, name: str, version: str) -> tuple[str, str, str]:
    if ecosystem == "pypi":
        name = str(canonicalize_name(name))
    elif ecosystem == "nuget":
        name = name.lower()
    return ecosystem, name, version


def syft_identity(package: dict) -> tuple[str, str, str]:
    ecosystem = {"python": "pypi", "dotnet": "nuget"}.get(package["type"], package["type"])
    return identity(ecosystem, package["name"], package["version"])


def reconcile(syft: dict, records: list[dict]) -> dict:
    indexed = {}
    for package in syft["artifacts"]:
        indexed.setdefault(syft_identity(package), []).append(package["id"])
    covered = []
    for record in records:
        key = identity(record["ecosystem"], record["name"], record["version"])
        require(key in indexed, "resolved-component-missing-from-sbom")
        covered.append({"ecosystem": key[0], "name": key[1], "version": key[2], "ids": sorted(indexed[key])})
    require(covered, "missing-resolved-oracle")
    return {"status": "complete", "resolved": sorted(covered, key=lambda r: canonical(r))}


def compile_inventory(syft: dict, files: ImageFiles, blobs: Blobs, records: list[dict],
                      *, source: dict, subject: dict, scope: str, gaps: list[str],
                      edges: list[list[str]]) -> dict:
    by_key = {}
    for record in records:
        by_key.setdefault(identity(record["ecosystem"], record["name"], record["version"]), []).append(record)
    package_files = {}
    raw_files = {f["id"]: f for f in syft.get("files", [])}
    for relationship in syft["artifactRelationships"]:
        if relationship["child"] in raw_files and relationship["type"] in {"contains", "evident-by"}:
            package_files.setdefault(relationship["parent"], []).append(raw_files[relationship["child"]])
    components = []
    for package in syft["artifacts"]:
        related = by_key.get(syft_identity(package), [])
        facts = {}
        for raw in package_files.get(package["id"], []):
            path = raw["location"]["path"]
            if files.has(path) and files.members[files.resolve(path)].isfile():
                facts[path] = file_fact(files, path, blobs, retain=license_path(path))
        for location in package["locations"]:
            path = location["path"]
            if files.has(path) and files.members[files.resolve(path)].isfile():
                facts[path] = file_fact(files, path, blobs, retain=license_path(path))
        for record in related:
            for fact in record.get("files", []) + [record["metadata"]]:
                facts[fact["path"]] = fact
        license_files = [f for f in facts.values() if "blob" in f and license_path(f["path"])]
        for record in related:
            license_files.extend(record.get("license_files", []))
        # Debian copyright files are evidence, not a license inferred from a package name.
        copyright_path = "/usr/share/doc/" + package["name"] + "/copyright"
        if package["type"] == "deb" and files.has(copyright_path):
            license_files.append(file_fact(files, copyright_path, blobs, retain=True))
        declared = set()
        detected = []
        for license_info in package.get("licenses", []):
            value = license_info.get("spdxExpression") or license_info.get("value", "")
            if license_info.get("type") == "declared":
                if value:
                    declared.add(value)
            else:
                detected.append(license_info)
        for record in related:
            declared.update(record.get("declared", []))
        archives = [r["archive"] for r in related if "archive" in r]
        upstream = [r.get("upstream") for r in related if r.get("upstream")]
        if package.get("metadata", {}).get("url"):
            upstream.append(package["metadata"]["url"])
        distributed = scope
        if scope == "build" and package["foundBy"] == "javascript-lock-cataloger":
            distributed = "build-lock-candidate"
        if scope == "build" and related and package["type"] == "npm":
            distributed = "bundled-production-input"
        component = {
            "id": package["id"], "name": package["name"], "version": package["version"],
            "purl": package.get("purl", ""), "ecosystem": package["type"],
            "scope": distributed, "found_by": package["foundBy"],
            "locations": package["locations"], "declared": sorted(declared),
            "package_metadata": package.get("metadata", {}),
            "detected": sorted(detected, key=lambda r: canonical(r)),
            "syft_licenses": package.get("licenses", []),
            "files": sorted(facts.values(), key=lambda r: r["path"]),
            "license_files": sorted({digest(canonical(f)): f for f in license_files}.values(), key=lambda r: canonical(r)),
            "archives": archives, "upstream": upstream,
        }
        component["evidence_sha256"] = evidence_digest(component)
        components.append(component)
    return {
        "format": "nexus-supply-chain-evidence-v1",
        "source_revision": source["revision"], "source_tree": source["tree"],
        "observed_on": dt.datetime.now(dt.timezone.utc).date().isoformat(),
        "inputs": source["inputs"], "tool": syft["descriptor"],
        "subject": subject, "scope": scope,
        "coverage": reconcile(syft, records), "resolved_edges": edges,
        "resolved": records,
        "components": sorted(components, key=lambda c: c["id"]),
        "relationships": syft["artifactRelationships"],
        "file_components": syft.get("files", []),
        "distro": syft.get("distro", {}),
        "gaps": sorted(set(gaps)),
    }
