from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import tomllib
import tarfile
import zipfile
from contextlib import ExitStack
from pathlib import Path

from scripts.supply_chain.adapters import (
    Blobs, ImageFiles, dotnet_inventory, enrich_nuget, file_fact, os_inventory,
    pnpm_inventory, python_edges, python_inventory, upstream_npm, upstream_wheels,
)
from scripts.supply_chain.common import (
    EvidenceError, HERE, ROOT, canonical, digest, load, require, run, sha_file, write,
)
from scripts.supply_chain.inventory import compile_inventory, reconcile
from scripts.supply_chain.review import review_inventory
from scripts.supply_chain.standards import bind_evidence, normalize, validate_cyclonedx
from scripts.supply_chain.tool_evidence import collect_tools

TARGETS = {
    "semantic-api": ("services/semantic-api", "services/semantic-api/Dockerfile", False),
    "semantic-backend": (".", "services/semantic-backend/Dockerfile", True),
    "control-api": ("services/control-api", "services/control-api/Dockerfile", True),
    "web": ("apps/web", "apps/web/Dockerfile", True),
}


def source_provenance(revision: str) -> dict:
    require(re.fullmatch("[0-9a-f]{40}", revision), "invalid-source-revision")
    require(run(["git", "rev-parse", "HEAD"], cwd=ROOT).strip() == revision, "source-revision-drift")
    require(not run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT).strip(), "dirty-source-tree")
    paths = run(["git", "ls-files", "-z"], cwd=ROOT).split("\0")
    inputs = [{"path": p, "sha256": sha_file(ROOT / p)} for p in paths if p]
    return {"revision": revision, "tree": run(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT).strip(), "inputs": inputs}


def local_python_names() -> set[str]:
    names = set()
    for parent in ("services", "connectors"):
        for path in (ROOT / parent).glob("*/pyproject.toml"):
            names.add(tomllib.loads(path.read_text())["project"]["name"])
    return names


def snapshot_image(tag: str, name: str, work: Path, tools: Path, output: Path, revision: str) -> tuple[dict, dict, ImageFiles, dict]:
    image_id = run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"]).strip()
    require(re.fullmatch("sha256:[0-9a-f]{64}", image_id), "invalid-image-identity")
    archive = work / (name + ".image.tar")
    run(["docker", "save", "--output", str(archive), image_id])
    syft = tools / ("syft.exe" if os.name == "nt" else "syft")
    raw_path, cdx_path = work / (name + ".syft.json"), work / (name + ".raw.cdx.json")
    args = [
        str(syft), "scan", "docker-archive:" + str(archive),
        "--config", str(HERE / "syft.yaml"), "--source-name", name, "--source-version", revision,
        "-o", "syft-json=" + str(raw_path), "-o", "cyclonedx-json@1.6=" + str(cdx_path),
    ]
    if name == "web-build":
        args += ["--select-catalogers", "+javascript-lock-cataloger"]
    run(args)
    raw, cdx = load(raw_path), load(cdx_path)
    require(raw["descriptor"]["version"] == load(HERE / "tools.json")["syft"]["version"], "unexpected-syft-version")
    # Keep cataloger facts, not host paths, Docker configuration, or full environment.
    metadata = raw["source"]["metadata"]
    require(metadata.get("imageID") == image_id, "scanner-image-identity-drift")
    raw["source"]["metadata"] = {key: metadata[key] for key in (
        "imageID", "manifestDigest", "mediaType", "tags", "repoDigests", "architecture",
        "os", "osVersion", "variant", "layers", "size",
    ) if key in metadata}
    raw["descriptor"].pop("configuration", None)
    raw["descriptor"]["configuration_sha256"] = sha_file(HERE / "syft.yaml")
    raw["descriptor"]["pins_sha256"] = sha_file(HERE / "tools.json")
    write(output / (name + ".syft.json"), raw)
    write(output / (name + ".generator.cdx.json"), cdx)
    validate_cyclonedx(cdx, tools)
    subject = {
        "name": name, "image_id": image_id, "platform": "linux/amd64",
        "image_archive_sha256": sha_file(archive), "filesystem_view": "saved-image-layer-overlay",
        "syft_sha256": digest(canonical(raw)),
        "syft_executable_sha256": sha_file(syft),
    }
    return raw, cdx, ImageFiles.from_docker_archive(archive, image_id), subject


def collect(target: str, output: Path, tools: Path, revision: str) -> None:
    require(not output.exists(), "output-already-exists")
    source = source_provenance(revision)
    context, dockerfile, has_build = TARGETS[target]
    output.mkdir(parents=True)
    blobs = Blobs(output / "blobs")
    write(output / "source.json", source)
    collect_tools(output, tools)
    toolchain = load(output / "toolchain.json")
    toolchain["build_engine"] = {
        "docker_client_server": run(["docker", "version", "--format", "{{.Client.Version}}/{{.Server.Version}}"]).strip(),
        "buildx": run(["docker", "buildx", "version"]).strip(),
    }
    write(output / "toolchain.json", toolchain)
    with ExitStack() as cleanup:
        temporary = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="nexus-supply-chain-"))
        with ExitStack() as images:
            work = Path(temporary)
            snapshots = {}
            for stage in (["build", "runtime"] if has_build else ["runtime"]):
                name = target + "-" + stage
                tag = "nexus-evidence/" + name + ":" + revision
                args = ["docker", "build", "--platform", "linux/amd64", "--file", str(ROOT / dockerfile), "--tag", tag]
                if stage == "build":
                    args += ["--target", "build"]
                run(args + [str(ROOT / context)])
                snapshot = snapshot_image(tag, name, work, tools, output, revision)
                snapshots[stage] = (*snapshot, tag)
                images.callback(snapshot[2].close)
            runtime_raw, runtime_cdx, runtime_files, runtime_subject, runtime_tag = snapshots["runtime"]
            runtime_records = os_inventory(runtime_files, blobs)
            runtime_edges, runtime_gaps = [], []
            resolution_context = {}
            relations = []
            build_extra = []
            build_edges = []
            if target in {"semantic-api", "semantic-backend"}:
                records = python_inventory(runtime_files, blobs)
                require(records, "empty-python-environment")
                # Read interpreter facts only; do not import installed product modules.
                code = (
                    "import json,sys,platform; print(json.dumps({"
                    "'implementation_name':sys.implementation.name,"
                    "'implementation_version':platform.python_version(),"
                    "'os_name':'posix','platform_machine':platform.machine(),"
                    "'platform_release':platform.release(),'platform_system':'Linux',"
                    "'platform_version':platform.version(),"
                    "'python_full_version':platform.python_version(),"
                    "'platform_python_implementation':platform.python_implementation(),"
                    "'python_version':'.'.join(platform.python_version_tuple()[:2]),"
                    "'sys_platform':'linux'}))"
                )
                environment = json.loads(run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", runtime_tag, "-I", "-S", "-c", code]))
                extras = {"semantic-backend": ["databricks"]} if target == "semantic-backend" else {}
                resolution_context = {"marker_environment": environment, "extras": extras, "introspection": "python -I -S"}
                runtime_edges = python_edges(records, environment, extras)
                upstream_wheels(records, runtime_files, blobs, local_python_names())
                for record in records:
                    if "archive_gap" in record:
                        record["upstream"] = [{"source_revision": revision, "source_tree": source["tree"]}]
                        runtime_gaps.append(record["archive_gap"])
                runtime_records += records
            elif target == "control-api":
                records, runtime_edges = dotnet_inventory(runtime_files, blobs)
                require(records, "empty-dotnet-publish")
                enrich_nuget(records, snapshots["build"][2], blobs)
                for record in records:
                    if record["package_type"] == "project":
                        record["upstream"] = [{"source_revision": revision, "source_tree": source["tree"]}]
                runtime_records += records
                resolution_context = {
                    "sdk": run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "dotnet", snapshots["build"][4], "--list-sdks"]).strip(),
                    "runtimes": run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "dotnet", runtime_tag, "--list-runtimes"]).strip(),
                }
            elif target == "web":
                build_raw, _, build_files, build_subject, build_tag = snapshots["build"]
                tree = json.loads(run([
                    "docker", "run", "--rm", "--network", "none", "--entrypoint", "pnpm",
                    build_tag, "list", "--prod", "--depth", "Infinity", "--json",
                ]))
                build_extra, build_edges = pnpm_inventory(tree, build_files, blobs)
                resolution_context = {
                    "node": run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "node", build_tag, "--version"]).strip(),
                    "pnpm": run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "pnpm", build_tag, "--version"]).strip(),
                }
                project_name = json.loads(build_files.read("/app/package.json"))["name"]
                upstream_npm(build_extra, build_files, build_raw, project_name)
                for record in build_extra:
                    if record["name"] == project_name:
                        record["upstream"] = [{"source_revision": revision, "source_tree": source["tree"]}]
                bundle_files = []
                for path in build_files.paths():
                    if path.startswith("/app/dist/"):
                        final_path = "/usr/share/nginx/html/" + path[len("/app/dist/"):]
                        require(runtime_files.has(final_path), "missing-distributed-web-bundle")
                        require(digest(build_files.read(path)) == digest(runtime_files.read(final_path)), "web-bundle-copy-drift")
                        bundle_files.append({
                            "input": file_fact(build_files, path, blobs),
                            "output": file_fact(runtime_files, final_path, blobs),
                        })
                require(bundle_files, "missing-web-build-output")
                relations.append({
                    "type": "bundled-production-inputs", "from": build_subject["image_id"],
                    "to": runtime_subject["image_id"], "production": [
                        {"name": r["name"], "version": r["version"]} for r in build_extra
                    ], "bundle_files": bundle_files,
                    "lock": file_fact(build_files, "/app/pnpm-lock.yaml", blobs, retain=True),
                    "manifest": file_fact(build_files, "/app/package.json", blobs, retain=True),
                    "limitation": "Conservative production input closure, not per-symbol tree-shaking attribution.",
                })
            subjects = []
            for stage, (raw, cdx, files, subject, _) in snapshots.items():
                records = runtime_records if stage == "runtime" else os_inventory(files, blobs) + build_extra
                gaps = runtime_gaps if stage == "runtime" else ["build-ephemeral-install-environments-not-retained"]
                edges = runtime_edges if stage == "runtime" else build_edges
                inventory = compile_inventory(raw, files, blobs, records, source=source, subject=subject,
                                              scope=stage, gaps=gaps, edges=edges)
                inventory["subject_relationships"] = list(relations)
                inventory["resolution_context"] = resolution_context
                if has_build:
                    inventory["subject_relationships"].append({
                        "type": "built-from", "from": snapshots["build"][3]["image_id"],
                        "to": runtime_subject["image_id"],
                    })
                document = bind_evidence(cdx, inventory)
                validate_cyclonedx(document, tools)
                require(canonical(normalize(document)) == canonical(document), "non-idempotent-normalization")
                write(output / (subject["name"] + ".inventory.json"), inventory)
                write(output / (subject["name"] + ".cdx.json"), document)
                subjects.append(subject["name"])
            write(output / "bundle.json", {
                "source_revision": revision, "subjects": subjects, "target": target,
                "files": {p.relative_to(output).as_posix(): sha_file(p) for p in sorted(output.rglob("*")) if p.is_file()},
            })


def accept(output: Path, tools: Path, revision: str, policy_path: Path) -> bool:
    require(re.fullmatch("[0-9a-f]{40}", revision), "invalid-source-revision")
    write(output / "acceptance.json", {
        "status": "INVALID", "source_revision": revision, "reason": "evaluation-not-complete",
    })
    bundle = load(output / "bundle.json")
    require(bundle["source_revision"] == revision and bundle["subjects"], "stale-bundle")
    require(len(bundle["subjects"]) == len(set(bundle["subjects"])), "duplicate-subject")
    for path, expected in bundle["files"].items():
        require(re.fullmatch(r"[a-zA-Z0-9_.\-/]+", path) and not path.startswith("/")
                and ".." not in path.split("/"), "unsafe-evidence-path")
        require(sha_file(output / path) == expected, "bundle-file-hash-drift")
    required = {"source.json", "toolchain.json"}
    for subject in bundle["subjects"]:
        require(re.fullmatch("[a-z-]+", subject), "invalid-subject-name")
        required.update(subject + suffix for suffix in (".inventory.json", ".cdx.json", ".generator.cdx.json", ".syft.json"))
    require(required <= set(bundle["files"]), "missing-bundle-file")
    source = load(output / "source.json")
    require(source["revision"] == revision and source["inputs"], "missing-source-provenance")
    toolchain = load(output / "toolchain.json")
    require(toolchain["syft"] == load(HERE / "tools.json")["syft"], "tool-provenance-drift")
    policy = load(policy_path)
    reports = []
    for subject in bundle["subjects"]:
        inventory = load(output / (subject + ".inventory.json"))
        raw = load(output / (subject + ".syft.json"))
        require(inventory["subject"]["syft_sha256"] == digest(canonical(raw)), "scanner-facts-drift")
        require(inventory["inputs"] == source["inputs"] and inventory["source_tree"] == source["tree"], "source-input-drift")
        require(inventory["coverage"] == reconcile(raw, inventory["resolved"]), "resolved-coverage-drift")
        require({c["id"] for c in inventory["components"]} == {p["id"] for p in raw["artifacts"]}, "scanner-component-drift")
        document = load(output / (subject + ".cdx.json"))
        replay = bind_evidence(load(output / (subject + ".generator.cdx.json")), inventory)
        require((output / (subject + ".cdx.json")).read_bytes() == canonical(replay), "nonreproducible-document")
        reports.append(review_inventory(
            inventory, document,
            policy, output, tools, revision, dt.datetime.now(dt.timezone.utc).date(),
        ))
    status = "ACCEPTED" if all(r["status"] == "ACCEPTED" for r in reports) else "BLOCKED"
    write(output / "acceptance.json", {"status": status, "source_revision": revision, "subjects": reports})
    print("Release acceptance: " + status)
    return status == "ACCEPTED"


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventory actual private CI artifacts; never publish images.")
    parser.add_argument("command", choices=["collect", "accept"])
    parser.add_argument("--target", choices=TARGETS)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tools", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--policy", type=Path, default=HERE / "review-policy.json")
    args = parser.parse_args()
    try:
        if args.command == "collect":
            require(args.target in TARGETS, "missing-target")
            collect(args.target, args.output, args.tools, args.revision)
            print("Inventory: COMPLETE")
            return 0
        return 0 if accept(args.output, args.tools, args.revision, args.policy) else 2
    except EvidenceError as error:
        print("Supply-chain error: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, LookupError, EOFError, tarfile.TarError, zipfile.BadZipFile):
        print("Supply-chain error: invalid-or-unavailable-evidence", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
