from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from scripts.supply_chain.adapters import ImageFiles
from scripts.supply_chain.common import EvidenceError, canonical, digest


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


def image(tmp_path: Path, layers: list[bytes], diff_ids: list[str] | None = None,
          names: list[str] | None = None, omit: str | None = None) -> ImageFiles:
    path = tmp_path / "image.tar"
    if names is None:
        names = [f"{i}.tar" for i in range(len(layers))]
    if diff_ids is None:
        diff_ids = ["sha256:" + digest(gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data) for data in layers]
    config = canonical({"rootfs": {"type": "layers", "diff_ids": diff_ids}})
    config_name = digest(config) + ".json"
    with tarfile.open(path, "w") as archive:
        values = {"manifest.json": canonical([{"Layers": names, "Config": config_name}]),
                  config_name: config, **dict(zip(names, layers, strict=True))}
        for name, data in values.items():
            if name == omit:
                continue
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return ImageFiles.from_docker_archive(path, "sha256:" + digest(config))


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
    [("escape", ("../../outside", tarfile.SYMTYPE))],
    [("missing", ("absent", tarfile.LNKTYPE))],
    [("a", ("b", tarfile.SYMTYPE)), ("b", ("a", tarfile.SYMTYPE)), ("a/file", b"cycle")],
])
def test_unsafe_or_ambiguous_layers_fail_closed(tmp_path, entries):
    with pytest.raises((EvidenceError, ValueError)):
        image(tmp_path, [layer(entries)])
    assert not (tmp_path.parent / "outside").exists()


@pytest.mark.parametrize("compressed", [False, True])
def test_corrupt_or_substituted_layer_digest_fails(tmp_path, compressed):
    data = layer([("file", b"changed")])
    if compressed:
        data = gzip.compress(data)
    with pytest.raises(EvidenceError, match="image-layer-digest-drift"):
        image(tmp_path, [data], ["sha256:" + "0" * 64])


def test_wrong_external_subject_digest_fails(tmp_path):
    files = image(tmp_path, [layer([("file", b"content")])])
    files.close()
    with pytest.raises(EvidenceError, match="image-subject-digest-drift"):
        ImageFiles.from_docker_archive(tmp_path / "image.tar", "sha256:" + "0" * 64)


def test_content_addressed_blob_digest_is_verified(tmp_path):
    with pytest.raises(EvidenceError, match="image-layer-blob-drift"):
        image(tmp_path, [layer([("file", b"content")])], names=["blobs/sha256/" + "0" * 64])


def test_missing_manifest_layer_is_rejected(tmp_path):
    with pytest.raises(KeyError):
        image(tmp_path, [layer([("file", b"content")])], omit="0.tar")
