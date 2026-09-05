from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

from scripts.supply_chain.adapters import Blobs, license_path
from scripts.supply_chain.common import HERE, load, require, sha_file, write


def collect_tools(output: Path, tools: Path) -> None:
    blobs = Blobs(output / "blobs")
    packages = []
    for pin in load(HERE / "python-tools.json")["packages"]:
        if pin["name"] == "colorama" and sys.platform != "win32":
            continue
        dist = importlib.metadata.distribution(pin["name"])
        require(dist.version == pin["version"], "tool-version-drift")
        licenses = []
        for path in dist.files or []:
            if ".dist-info/" in str(path).replace("\\", "/") and license_path(str(path)):
                data = dist.locate_file(path).read_bytes()
                licenses.append({"path": str(path).replace("\\", "/"), "blob": blobs.add(data)})
        require(licenses, "missing-tool-license")
        packages.append({
            "name": pin["name"], "version": pin["version"], "declared": pin["declared"],
            "metadata_url": pin["metadata_url"], "license_files": licenses,
            "scope": "inventory-tool-only", "review": "metadata-inspected-not-legal-clearance",
        })
    write(output / "toolchain.json", {
        "syft": load(HERE / "tools.json")["syft"],
        "syft_license_blob": blobs.add((tools / "syft-LICENSE").read_bytes()),
        "schemas": load(HERE / "tools.json")["schema"],
        "python_packages": packages, "python_version": sys.version.split()[0],
        "requirements_lock_sha256": sha_file(HERE / "requirements.lock"),
        "python_tools_sha256": sha_file(HERE / "python-tools.json"),
    })
