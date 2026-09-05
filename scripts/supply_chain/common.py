from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


class EvidenceError(Exception):
    """Only fixed, value-free error codes may cross the CLI boundary."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise EvidenceError(code)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode()


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate-json-key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise EvidenceError("invalid-json-constant")


def load(path: Path) -> dict:
    value = json.loads(path.read_bytes(), object_pairs_hook=unique_object, parse_constant=reject_constant)
    require(isinstance(value, dict), "invalid-json-object")
    return value


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value))


def run(args: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, check=False)
    require(result.returncode == 0, "external-command-failed-" + Path(args[0]).stem + "-" + str(result.returncode))
    return result.stdout.decode("utf-8")


def sha_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
