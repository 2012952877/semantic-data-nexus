from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = ROOT / "data" / "synthetic" / "generate.py"


def load_generator():
    spec = importlib.util.spec_from_file_location("synthetic_generator", GENERATOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generation_is_byte_deterministic(tmp_path: Path) -> None:
    generator = load_generator()
    first = tmp_path / "first"
    second = tmp_path / "second"
    generator.generate(first)
    generator.generate(second)
    assert generator.corpus_digest(first) == generator.corpus_digest(second)


def test_checked_in_data_matches_generator(tmp_path: Path) -> None:
    generator = load_generator()
    regenerated = tmp_path / "generated"
    generator.generate(regenerated)
    assert generator.corpus_digest(regenerated) == generator.corpus_digest(
        ROOT / "data" / "synthetic" / "generated"
    )


def test_digest_is_stable_across_checkout_line_endings(tmp_path: Path) -> None:
    generator = load_generator()
    lf = tmp_path / "lf"
    crlf = tmp_path / "crlf"
    lf.mkdir()
    crlf.mkdir()
    (lf / "sample.csv").write_bytes(b"id,value\n1,synthetic\n")
    (crlf / "sample.csv").write_bytes(b"id,value\r\n1,synthetic\r\n")
    assert generator.corpus_digest(lf) == generator.corpus_digest(crlf)
