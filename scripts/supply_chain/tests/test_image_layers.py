from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from scripts.supply_chain.adapters import ImageFiles
from scripts.supply_chain.common import EvidenceError, canonical


def layer(entries: list[tuple[str, bytes | tuple[str, bytes]]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, value in entries:
            member = tarfile.TarInfo(name)
            if isinstance(value, tuple):
                member.linkname, member.type = value
                archive.addfile(member)
            else:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
    return stream.getvalue()


def image(tmp_path: Path, layers: list[bytes]) -> ImageFiles:
    path = tmp_path / "image.tar"
    names = [f"{i}.tar" for i in range(len(layers))]
    with tarfile.open(path, "w") as archive:
        values = {"manifest.json": canonical([{"Layers": names}]), **dict(zip(names, layers, strict=True))}
        for name, data in values.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return ImageFiles.from_docker_archive(path)


def test_saved_image_preserves_files_hidden_by_container_mounts(tmp_path):
    files = image(tmp_path, [layer([("etc/hosts", b"synthetic image hosts"), ("etc/hostname", b"synthetic image hostname")])])
    try:
        assert files.read("/etc/hosts") == b"synthetic image hosts"
        assert files.read("/etc/hostname") == b"synthetic image hostname"
    finally:
        files.close()


def test_whiteouts_only_affect_lower_layers_and_gzip_is_supported(tmp_path):
    base = layer([("keep", b"base"), ("removed", b"old"), ("opaque/old", b"old"), ("replace", b"old")])
    upper = layer([("opaque/new", b"new"), ("opaque/.wh..wh..opq", b""), (".wh.removed", b""),
                   ("replace", b"new"), (".wh.replace", b"")])
    files = image(tmp_path, [base, gzip.compress(upper)])
    try:
        assert files.read("/keep") == b"base"
        assert not files.has("/removed")
        assert not files.has("/opaque/old")
        assert files.read("/opaque/new") == files.read("/replace") == b"new"
        assert not any(".wh." in path for path in files.paths())
    finally:
        files.close()


def test_hardlinks_retain_inode_when_target_replaced_and_symlinks_stay_inside_image(tmp_path):
    base = layer([("original", b"old"), ("hard", ("original", tarfile.LNKTYPE)),
                  ("alias", ("/original", tarfile.SYMTYPE)),
                  ("bin", ("usr/bin", tarfile.SYMTYPE))])
    upper = layer([("original", b"new"), ("bin/tool", b"tool")])
    files = image(tmp_path, [base, upper])
    try:
        assert files.read("/hard") == b"old"
        assert files.read("/alias") == b"new"
        assert files.read("/usr/bin/tool") == files.read("/bin/tool") == b"tool"
    finally:
        files.close()


@pytest.mark.parametrize("entries", [
    [("../outside", b"not extracted")],
    [("same", b"one"), ("same", b"two")],
    [("a", ("b", tarfile.SYMTYPE)), ("b", ("a", tarfile.SYMTYPE)), ("a/file", b"cycle")],
])
def test_unsafe_or_ambiguous_layers_fail_closed(tmp_path, entries):
    with pytest.raises((EvidenceError, ValueError)):
        image(tmp_path, [layer(entries)])
    assert not (tmp_path.parent / "outside").exists()
