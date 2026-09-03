from __future__ import annotations

import hashlib
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


def digest(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*.csv"))
    }


def test_generation_is_byte_deterministic(tmp_path: Path) -> None:
    generator = load_generator()
    first = tmp_path / "first"
    second = tmp_path / "second"
    generator.generate(first)
    generator.generate(second)
    assert digest(first) == digest(second)


def test_checked_in_data_matches_generator(tmp_path: Path) -> None:
    generator = load_generator()
    regenerated = tmp_path / "generated"
    generator.generate(regenerated)
    assert digest(regenerated) == digest(ROOT / "data" / "synthetic" / "generated")
