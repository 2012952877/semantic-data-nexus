from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_RUN_ID = re.compile(r"^run_[0-9a-f]{32}$")
_DECIMAL_TEXT = re.compile(r"^-?(0|[1-9][0-9]*)(?:\.([0-9]+))?$")
_DATE_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_TEXT = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_TERMINAL_STATES = {"Cancelled", "Succeeded", "Failed"}
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_NUMBER = Decimal("1e28")


class SmokeFailure(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _read_response(response: Any) -> bytes:
    body = response.read(_MAX_RESPONSE_BYTES + 1)
    _require(len(body) <= _MAX_RESPONSE_BYTES, "response exceeded the smoke-test limit")
    return body


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 15,
) -> tuple[int, bytes]:
    data = (
        None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    )
    headers = {
        "Accept": "application/json",
        "User-Agent": "semantic-data-nexus-full-stack-smoke/1",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        _url(base_url, path),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _read_response(response)
    except urllib.error.HTTPError as error:
        detail = _read_response(error)[:4_096].decode("utf-8", errors="replace")
        raise SmokeFailure(
            f"{method} {path} returned HTTP {error.code}: {detail}"
        ) from error
    except urllib.error.URLError as error:
        raise SmokeFailure(
            f"{method} {path} could not reach the local stack"
        ) from error


def _request_json(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    status, body = _request(base_url, method, path, payload=payload)
    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise SmokeFailure(f"{method} {path} returned invalid JSON") from error
    _require(isinstance(value, dict), f"{method} {path} did not return a JSON object")
    return status, value


def _validate_status(summary: dict[str, Any], *, question: str) -> None:
    _require(
        isinstance(summary.get("id"), str)
        and _RUN_ID.fullmatch(summary["id"]) is not None,
        "BFF status did not contain a canonical run ID",
    )
    _require(
        summary.get("question") == question, "BFF status changed the governed question"
    )
    _require(
        summary.get("state")
        in {
            "StartPending",
            "DispatchUnknown",
            "Queued",
            "Starting",
            "Running",
            "CancelRequested",
            "Cancelled",
            "Succeeded",
            "Failed",
        },
        "BFF status returned an unknown state",
    )
    _require(isinstance(summary.get("version"), int), "BFF status version is not typed")
    _require(isinstance(summary.get("stages"), list), "BFF status stages are not typed")
    usage = summary.get("tokenUsage")
    _require(isinstance(usage, dict), "BFF status token usage is not typed")
    _require(
        all(
            isinstance(usage.get(key), int)
            for key in ("inputTokens", "outputTokens", "totalTokens")
        ),
        "BFF status token counts are not integers",
    )


def _create_run(
    base_url: str,
    *,
    question: str,
    compilation_mode: str,
) -> dict[str, Any]:
    status, summary = _request_json(
        base_url,
        "POST",
        "/api/v1/runs",
        payload={
            "clientRequestId": f"smoke-{uuid.uuid4().hex[:16]}",
            "workload": "synthetic-profit",
            "question": question,
            "evaluationClock": "2024-04-15T09:00:00Z",
            "evaluationTimezone": "Etc/UTC",
            "compilationMode": compilation_mode,
            "executionMode": "thread",
            "outputMode": "normal",
        },
    )
    _require(status in {200, 202}, "BFF did not accept the run")
    _validate_status(summary, question=question)
    _require(
        summary.get("compilationMode") == compilation_mode,
        "BFF status changed the compilation mode",
    )
    return summary


def _wait_for_status(
    base_url: str,
    run_id: str,
    *,
    question: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float,
    description: str,
) -> tuple[dict[str, Any], list[str]]:
    deadline = time.monotonic() + timeout
    observed: list[str] = []
    while time.monotonic() < deadline:
        status, summary = _request_json(
            base_url,
            "GET",
            f"/api/v1/runs/{run_id}/semantic-status",
        )
        _require(status == 200, f"status refresh for {run_id} did not succeed")
        _validate_status(summary, question=question)
        state = summary["state"]
        if not observed or observed[-1] != state:
            observed.append(state)
        if predicate(summary):
            return summary, observed
        if state in _TERMINAL_STATES:
            raise SmokeFailure(
                f"{run_id} became {state} before {description}; observed {observed}"
            )
        time.sleep(0.05)
    raise SmokeFailure(f"{run_id} did not reach {description}; observed {observed}")


def _terminal_status(
    base_url: str,
    summary: dict[str, Any],
    *,
    question: str,
    timeout: float,
) -> tuple[dict[str, Any], list[str]]:
    return _wait_for_status(
        base_url,
        summary["id"],
        question=question,
        predicate=lambda value: value["state"] in _TERMINAL_STATES,
        timeout=timeout,
        description="a terminal state",
    )


def _detail(base_url: str, run_id: str, *, question: str) -> dict[str, Any]:
    status, detail = _request_json(base_url, "GET", f"/api/v1/runs/{run_id}/detail")
    _require(status == 200, "BFF detail request did not succeed")
    _require(detail.get("runId") == run_id, "detail run ID did not match the status")
    _require(
        detail.get("question") == question, "detail question did not match the request"
    )
    _require(isinstance(detail.get("sqg"), dict), "detail SQG is not typed")
    _require(isinstance(detail.get("physicalNodes"), list), "detail plan is not typed")
    lineage = detail.get("lineage")
    _require(isinstance(lineage, dict), "detail lineage is not typed")
    _require(lineage.get("runId") == run_id, "lineage run ID did not match")
    _require(isinstance(lineage.get("nodes"), list), "lineage nodes are not typed")
    _require(isinstance(lineage.get("edges"), list), "lineage edges are not typed")
    _require(
        isinstance(detail.get("diagnostics"), list), "detail diagnostics are not typed"
    )
    return detail


def _decimal_value(value: Any) -> Decimal | None:
    if not isinstance(value, str):
        return None
    match = _DECIMAL_TEXT.fullmatch(value)
    if match is None:
        return None
    integer, fraction = match.groups()
    digits = f"{integer}{fraction or ''}".lstrip("0")
    if (len(digits) if digits else 1) > 29 or len(fraction or "") > 28:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    if (parsed == 0 and value.startswith("-")) or parsed.copy_abs() > _MAX_NUMBER:
        return None
    return parsed


def _float_value(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return Decimal(value) if abs(value) <= _MAX_SAFE_INTEGER else None
    if not math.isfinite(value):
        return None
    parsed = Decimal(str(value))
    if parsed.copy_abs() > _MAX_NUMBER:
        return None
    if value.is_integer() and abs(value) > _MAX_SAFE_INTEGER:
        return None
    return parsed


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or _TIMESTAMP_TEXT.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_cell(value: Any, column: dict[str, Any]) -> bool:
    if value is None:
        return column.get("nullable") is True
    data_type = column.get("dataType")
    if data_type == "string":
        return isinstance(value, str) and len(value) <= 4_000
    if data_type == "integer":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and abs(value) <= _MAX_SAFE_INTEGER
        )
    if data_type == "float":
        return _float_value(value) is not None
    if data_type == "decimal":
        return _decimal_value(value) is not None
    if data_type == "boolean":
        return isinstance(value, bool)
    if data_type == "date":
        if not isinstance(value, str) or _DATE_TEXT.fullmatch(value) is None:
            return False
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if data_type == "timestamp":
        return _valid_timestamp(value)
    return False


def _rows_by_key(
    result: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    columns = result.get("columns")
    rows = result.get("rows")
    _require(isinstance(columns, list) and columns, "result columns are missing")
    _require(isinstance(rows, list), "result rows are missing")
    _require(
        all(isinstance(column, dict) for column in columns),
        "result columns are invalid",
    )
    typed_columns = [column for column in columns if isinstance(column, dict)]
    keys = [column.get("key") for column in typed_columns]
    _require(
        all(isinstance(key, str) and key for key in keys)
        and len(keys) == len(set(keys)),
        "result column keys are invalid",
    )
    columns_by_key = dict(zip(keys, typed_columns, strict=True))
    mapped: list[dict[str, Any]] = []
    for row in rows:
        _require(
            isinstance(row, list) and len(row) == len(keys),
            "result row shape is invalid",
        )
        _require(
            all(
                _valid_cell(value, column)
                for value, column in zip(row, typed_columns, strict=True)
            ),
            "result cell did not match its declared column type",
        )
        mapped.append(dict(zip(keys, row, strict=True)))
    return mapped, columns_by_key


def _validate_committed_detail(
    detail: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    result = detail.get("result")
    manifest = detail.get("manifest")
    _require(isinstance(result, dict), "succeeded detail did not contain a result")
    _require(isinstance(manifest, dict), "succeeded detail did not contain a manifest")
    rows, columns = _rows_by_key(result)
    _require(
        result.get("rowCount") == len(rows),
        "inline row count did not match committed rows",
    )
    _require(
        result.get("truncated") is False, "deterministic smoke result was truncated"
    )
    _require(manifest.get("runId") == detail["runId"], "manifest run ID did not match")
    _require(manifest.get("rowCount") == len(rows), "manifest row count did not match")
    _require(
        manifest.get("storage") == "inline", "smoke result was not committed inline"
    )
    _require(
        isinstance(manifest.get("checksum"), str)
        and manifest["checksum"].startswith("sha256:"),
        "manifest checksum was not typed",
    )
    lineage = detail["lineage"]
    relations = {
        edge.get("relation") for edge in lineage["edges"] if isinstance(edge, dict)
    }
    _require(
        {"reads_from", "depends_on", "produces"}.issubset(relations),
        "lineage did not contain the expected relationships",
    )
    return rows, columns


def _run_simple(base_url: str, timeout: float) -> str:
    question = "上季度各区域利润是多少?"
    started = _create_run(
        base_url,
        question=question,
        compilation_mode="regional_quarterly_profit",
    )
    terminal, _ = _terminal_status(
        base_url,
        started,
        question=question,
        timeout=timeout,
    )
    _require(terminal["state"] == "Succeeded", "simple run did not succeed")
    _require(
        [stage.get("name") for stage in terminal["stages"]]
        == ["Initialize", "Compile", "Optimize", "Execute", "Generate"],
        "simple run did not expose the five typed stages",
    )
    _require(
        all(stage.get("state") == "Succeeded" for stage in terminal["stages"]),
        "simple run did not complete every stage",
    )
    detail = _detail(base_url, started["id"], question=question)
    rows, columns = _validate_committed_detail(detail)
    profit_type = columns.get("profit", {}).get("dataType")
    _require(
        profit_type in {"decimal", "float"},
        "simple profit column was not a governed numeric type",
    )
    profits = {
        row.get("region"): (
            _decimal_value(row.get("profit"))
            if profit_type == "decimal"
            else _float_value(row.get("profit"))
        )
        for row in rows
    }
    _require(
        profits
        == {
            "北辰区": Decimal("2334.00"),
            "西岭区": Decimal("2095.50"),
            "南港区": Decimal("1863.00"),
            "东湖区": Decimal("1059.00"),
        },
        "simple regional-period profit rows changed",
    )
    return started["id"]


def _run_complex(base_url: str, timeout: float) -> str:
    question = "对比各区域销售利润, 按月份"
    started = _create_run(
        base_url,
        question=question,
        compilation_mode="monthly_regional_comparison",
    )
    terminal, _ = _terminal_status(
        base_url,
        started,
        question=question,
        timeout=timeout,
    )
    _require(terminal["state"] == "Succeeded", "complex run did not succeed")
    detail = _detail(base_url, started["id"], question=question)
    rows, columns = _validate_committed_detail(detail)
    kinds = [
        node.get("kind") for node in detail["physicalNodes"] if isinstance(node, dict)
    ]
    expected = ["AGGREGATE", "PIVOT", "DERIVE", "PROJECT"]
    cursor = 0
    for kind in kinds:
        if cursor < len(expected) and kind == expected[cursor]:
            cursor += 1
    _require(
        cursor == len(expected),
        "complex plan did not contain AGGREGATE -> PIVOT -> DERIVE -> PROJECT",
    )
    _require(len(rows) == 4, "complex run did not commit four regional rows")
    _require(
        all(
            columns.get(key, {}).get("dataType") == "float"
            for key in ("profit_current", "profit_previous", "profit_change")
        ),
        "complex profit columns did not declare float values",
    )
    for row in rows:
        current = _float_value(row.get("profit_current"))
        previous = _float_value(row.get("profit_previous"))
        change = _float_value(row.get("profit_change"))
        _require(
            current is not None
            and previous is not None
            and change is not None
            and current - previous == change,
            "complex derived profit value did not match its typed inputs",
        )
    return started["id"]


def _run_cancellation(base_url: str, timeout: float) -> tuple[str, list[str]]:
    question = "上季度各区域利润是多少?"
    started = _create_run(
        base_url,
        question=question,
        compilation_mode="regional_quarterly_profit",
    )

    def executing(summary: dict[str, Any]) -> bool:
        return summary["state"] == "Running" and any(
            isinstance(stage, dict)
            and stage.get("name") == "Execute"
            and stage.get("state") == "Running"
            for stage in summary["stages"]
        )

    active, observed = _wait_for_status(
        base_url,
        started["id"],
        question=question,
        predicate=executing,
        timeout=timeout,
        description="observable Execute activity",
    )
    status, cancellation = _request_json(
        base_url,
        "POST",
        f"/api/v1/runs/{started['id']}/cancel",
        payload={},
    )
    _require(status == 202, "BFF did not accept cancellation")
    _validate_status(cancellation, question=question)
    _require(
        cancellation.get("cancellationGeneration", 0) >= 1,
        "cancellation was not versioned",
    )
    _require(
        cancellation.get("cancellationDelivery") == "Delivered",
        "cancellation was not delivered",
    )
    final, terminal_observed = _terminal_status(
        base_url,
        cancellation,
        question=question,
        timeout=timeout,
    )
    observed.extend(
        state for state in terminal_observed if not observed or observed[-1] != state
    )
    _require(final["state"] == "Cancelled", "cancelled run did not become terminal")
    _require(
        active["version"] < final["version"],
        "cancellation was not observable as a status change",
    )
    detail = _detail(base_url, started["id"], question=question)
    _require(detail.get("result") is None, "cancelled run published result rows")
    _require(detail.get("manifest") is None, "cancelled run published a manifest")
    return started["id"], observed


def run(base_url: str, timeout: float) -> dict[str, Any]:
    status, index = _request(base_url, "GET", "/")
    _require(
        status == 200 and b'<div id="app">' in index,
        "Web image did not serve the workbench",
    )
    status, principal = _request_json(base_url, "GET", "/api/v1/me")
    _require(status == 200, "same-origin BFF route did not authenticate")
    _require(
        principal.get("subject") == "local-compose-user",
        "unexpected local Compose identity",
    )
    _require(
        {"reader", "contributor", "admin"}.issubset(set(principal.get("roles", []))),
        "local Compose identity did not receive bounded development roles",
    )

    simple = _run_simple(base_url, timeout)
    complex_run = _run_complex(base_url, timeout)
    cancelled, observed = _run_cancellation(base_url, timeout)

    status, listing = _request_json(base_url, "GET", "/api/v1/runs?limit=10")
    _require(
        status == 200 and isinstance(listing.get("items"), list),
        "typed run list failed",
    )
    listed_ids = {item.get("id") for item in listing["items"] if isinstance(item, dict)}
    _require(
        {simple, complex_run, cancelled}.issubset(listed_ids),
        "run list omitted smoke runs",
    )
    return {
        "baseUrl": base_url,
        "clientMode": "http",
        "resolver": "fake",
        "principal": principal["subject"],
        "simpleRunId": simple,
        "complexRunId": complex_run,
        "cancelledRunId": cancelled,
        "cancellationStates": observed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exercise the real local Compose stack through Web HTTP."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    try:
        evidence = run(args.base_url, args.timeout)
    except SmokeFailure as error:
        print(f"FULL-STACK SMOKE FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
