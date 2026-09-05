from __future__ import annotations

import base64
import csv
import email
import hashlib
import io
import json
import posixpath
import re
import tarfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

from scripts.supply_chain.common import canonical, digest, require


def image_path(path: str) -> str:
    require("\\" not in path and "\x00" not in path, "invalid-image-path")
    return posixpath.normpath("/" + path.lstrip("/"))


class ImageFiles:
    """Read an exported filesystem without extracting or following host symlinks."""

    def __init__(self, path: Path):
        self.archive = tarfile.open(path)
        self.members = {image_path(m.name): m for m in self.archive.getmembers()}

    def close(self) -> None:
        self.archive.close()

    def resolve(self, path: str) -> str:
        path = image_path(path)
        for _ in range(40):
            parts = path.strip("/").split("/")
            for index in range(len(parts)):
                prefix = "/" + "/".join(parts[:index + 1])
                member = self.members.get(prefix)
                if member and (member.issym() or member.islnk()):
                    target = member.linkname
                    if member.issym() and not target.startswith("/"):
                        target = posixpath.join(posixpath.dirname(prefix), target)
                    path = image_path(posixpath.join(target, *parts[index + 1:]))
                    break
            else:
                return path
        raise ValueError("image-link-cycle")

    def has(self, path: str) -> bool:
        return self.resolve(path) in self.members

    def read(self, path: str) -> bytes:
        member = self.members.get(self.resolve(path))
        require(member is not None and member.isfile(), "missing-artifact-file")
        stream = self.archive.extractfile(member)
        require(stream is not None, "missing-artifact-file")
        with stream:
            return stream.read()

    def paths(self) -> list[str]:
        return sorted(p for p, m in self.members.items() if m.isfile())


class Blobs:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def add(self, data: bytes) -> str:
        sha = digest(data)
        (self.directory / sha).write_bytes(data)
        return sha


def file_fact(files: ImageFiles, path: str, blobs: Blobs, *, retain: bool = False) -> dict:
    data = files.read(path)
    fact = {"path": image_path(path), "sha256": digest(data), "size": len(data)}
    if retain:
        fact["blob"] = blobs.add(data)
    return fact


def license_path(path: str) -> bool:
    name = posixpath.basename(path).lower()
    return bool(re.match(r"^(licen[sc]e|copying|copyright|notice|third[-_]?party)([._-]|$)", name))


def python_inventory(files: ImageFiles, blobs: Blobs) -> list[dict]:
    result = []
    for path in files.paths():
        if not path.endswith(".dist-info/METADATA"):
            continue
        metadata = email.message_from_bytes(files.read(path))
        name, version = metadata["Name"], metadata["Version"]
        require(name and version, "invalid-python-metadata")
        folder = posixpath.dirname(path)
        root = posixpath.dirname(folder)
        record_path = folder + "/RECORD"
        wheel_path = folder + "/WHEEL"
        require(files.has(record_path) and files.has(wheel_path), "missing-wheel-metadata")
        owned = []
        for relative, encoded, size in csv.reader(io.StringIO(files.read(record_path).decode())):
            target = image_path(posixpath.join(root, relative))
            require(files.has(target), "missing-wheel-file")
            if encoded:
                algorithm, expected = encoded.split("=", 1)
                require(algorithm in {"sha256", "sha384", "sha512"}, "weak-wheel-hash")
                data = files.read(target)
                actual = base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest()).decode().rstrip("=")
                require(actual == expected and len(data) == int(size), "wheel-record-hash-drift")
            if not target.endswith(".pyc"):
                owned.append(file_fact(files, target, blobs, retain=license_path(target)))
        result.append({
            "ecosystem": "pypi", "name": str(canonicalize_name(name)), "version": version,
            "location": path, "metadata": file_fact(files, path, blobs),
            "wheel": file_fact(files, wheel_path, blobs, retain=True),
            "record": file_fact(files, record_path, blobs, retain=True),
            "declared": metadata.get_all("License-Expression", []) + metadata.get_all("License", []),
            "license_files": [f for f in owned if "blob" in f],
            "files": owned, "requires": metadata.get_all("Requires-Dist", []),
            "upstream": metadata.get_all("Project-URL", []) + metadata.get_all("Home-page", []),
        })
    return result


def python_edges(records: list[dict], environment: dict, extras: dict[str, list[str]]) -> list[list[str]]:
    by_name = {r["name"]: r for r in records}
    require(len(by_name) == len(records), "ambiguous-python-environment")
    selected = {name: set(value) for name, value in extras.items()}
    edges = set()
    changed = True
    while changed:
        changed = False
        for record in records:
            for text in record["requires"]:
                req = Requirement(text)
                active = not req.marker or any(
                    req.marker.evaluate({**environment, "extra": extra})
                    for extra in {"", *selected.get(record["name"], set())}
                )
                if not active:
                    continue
                name = str(canonicalize_name(req.name))
                require(name in by_name, "missing-python-transitive")
                target = by_name[name]
                require(req.specifier.contains(target["version"]), "python-version-conflict")
                edges.add((record["name"], name))
                old = selected.setdefault(name, set())
                if not req.extras <= old:
                    old.update(req.extras)
                    changed = True
    return [list(edge) for edge in sorted(edges)]


def pnpm_inventory(tree: list[dict], files: ImageFiles, blobs: Blobs) -> tuple[list[dict], list[list[str]]]:
    require(len(tree) == 1, "invalid-pnpm-root")
    records, edges = {}, set()

    def visit(node: dict, parent: str | None = None, alias: str | None = None) -> None:
        location = node.get("path", "")
        require(location.startswith("/app"), "invalid-pnpm-path")
        manifest_path = location + "/package.json"
        data = json.loads(files.read(manifest_path))
        name, version = data["name"], data["version"]
        require(not alias or node["version"] == version, "pnpm-version-conflict")
        identity = name + "@" + version
        if parent:
            edges.add((parent, identity))
        if identity not in records:
            folder = files.resolve(location)
            owned = [p for p in files.paths() if p.startswith(folder + "/") and "/node_modules/" not in p[len(folder) + 1:]]
            records[identity] = {
                "ecosystem": "npm", "name": name, "version": version,
                "location": files.resolve(manifest_path),
                "metadata": file_fact(files, manifest_path, blobs, retain=True),
                "declared": [data["license"]] if isinstance(data.get("license"), str) else [],
                "license_files": [file_fact(files, p, blobs, retain=True) for p in owned if license_path(p)],
                "files": [file_fact(files, p, blobs) for p in owned],
                "upstream": [data.get("repository", {}), data.get("homepage", "")],
            }
        for group in ("dependencies", "optionalDependencies"):
            for child_name, child in node.get(group, {}).items():
                visit(child, identity, child_name)

    visit(tree[0])
    return sorted(records.values(), key=lambda r: (r["name"], r["version"])), [list(e) for e in sorted(edges)]


def dotnet_inventory(files: ImageFiles, blobs: Blobs, prefix: str = "/app/") -> tuple[list[dict], list[list[str]]]:
    records, edges = [], set()
    for path in files.paths():
        if not path.startswith(prefix) or not path.endswith(".deps.json"):
            continue
        doc = json.loads(files.read(path))
        target = doc["targets"][doc["runtimeTarget"]["name"]]
        for identity, entry in target.items():
            name, version = identity.rsplit("/", 1)
            library = doc["libraries"][identity]
            owned = []
            for kind in ("runtime", "native", "resources"):
                for claimed in entry.get(kind, {}):
                    if posixpath.basename(claimed) == "_._":
                        continue
                    relative = posixpath.basename(claimed)
                    if kind == "resources":
                        relative = posixpath.basename(posixpath.dirname(claimed)) + "/" + relative
                    actual = posixpath.dirname(path) + "/" + relative
                    require(files.has(actual), "missing-published-runtime-file")
                    owned.append({**file_fact(files, actual, blobs), "package_path": claimed})
            for dep, dep_version in entry.get("dependencies", {}).items():
                require(dep + "/" + dep_version in target, "missing-dotnet-transitive")
                edges.add((identity, dep + "/" + dep_version))
            records.append({
                "ecosystem": "nuget", "name": name, "version": version,
                "location": path, "metadata": file_fact(files, path, blobs, retain=True),
                "package_type": library["type"], "package_sha512": library.get("sha512", ""),
                "files": owned, "declared": [], "license_files": [], "upstream": [],
            })
    return records, [list(e) for e in sorted(edges)]


def enrich_nuget(records: list[dict], build_files: ImageFiles, blobs: Blobs) -> None:
    paths = build_files.paths()
    for record in records:
        if record["package_type"] != "package":
            continue
        name, version = record["name"].lower(), record["version"].lower()
        suffix = f"/{name}/{version}/{name}.{version}.nupkg"
        matches = [p for p in paths if p.endswith(suffix)]
        require(len(matches) == 1, "missing-nuget-archive")
        data = build_files.read(matches[0])
        url = f"https://api.nuget.org/v3-flatcontainer/{name}/{version}/{name}.{version}.nupkg"
        # NuGet restore contentHash is NOT the raw ZIP hash for signed packages.
        # Compare exact public archive bytes instead of inventing that algorithm.
        with urllib.request.urlopen(url, timeout=120) as response:
            upstream_data = response.read()
        require(digest(data) == digest(upstream_data), "nuget-archive-hash-drift")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for fact in record["files"]:
                require(digest(archive.read(fact["package_path"])) == fact["sha256"], "nuget-publish-hash-drift")
            nuspec = [p for p in archive.namelist() if p.endswith(".nuspec")]
            require(len(nuspec) == 1, "missing-nuspec")
            xml = archive.read(nuspec[0])
            root = ElementTree.fromstring(xml)
            declared = [n.text for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "license" and n.text]
            record["declared"] = declared
            record["nuspec"] = {"blob": blobs.add(xml), "sha256": digest(xml)}
            for path in archive.namelist():
                if license_path(path) and not path.endswith("/"):
                    text = archive.read(path)
                    record["license_files"].append({"path": path, "sha256": digest(text), "blob": blobs.add(text)})
        record["archive"] = {
            "url": url,
            "sha256": digest(data), "sha512": hashlib.sha512(data).hexdigest(),
            "restore_content_hash": record["package_sha512"],
            "verification": "public-archive-bytes-and-published-files",
        }


def os_inventory(files: ImageFiles, blobs: Blobs) -> list[dict]:
    if files.has("/var/lib/dpkg/status"):
        path = "/var/lib/dpkg/status"
        records = []
        for block in files.read(path).decode().split("\n\n"):
            metadata = email.message_from_string(block)
            if metadata.get("Status") == "install ok installed":
                records.append({"name": metadata["Package"], "version": metadata["Version"], "ecosystem": "deb"})
    else:
        path = "/lib/apk/db/installed"
        require(files.has(path), "missing-os-package-database")
        records = []
        for block in files.read(path).decode().split("\n\n"):
            metadata = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
            if "P" in metadata:
                records.append({"name": metadata["P"], "version": metadata["V"], "ecosystem": "apk"})
    require(records, "empty-os-package-database")
    fact = file_fact(files, path, blobs, retain=True)
    return [{**r, "metadata": fact} for r in records]


def upstream_wheels(records: list[dict], files: ImageFiles, blobs: Blobs, local_names: set[str]) -> None:
    """Reconstruct public wheel evidence only when its bytes match the installed RECORD."""
    for record in records:
        if record["name"] in local_names:
            record["archive_gap"] = "first-party-wheel-not-retained-by-product-build"
            continue
        name, version = record["name"], record["version"]
        url = f"https://pypi.org/pypi/{urllib.parse.quote(name, safe='')}/{urllib.parse.quote(version, safe='')}/json"
        with urllib.request.urlopen(url, timeout=120) as response:
            release = json.load(response)
        wheel = email.message_from_bytes(files.read(posixpath.dirname(record["location"]) + "/WHEEL"))
        tags = set(wheel.get_all("Tag", []))
        candidates = []
        for item in release["urls"]:
            if item["packagetype"] == "bdist_wheel":
                _, _, _, candidate_tags = parse_wheel_filename(item["filename"])
                if tags & {str(tag) for tag in candidate_tags}:
                    candidates.append(item)
        require(len(candidates) == 1, "ambiguous-upstream-wheel")
        candidate = candidates[0]
        parsed = urllib.parse.urlsplit(candidate["url"])
        require(parsed.scheme == "https" and parsed.hostname == "files.pythonhosted.org" and not parsed.username, "unsafe-wheel-origin")
        with urllib.request.urlopen(candidate["url"], timeout=120) as response:
            data = response.read()
        require(digest(data) == candidate["digests"]["sha256"], "upstream-wheel-hash-drift")
        root = posixpath.dirname(posixpath.dirname(record["location"]))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            record_files = [p for p in archive.namelist() if p.endswith(".dist-info/RECORD")]
            require(len(record_files) == 1, "missing-upstream-wheel-record")
            wheel_record = archive.read(record_files[0])
            for relative, encoded, _ in csv.reader(io.StringIO(wheel_record.decode())):
                if not encoded:
                    continue
                require(".data/" not in relative, "unsupported-wheel-data-mapping")
                actual = image_path(posixpath.join(root, relative))
                require(files.has(actual), "missing-reconstructed-wheel-file")
                require(digest(archive.read(relative)) == digest(files.read(actual)), "reconstructed-wheel-hash-drift")
        record["archive"] = {
            "url": candidate["url"], "metadata_url": url,
            "sha256": digest(data), "filename": candidate["filename"],
            "verification": "public-wheel-bytes-match-installed-record",
            "record_blob": blobs.add(wheel_record),
        }


def upstream_npm(records: list[dict], files: ImageFiles, syft: dict, local_name: str) -> None:
    locked = {}
    for package in syft["artifacts"]:
        if package.get("metadataType") == "javascript-pnpm-lock-entry":
            integrity = package.get("metadata", {}).get("resolution", {}).get("integrity")
            if integrity:
                locked.setdefault((package["name"], package["version"]), set()).add(integrity)
    for record in records:
        if record["name"] == local_name:
            continue
        name, version = record["name"], record["version"]
        integrity = locked.get((name, version), set())
        require(len(integrity) == 1, "missing-or-conflicting-pnpm-integrity")
        expected = next(iter(integrity))
        url = f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='')}/{urllib.parse.quote(version, safe='')}"
        with urllib.request.urlopen(url, timeout=120) as response:
            metadata = json.load(response)
        archive_url = metadata["dist"]["tarball"]
        parsed = urllib.parse.urlsplit(archive_url)
        require(parsed.scheme == "https" and parsed.hostname == "registry.npmjs.org" and not parsed.username, "unsafe-npm-origin")
        with urllib.request.urlopen(archive_url, timeout=120) as response:
            data = response.read()
        algorithm, encoded = expected.split("-", 1)
        require(algorithm in {"sha1", "sha256", "sha512"}, "unsupported-npm-integrity")
        require(base64.b64encode(hashlib.new(algorithm, data).digest()).decode() == encoded, "pnpm-archive-hash-drift")
        folder = posixpath.dirname(record["location"])
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                require(member.name.startswith("package/") and ".." not in member.name.split("/"), "unsafe-npm-archive-path")
                actual = folder + "/" + member.name[len("package/"):]
                require(files.has(actual), "missing-npm-installed-file")
                stream = archive.extractfile(member)
                require(stream is not None and digest(stream.read()) == digest(files.read(actual)), "npm-installed-hash-drift")
        record["archive"] = {
            "url": archive_url, "metadata_url": url, "sha256": digest(data),
            "lock_integrity": expected, "verification": "pnpm-lock-integrity-and-installed-files",
        }
