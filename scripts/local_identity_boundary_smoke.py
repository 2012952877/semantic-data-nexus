from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

_MAX_RESPONSE_BYTES = 64 * 1024


class IdentityBoundaryFailure(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise IdentityBoundaryFailure(message)


def _request(
    base_url: str,
    *,
    host: str,
    origin: str | None = None,
) -> tuple[int, bytes]:
    headers = {
        "Accept": "application/json",
        "Host": host,
        "User-Agent": "semantic-data-nexus-identity-boundary-smoke/1",
    }
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/me",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, error.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.URLError as error:
        raise IdentityBoundaryFailure(
            "could not reach the local Compose identity boundary"
        ) from error


def _assert_bounded_response(body: bytes) -> None:
    _require(
        len(body) <= _MAX_RESPONSE_BYTES,
        "identity boundary response exceeded the smoke-test limit",
    )


def _assert_no_principal(body: bytes, description: str) -> None:
    _assert_bounded_response(body)
    _require(
        b'"subject"' not in body and b"local-compose-user" not in body,
        f"{description} returned the fixed development principal",
    )


def run(base_url: str) -> dict[str, Any]:
    local_status, local_body = _request(
        base_url,
        host="127.0.0.1",
        origin=base_url,
    )
    _assert_bounded_response(local_body)
    _require(local_status == 200, "local Host and Origin were not accepted")
    try:
        principal = json.loads(local_body)
    except json.JSONDecodeError as error:
        raise IdentityBoundaryFailure(
            "local identity response was not valid JSON"
        ) from error
    _require(
        isinstance(principal, dict)
        and principal.get("subject") == "local-compose-user",
        "local request did not receive the fixed development principal",
    )

    host_status, host_body = _request(
        base_url,
        host="rebind.attacker.invalid",
    )
    _require(host_status == 421, "non-local Host was not rejected by Nginx")
    _assert_no_principal(host_body, "non-local Host")

    origin_status, origin_body = _request(
        base_url,
        host="127.0.0.1",
        origin="https://attacker.invalid",
    )
    _require(origin_status == 403, "foreign Origin was not rejected by Nginx")
    _assert_no_principal(origin_body, "foreign Origin")

    return {
        "localStatus": local_status,
        "nonLocalHostStatus": host_status,
        "foreignOriginStatus": origin_status,
        "principal": principal["subject"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the local Compose development identity boundary."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    try:
        evidence = run(args.base_url)
    except IdentityBoundaryFailure as error:
        print(f"IDENTITY BOUNDARY SMOKE FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
