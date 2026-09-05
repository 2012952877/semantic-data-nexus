"""Download only checksum-pinned tooling and schemas; never execute installers."""
from __future__ import annotations

import argparse
import io
import platform
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from scripts.supply_chain.common import HERE, digest, load, require


def fetch(url: str, expected: str) -> bytes:
    require(url.startswith("https://"), "insecure-download")
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    require(digest(data) == expected, "download-hash-mismatch")
    return data


def bootstrap(destination: Path) -> None:
    pins = load(HERE / "tools.json")
    system = platform.system().lower()
    require(platform.machine().lower() in {"amd64", "x86_64"}, "unsupported-platform")
    require(system in {"linux", "windows"}, "unsupported-platform")
    destination.mkdir(parents=True, exist_ok=True)
    pin = pins["syft"][f"{system}_amd64"]
    archive = fetch(pin["url"], pin["sha256"])
    name = "syft.exe" if system == "windows" else "syft"
    if system == "windows":
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            executable = bundle.read(name)
    else:
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            member = bundle.extractfile(name)
            require(member is not None, "missing-tool-executable")
            executable = member.read()
    (destination / name).write_bytes(executable)
    (destination / name).chmod(0o755)
    license_data = fetch(pins["syft"]["license_url"], pins["syft"]["license_sha256"])
    (destination / "syft-LICENSE").write_bytes(license_data)
    for filename, sha in pins["schema"]["files"].items():
        (destination / filename).write_bytes(fetch(pins["schema"]["base_url"] + filename, sha))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", required=True, type=Path)
    bootstrap(parser.parse_args().tools)
