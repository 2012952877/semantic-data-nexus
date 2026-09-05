from __future__ import annotations

import base64
import copy
import csv
import datetime as dt
import hashlib
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.supply_chain.adapters import (
    Blobs, ImageFiles, dotnet_inventory, enrich_nuget, pnpm_inventory,
    python_edges, python_inventory,
)
from scripts.supply_chain.common import HERE, EvidenceError, canonical, digest, load, run, sha_file, write
from scripts.supply_chain.inventory import reconcile
from scripts.supply_chain.pipeline import accept, main
from scripts.supply_chain.review import evidence_digest, review_inventory, validate_policy
from scripts.supply_chain.standards import bind_evidence, normalize, package_ref, validate_cyclonedx

REVISION = "a" * 40
TODAY = dt.datetime.now(dt.timezone.utc).date()


@pytest.fixture
def tools() -> Path:
    directory = os.environ.get("SUPPLY_CHAIN_TOOLS")
    assert directory, "Bootstrap pinned tools and set SUPPLY_CHAIN_TOOLS before running."
    return Path(directory)


def image(tmp_path: Path, entries: dict[str, bytes]) -> ImageFiles:
    archive = tmp_path / "image.tar"
    with tarfile.open(archive, "w") as output:
        for path, data in entries.items():
            entry = tarfile.TarInfo(path)
            entry.size = len(data)
            output.addfile(entry, io.BytesIO(data))
    return ImageFiles(archive)


@pytest.fixture
def evidence(tmp_path: Path) -> tuple[dict, dict, dict]:
    text = b"Synthetic test license evidence, not a third-party license.\n"
    sha = Blobs(tmp_path / "blobs").add(text)
    component = {
        "id": "synthetic-id", "name": "synthetic-library", "version": "1.2.3",
        "purl": "pkg:pypi/synthetic-library@1.2.3", "scope": "runtime",
        "files": [{"path": "/synthetic.dist-info/METADATA", "sha256": sha}],
        "declared": ["MIT"], "detected": [],
        "license_files": [{"path": "/synthetic.dist-info/LICENSE", "blob": sha, "sha256": sha}],
        "archives": [], "upstream": ["https://example.invalid/synthetic/1.2.3"],
    }
    component["evidence_sha256"] = evidence_digest(component)
    inventory = {
        "source_revision": REVISION, "observed_on": TODAY.isoformat(),
        "subject": {"name": "synthetic-runtime", "image_id": "sha256:" + "b" * 64},
        "inputs": [{"path": "synthetic.toml", "sha256": "c" * 64}],
        "tool": {"name": "syft", "version": "1.51.1"},
        "components": [component], "coverage": {"status": "complete"}, "gaps": [],
    }
    document = {
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "metadata": {"component": {"bom-ref": "synthetic-root", "type": "container", "name": "synthetic"}},
        "components": [{
            "bom-ref": component["purl"] + "?package-id=synthetic-id", "type": "library",
            "name": component["name"], "version": component["version"], "purl": component["purl"],
        }],
        "dependencies": [{"ref": "synthetic-root", "dependsOn": [component["purl"] + "?package-id=synthetic-id"]}],
    }
    policy = {
        "version": 1, "max_evidence_age_days": 30, "reviews": [{
            "purl": component["purl"], "version": component["version"],
            "evidence_sha256": component["evidence_sha256"],
            "decision": "approved", "reviewer": "synthetic-test-reviewer",
            "rationale": "Synthetic fixture only; this does not approve a real component.",
            "expires": (TODAY + dt.timedelta(days=5)).isoformat(),
            "license_conclusion": "MIT", "notice": "required", "notice_blobs": [sha],
        }],
    }
    return inventory, document, policy


def review(evidence, tmp_path, tools):
    inventory, document, policy = evidence
    return review_inventory(inventory, bind_evidence(document, inventory), policy, tmp_path, tools, REVISION, TODAY)


def test_valid_official_document_and_review(evidence, tmp_path, tools):
    assert review(evidence, tmp_path, tools)["status"] == "ACCEPTED"
    evidence[2]["reviews"] = []
    result = review(evidence, tmp_path, tools)
    assert result["status"] == "BLOCKED"
    assert result["components"][0]["state"] == "unreviewed"


def test_deterministic_normalization_preserves_evidence(evidence, tools):
    inventory, document, _ = evidence
    a = bind_evidence(document, inventory)
    b = copy.deepcopy(a)
    b["metadata"]["timestamp"] = "2020-01-01T00:00:00Z"
    b["serialNumber"] = "urn:uuid:11111111-1111-4111-8111-111111111111"
    b["components"][0]["properties"].reverse()
    assert normalize(a) == normalize(b) == normalize(normalize(a))
    validate_cyclonedx(a, tools)
    assert a["components"][0]["version"] == "1.2.3"
    assert "nexus:evidence-sha256" in canonical(a).decode()


@pytest.mark.parametrize("mutation", ["schema", "dangling", "duplicate"])
def test_malformed_standard_rejected(evidence, tools, mutation):
    doc = bind_evidence(evidence[1], evidence[0])
    if mutation == "schema":
        doc["components"][0]["type"] = "invented-type"
    elif mutation == "dangling":
        doc["dependencies"][0]["dependsOn"] = ["missing"]
    else:
        doc["components"].append(doc["components"][0])
    with pytest.raises(EvidenceError):
        validate_cyclonedx(doc, tools)


@pytest.mark.parametrize("state", ["unknown", "conflicting", "missing-file", "missing-upstream", "hash-drift", "stale", "missing-component"])
def test_bad_evidence_cannot_be_accepted(evidence, tmp_path, tools, state):
    inventory, doc, policy = evidence
    component = inventory["components"][0]
    if state == "unknown":
        component["declared"] = []
    elif state == "conflicting":
        component["detected"] = [{"value": "GPL-3.0-only"}]
    elif state == "missing-file":
        component["license_files"] = []
    elif state == "missing-upstream":
        component["upstream"] = []
    elif state == "hash-drift":
        (tmp_path / "blobs" / component["license_files"][0]["blob"]).write_bytes(b"changed")
    elif state == "stale":
        inventory["observed_on"] = (TODAY - dt.timedelta(days=31)).isoformat()
    else:
        doc["components"] = []
    component["evidence_sha256"] = evidence_digest(component)
    policy["reviews"][0]["evidence_sha256"] = component["evidence_sha256"]
    try:
        result = review(evidence, tmp_path, tools)
    except EvidenceError:
        return
    assert result["status"] == "BLOCKED"


@pytest.mark.parametrize("field,value", [
    ("purl", "pkg:pypi/*"), ("version", "*"), ("rationale", ""),
    ("expires", "2000-01-01"), ("decision", "ignore"), ("notice_blobs", []),
])
def test_exact_expiring_review_policy(evidence, field, value):
    policy = evidence[2]
    policy["reviews"][0][field] = value
    with pytest.raises(EvidenceError):
        validate_policy(policy, TODAY)


def test_explicit_exception_still_requires_version_evidence_and_notice(evidence, tmp_path, tools):
    c = evidence[0]["components"][0]
    c["declared"] = []
    c["evidence_sha256"] = evidence_digest(c)
    entry = evidence[2]["reviews"][0]
    entry["evidence_sha256"] = c["evidence_sha256"]
    assert review(evidence, tmp_path, tools)["status"] == "BLOCKED"
    entry["decision"] = "exception"
    assert review(evidence, tmp_path, tools)["status"] == "ACCEPTED"
    entry["notice_blobs"] = ["d" * 64]
    with pytest.raises(EvidenceError, match="unbound-notice"):
        review(evidence, tmp_path, tools)


def test_duplicate_reviews_fail(evidence):
    policy = evidence[2]
    policy["reviews"].append(copy.deepcopy(policy["reviews"][0]))
    with pytest.raises(EvidenceError, match="conflicting-review"):
        validate_policy(policy, TODAY)


def test_ordinary_approval_cannot_substitute_a_license(evidence, tmp_path, tools):
    evidence[2]["reviews"][0]["license_conclusion"] = "Apache-2.0"
    result = review(evidence, tmp_path, tools)
    assert result["status"] == "BLOCKED"
    assert "license-conclusion-conflict" in result["components"][0]["issues"]


@pytest.mark.parametrize("text", ['{"reviews": [], "reviews": [{}]}', '{"version": NaN}'])
def test_ambiguous_or_nonstandard_json_rejected(tmp_path, text):
    path = tmp_path / "invalid.json"
    path.write_text(text)
    with pytest.raises(EvidenceError):
        load(path)


def test_named_transitive_coverage_not_count_proxy():
    raw = {"artifacts": [
        {"id": "1", "type": "python", "name": "direct", "version": "1"},
        {"id": "2", "type": "python", "name": "unrelated", "version": "1"},
    ]}
    expected = [{"ecosystem": "pypi", "name": "transitive", "version": "1"}]
    with pytest.raises(EvidenceError, match="resolved-component-missing"):
        reconcile(raw, expected)
    raw["artifacts"][1]["name"] = "transitive"
    assert reconcile(raw, expected)["status"] == "complete"


def test_python_record_hashes_and_extra_transitives(tmp_path):
    root = "usr/local/lib/python3.12/site-packages/"
    metadata = b"Name: synthetic\nVersion: 1.0\nLicense-Expression: MIT\nRequires-Dist: nested>=2\n\n"
    data = {root + "synthetic-1.0.dist-info/METADATA": metadata,
            root + "synthetic-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\n",
            root + "synthetic.py": b"synthetic bytes\n"}
    rows = []
    for path, content in data.items():
        rows.append([path[len(root):], "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("="), str(len(content))])
    record_path = root + "synthetic-1.0.dist-info/RECORD"
    rows.append([record_path[len(root):], "", ""])
    rows.append(["__pycache__/synthetic.cpython-312.pyc", "", ""])
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    data[record_path] = output.getvalue().encode()
    files = image(tmp_path, data)
    records = python_inventory(files, Blobs(tmp_path / "blobs"))
    assert records[0]["generated_bytecode"][0]["present"] is False
    files.close()
    with pytest.raises(EvidenceError, match="missing-python-transitive"):
        python_edges(records, {}, {})
    records.append({"name": "nested", "version": "2", "requires": []})
    assert python_edges(records, {}, {}) == [["synthetic", "nested"]]
    data[root + "synthetic.py"] = b"changed bytes\n"
    files = image(tmp_path, data)
    with pytest.raises(EvidenceError, match="wheel-record-hash-drift"):
        python_inventory(files, Blobs(tmp_path / "blobs"))
    files.close()


def test_dotnet_publish_and_restore_archive_relationship(tmp_path, monkeypatch):
    dll = b"Synthetic DLL fixture, not executable"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("lib/net8.0/Synthetic.dll", dll)
        package.writestr("runtimes/linux-x64/native/libsynthetic.so", b"synthetic native fixture")
        package.writestr("Synthetic.nuspec", '<package><metadata><license type="expression">MIT</license></metadata></package>')
        package.writestr("LICENSE", "Synthetic fixture evidence")
    nupkg = archive.getvalue()
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(nupkg))
    deps = {
        "runtimeTarget": {"name": "net8.0"},
        "targets": {"net8.0": {"Synthetic/1.0": {
            "runtime": {"lib/net8.0/Synthetic.dll": {}},
            "runtimeTargets": {"runtimes/linux-x64/native/libsynthetic.so": {"rid": "linux-x64", "assetType": "native"}},
        }}},
        "libraries": {"Synthetic/1.0": {"type": "package", "sha512": base64.b64encode(hashlib.sha512(nupkg).digest()).decode()}},
    }
    data = {"app/test.deps.json": canonical(deps), "app/Synthetic.dll": dll,
            "app/runtimes/linux-x64/native/libsynthetic.so": b"synthetic native fixture",
            "root/.nuget/packages/synthetic/1.0/synthetic.1.0.nupkg": nupkg}
    files = image(tmp_path, data)
    records, _ = dotnet_inventory(files, Blobs(tmp_path / "blobs"))
    enrich_nuget(records, files, Blobs(tmp_path / "blobs"))
    assert records[0]["archive"]["sha256"] == digest(nupkg)
    assert {f["asset_kind"] for f in records[0]["files"]} == {"runtime", "runtimeTargets"}
    records[0]["files"][0]["sha256"] = "f" * 64
    with pytest.raises(EvidenceError, match="nuget-publish-hash-drift"):
        enrich_nuget(records, files, Blobs(tmp_path / "blobs"))
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(b"drift"))
    with pytest.raises(EvidenceError, match="nuget-archive-hash-drift"):
        enrich_nuget(records, files, Blobs(tmp_path / "blobs"))
    files.close()


def test_pnpm_production_transitive_tree_excludes_dev(tmp_path):
    tree = [{"path": "/app", "dependencies": {"direct": {
        "path": "/app/node_modules/direct", "version": "1.0",
        "dependencies": {"nested": {"path": "/app/node_modules/nested", "version": "2.0"}},
    }}}]
    data = {}
    for name, version, path in [("web", "1.0", "app"), ("direct", "1.0", "app/node_modules/direct"),
                                ("nested", "2.0", "app/node_modules/nested"), ("dev-only", "9.0", "app/node_modules/dev-only")]:
        data[path + "/package.json"] = canonical({"name": name, "version": version, "license": "MIT"})
    files = image(tmp_path, data)
    records, edges = pnpm_inventory(tree, files, Blobs(tmp_path / "blobs"))
    assert {r["name"] for r in records} == {"web", "direct", "nested"}
    assert ["direct@1.0", "nested@2.0"] in edges
    files.close()


def test_bundle_hash_drift_rejected_before_review(evidence, tmp_path, tools):
    write(tmp_path / "bundle.json", {
        "source_revision": REVISION, "subjects": ["synthetic-runtime"],
        "files": {"source.json": "0" * 64},
    })
    write(tmp_path / "source.json", {"changed": True})
    with pytest.raises(EvidenceError, match="bundle-file-hash-drift"):
        accept(tmp_path, tools, REVISION, tmp_path / "unused-policy.json")


def test_missing_bundle_manifest_files_rejected(tmp_path, tools):
    write(tmp_path / "bundle.json", {
        "source_revision": REVISION, "subjects": ["synthetic-runtime"], "files": {},
    })
    with pytest.raises(EvidenceError, match="missing-bundle-file"):
        accept(tmp_path, tools, REVISION, tmp_path / "unused-policy.json")


def test_cli_blocked_then_invalid_cannot_leave_success(evidence, tmp_path, tools, monkeypatch):
    inventory, document, policy = evidence
    component = inventory["components"][0]
    raw = {"artifacts": [{"id": component["id"], "type": "python", "name": component["name"],
                          "version": component["version"]}]}
    inventory["resolved"] = [{"ecosystem": "pypi", "name": component["name"], "version": component["version"]}]
    inventory["coverage"] = reconcile(raw, inventory["resolved"])
    inventory["source_tree"] = "e" * 40
    inventory["subject"]["syft_sha256"] = digest(canonical(raw))
    name = "synthetic-runtime"
    write(tmp_path / "source.json", {"revision": REVISION, "tree": inventory["source_tree"], "inputs": inventory["inputs"]})
    write(tmp_path / "toolchain.json", {"syft": load(HERE / "tools.json")["syft"]})
    write(tmp_path / (name + ".syft.json"), raw)
    write(tmp_path / (name + ".inventory.json"), inventory)
    write(tmp_path / (name + ".generator.cdx.json"), document)
    write(tmp_path / (name + ".cdx.json"), bind_evidence(document, inventory))
    write(tmp_path / "bundle.json", {
        "source_revision": REVISION, "subjects": [name],
        "files": {p.relative_to(tmp_path).as_posix(): sha_file(p) for p in tmp_path.rglob("*") if p.is_file()},
    })
    policy["reviews"] = []
    write(tmp_path / "policy.json", policy)
    monkeypatch.setattr("sys.argv", ["pipeline", "accept", "--output", str(tmp_path),
                                   "--tools", str(tools), "--revision", REVISION,
                                   "--policy", str(tmp_path / "policy.json")])
    assert main() == 2
    assert load(tmp_path / "acceptance.json")["status"] == "BLOCKED"
    cdx_path = tmp_path / (name + ".cdx.json")
    cdx_path.write_text(json.dumps(load(cdx_path)), encoding="utf-8")
    bundle = load(tmp_path / "bundle.json")
    bundle["files"][cdx_path.name] = sha_file(cdx_path)
    write(tmp_path / "bundle.json", bundle)
    assert main() == 1
    assert load(tmp_path / "acceptance.json")["status"] == "INVALID"
    write(tmp_path / "acceptance.json", {"status": "ACCEPTED"})
    (tmp_path / (name + ".cdx.json")).unlink()
    assert main() == 1
    assert load(tmp_path / "acceptance.json")["status"] == "INVALID"


def test_cyclonedx_evidence_binding_cannot_be_removed(evidence, tmp_path, tools):
    inventory, document, policy = evidence
    document = bind_evidence(document, inventory)
    document["components"][0]["properties"] = []
    with pytest.raises(EvidenceError, match="component-document-drift"):
        review_inventory(inventory, document, policy, tmp_path, tools, REVISION, TODAY)


def test_resolved_transitive_edge_is_added_and_required(evidence, tmp_path, tools):
    inventory, document, policy = evidence
    original = inventory["components"][0]
    nested = copy.deepcopy(original)
    nested.update(id="nested-id", name="nested", purl="pkg:pypi/nested@1.2.3")
    nested["evidence_sha256"] = evidence_digest(nested)
    inventory["components"].append(nested)
    document["components"].append({
        "bom-ref": nested["purl"] + "?package-id=nested-id", "type": "library",
        "name": "nested", "version": "1.2.3", "purl": nested["purl"],
    })
    inventory["coverage"]["resolved"] = [
        {"ecosystem": "pypi", "name": c["name"], "version": c["version"], "ids": [c["id"]]}
        for c in inventory["components"]
    ]
    inventory["resolved_edges"] = [["synthetic-library", "nested"]]
    bound = bind_evidence(document, inventory)
    parent = document["components"][0]["bom-ref"]
    assert next(d for d in bound["dependencies"] if d["ref"] == parent)["dependsOn"] == [document["components"][1]["bom-ref"]]
    bound["dependencies"] = [d for d in bound["dependencies"] if d["ref"] != parent]
    with pytest.raises(EvidenceError, match="missing-resolved-dependency-edge"):
        review_inventory(inventory, bound, policy, tmp_path, tools, REVISION, TODAY)


def test_stale_source_revision_rejected(evidence, tmp_path, tools):
    inventory, document, policy = evidence
    with pytest.raises(EvidenceError, match="stale-source-revision"):
        review_inventory(inventory, bind_evidence(document, inventory), policy, tmp_path, tools, "d" * 40, TODAY)


def test_file_and_os_components_are_retained_and_bound(evidence, tools):
    inventory, document, _ = evidence
    inventory["file_components"] = [{"id": "file-id", "location": {"path": "/lib/synthetic.so"},
                                      "digests": [{"algorithm": "sha256", "value": "e" * 64}]}]
    inventory["distro"] = {"id": "synthetic", "versionID": "1"}
    document["components"].extend([
        {"type": "file", "bom-ref": "file-id", "name": "/lib/synthetic.so",
         "hashes": [{"alg": "SHA-256", "content": "e" * 64}]},
        {"type": "operating-system", "bom-ref": "os:synthetic@1", "name": "synthetic", "version": "1"},
    ])
    bound = bind_evidence(document, inventory)
    validate_cyclonedx(bound, tools)
    assert len(bound["components"]) == 3
    document["components"][1]["hashes"][0]["content"] = "f" * 64
    with pytest.raises(EvidenceError, match="file-component-hash-drift"):
        bind_evidence(document, inventory)


def test_package_reference_preserves_purl_subpath():
    assert package_ref({"bom-ref": "pkg:golang/example@v1?package-id=synthetic#sub/path"}) == "synthetic"


def test_current_spdx_ids_validate_without_relabeling(evidence, tools):
    doc = evidence[1]
    doc["components"][0]["licenses"] = [{"license": {"id": "SMAIL-GPL"}}, {"license": {"id": "Artistic-dist"}}]
    validate_cyclonedx(doc, tools)


def test_version_prefix_is_not_exact_review(evidence):
    policy = evidence[2]
    policy["reviews"][0]["version"] = "1.2"
    with pytest.raises(EvidenceError, match="unversioned-review"):
        validate_policy(policy, TODAY)


def test_real_pinned_generator_and_offline_schema(tmp_path, tools):
    source = tmp_path / "input"
    source.mkdir()
    write(source / "node_modules" / "synthetic-fixture" / "package.json",
          {"name": "synthetic-fixture", "version": "1.2.3", "license": "MIT"})
    syft = tools / ("syft.exe" if os.name == "nt" else "syft")
    documents = []
    for number in range(2):
        path = tmp_path / f"{number}.cdx.json"
        run([str(syft), "scan", "dir:" + str(source),
             "--config", str(Path(__file__).parents[1] / "syft.yaml"),
             "--select-catalogers", "+javascript-package-cataloger",
             "--source-name", "synthetic-fixture", "--source-version", REVISION,
             "-o", "cyclonedx-json@1.6=" + str(path)])
        doc = load(path)
        validate_cyclonedx(doc, tools)
        assert any(c.get("name") == "synthetic-fixture" and c.get("version") == "1.2.3" for c in doc["components"])
        documents.append(normalize(doc))
    assert documents[0] == documents[1]
