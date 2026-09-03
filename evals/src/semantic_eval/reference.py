"""Reference adapter for static candidate artifact bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .evaluator import load_document


def load_static_candidate(path: str | Path) -> dict[str, Any]:
    """Load a provider-independent compiler/result/lineage fixture."""
    candidate = load_document(path)
    version = candidate.get("artifact_version")
    if version != "candidate-v0":
        raise ValueError(f"unsupported candidate artifact version: {version!r}")
    if not isinstance(candidate.get("cases"), (dict, list)):
        raise ValueError("candidate artifact must contain cases")
    return candidate
