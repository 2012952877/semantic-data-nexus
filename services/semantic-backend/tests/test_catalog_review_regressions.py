from __future__ import annotations

import asyncio
import copy
from decimal import Decimal

import pyarrow as pa
import pytest
from query_runtime.operators import ResourceLimits
from semantic_api.catalog_v1.catalog import fingerprint, pin_for
from semantic_api.catalog_v1.compiler import CompilerLimits
from semantic_api.catalog_v1.models import CatalogDocument, CompilerFailure
from test_catalog_compilation import CASES, setup, wire

from semantic_backend.catalog_compilation import execute_catalog


def repin(case):
    case["catalog"]["bindings_sha256"] = fingerprint(
        {
            "contract_version": "catalog-binding-content/v1",
            "entities": case["bindings"],
        }
    )
    document = CatalogDocument.model_validate(case["catalog"])
    case["candidate"]["graph"]["catalog"] = pin_for(document).model_dump(mode="json")


def scalar_total(case, name="energy"):
    graph = case["candidate"]["graph"]
    next(n for n in graph["nodes"] if n["id"] == "totals")["operation"]["group_by"] = []
    next(n for n in graph["nodes"] if n["id"] == "result")["operation"]["columns"] = [
        {"source": "total", "alias": name},
    ]
    graph["result_schema"] = [{"name": name, "data_type": "number"}]


@pytest.mark.parametrize("grants", [frozenset(), frozenset({"status.active"})])
async def test_retired_member_cannot_execute_through_raw_comparison(grants):
    case = copy.deepcopy(CASES[0])
    case["question"] = "Total energy by site name in June 2026"
    case["candidate"]["graph"]["nodes"][3]["operation"]["predicate"] = {
        "kind": "comparison",
        "column": "grid.site.status",
        "operator": "eq",
        "value": "R",
    }
    async with wire(case["candidate"]) as (provider, _):
        compiler, request, context, _, resolver, authority = setup(case, provider)
        compiler.limits = CompilerLimits(max_total_input_tokens=131_072)
        authority.access = authority.access.model_copy(update={"member_ids": grants})
        result = await compiler.compile(
            request, context=context, deadline=asyncio.get_running_loop().time() + 15
        )
        assert result.status == "blocked" and result.graph is None
        assert result.diagnostics == ("GOVERNED_PREDICATE_REQUIRED", "REPAIR_FAILED")
        assert not resolver.executions


@pytest.mark.parametrize("cardinality", ["many_to_one", "one_to_one"])
async def test_profile_metric_after_join_chain_is_not_multiplied(cardinality):
    case = copy.deepcopy(CASES[0])
    data = case["catalog"]
    data["relations"][0]["cardinality"] = cardinality
    data["entities"].append({"id": "grid.profile", "label": "Profiles"})
    data["fields"].extend(
        [
            {
                "id": "grid.profile.site",
                "label": "Profile identifier",
                "entity_id": "grid.profile",
                "data_type": "integer",
            },
            {
                "id": "grid.profile.capacity",
                "label": "Profile value",
                "entity_id": "grid.profile",
                "data_type": "number",
            },
        ]
    )
    data["metrics"].append(
        {
            "id": "metric.capacity",
            "label": "Profile capacity",
            "entity_id": "grid.profile",
            "field_id": "grid.profile.capacity",
            "function": "sum",
        }
    )
    data["relations"].append(
        {
            "id": "site.profile",
            "label": "Site profile",
            "from_entity": "grid.site",
            "to_entity": "grid.profile",
            "from_field": "grid.site.id",
            "to_field": "grid.profile.site",
            "cardinality": "one_to_one",
        }
    )
    case["bindings"].append(
        {
            "entity_id": "grid.profile",
            "alias": "profiles",
            "source_type": "synthetic",
            "object_name": "synthetic_profiles",
            "columns": {"grid.profile.site": "site_key", "grid.profile.capacity": "capacity"},
            "utc_naive_fields": [],
        }
    )
    case["rows"]["profiles"] = [
        {"site_key": 1, "capacity": 10.0},
        {"site_key": 2, "capacity": 20.0},
    ]
    if cardinality == "one_to_one":
        case["rows"]["measurements"] = [
            {"site_key": 1, "quantity": 9.0, "at": "2026-06-01T00:00:00Z"},
            {"site_key": 2, "quantity": 7.0, "at": "2026-06-01T00:00:00Z"},
        ]
    graph = case["candidate"]["graph"]
    graph["nodes"][5]["dependencies"] = ["with_profile"]
    graph["nodes"][5]["operation"]["measures"][0]["metric_id"] = "metric.capacity"
    graph["nodes"][5:5] = [
        {
            "id": "profiles",
            "dependencies": [],
            "operation": {
                "kind": "SELECT",
                "entity_id": "grid.profile",
                "columns": ["grid.profile.site", "grid.profile.capacity"],
            },
        },
        {
            "id": "with_profile",
            "dependencies": ["locations", "profiles"],
            "operation": {
                "kind": "JOIN",
                "relation_id": "site.profile",
                "join_type": "inner",
            },
        },
    ]
    case["question"] = "Profile capacity for active in June 2026"
    scalar_total(case, "capacity")
    repin(case)
    async with wire(case["candidate"]) as (provider, _):
        compiler, request, context, bindings, resolver, _ = setup(case, provider)
        compiler.limits = CompilerLimits(max_total_input_tokens=131_072)
        deadline = asyncio.get_running_loop().time() + 15
        compilation = await compiler.compile(request, context=context, deadline=deadline)
        if cardinality == "many_to_one":
            assert compilation.status == "blocked" and compilation.graph is None
            assert compilation.diagnostics == ("METRIC_JOIN_FANOUT", "REPAIR_FAILED")
            assert not resolver.executions
        else:
            assert compilation.status == "compiled", compilation.diagnostics
            result = await execute_catalog(
                request,
                compilation,
                compiler=compiler,
                context=context,
                bindings=bindings,
                resolver=resolver,
                run_id="synthetic-profile",
                deadline=deadline,
                limits=ResourceLimits(max_rows=100, max_bytes=1_000_000),
            )
            assert result.table.to_pylist() == [{"capacity": 30.0}]


def key_case(kind, keys):
    case = copy.deepcopy(CASES[0])
    for field in case["catalog"]["fields"]:
        if field["id"] in ("grid.reading.site", "grid.site.id"):
            field["data_type"] = kind
    case["question"] = "Total energy for active in June 2026"
    if kind == "datetime":
        case["question"] += " using observed time"
    case["rows"]["measurements"] = [
        {"site_key": keys[0], "quantity": 9.0, "at": "2026-06-01T00:00:00Z"},
    ]
    case["rows"]["locations"] = [
        {"site_key": key, "name": f"Site{index}", "state": "A"} for index, key in enumerate(keys)
    ]
    scalar_total(case)
    repin(case)
    return case


@pytest.mark.parametrize(
    ("kind", "keys", "error"),
    [
        ("number", [0.0, -0.0], "JOIN_CARDINALITY_VIOLATION"),
        ("number", [float("nan")], "JOIN_KEY_NONFINITE"),
        ("number", [float("inf")], "JOIN_KEY_NONFINITE"),
        ("number", [Decimal("0.0"), Decimal("1.0")], "SOURCE_TYPE_MISMATCH"),
        (
            "datetime",
            ["2026-06-01T00:00:00Z", "2026-06-01T08:00:00+08:00"],
            "JOIN_CARDINALITY_VIOLATION",
        ),
        ("number", [0.0, 1.0], None),
        ("datetime", ["2026-06-01T00:00:00Z", "2026-06-01T00:00:01Z"], None),
    ],
)
async def test_join_key_equivalence_uses_execution_semantics(kind, keys, error):
    case = key_case(kind, keys)
    async with wire(case["candidate"]) as (provider, _):
        compiler, request, context, bindings, resolver, _ = setup(case, provider)
        deadline = asyncio.get_running_loop().time() + 15
        compilation = await compiler.compile(request, context=context, deadline=deadline)
        assert compilation.status == "compiled", compilation.diagnostics
        kwargs = dict(
            compiler=compiler,
            context=context,
            bindings=bindings,
            resolver=resolver,
            run_id="synthetic-key-equality",
            deadline=deadline,
            limits=ResourceLimits(max_rows=100, max_bytes=1_000_000),
        )
        if error:
            with pytest.raises(CompilerFailure, match=error):
                await execute_catalog(request, compilation, **kwargs)
        else:
            result = await execute_catalog(request, compilation, **kwargs)
            assert result.table.to_pylist() == [{"energy": 9.0}]


async def test_join_timestamp_keys_cannot_be_lossily_truncated():
    case = key_case("datetime", ["2026-06-01T00:00:00Z", "2026-06-01T00:00:01Z"])
    async with wire(case["candidate"]) as (provider, _):
        compiler, request, context, bindings, resolver, _ = setup(case, provider)
        table = resolver._tables["locations"]
        precise = pa.array(
            [1780272000000000001, 1780272000000000002], type=pa.timestamp("ns", tz="UTC")
        )
        resolver._tables["locations"] = table.set_column(
            table.column_names.index("site_key"), "site_key", precise
        )
        original_execute = resolver.execute

        async def preserve_source_precision(execution_context, fragment, cancel_event):
            if fragment.source.alias == "locations":
                assert len(fragment.operations) == 1
                return resolver._tables["locations"].select(fragment.operations[0].columns)
            return await original_execute(execution_context, fragment, cancel_event)

        resolver.execute = preserve_source_precision
        deadline = asyncio.get_running_loop().time() + 15
        compilation = await compiler.compile(request, context=context, deadline=deadline)
        assert compilation.status == "compiled"
        with pytest.raises(CompilerFailure, match="SOURCE_TIME_PRECISION"):
            await execute_catalog(
                request,
                compilation,
                compiler=compiler,
                context=context,
                bindings=bindings,
                resolver=resolver,
                run_id="synthetic-key-precision",
                deadline=deadline,
                limits=ResourceLimits(max_rows=100, max_bytes=1_000_000),
            )


async def test_empty_numeric_dimension_is_a_valid_unique_key_set():
    case = key_case("number", [0.0, 1.0])
    async with wire(case["candidate"]) as (provider, _):
        compiler, request, context, bindings, resolver, _ = setup(case, provider)
        resolver._tables["locations"] = resolver._tables["locations"].slice(0, 0)
        deadline = asyncio.get_running_loop().time() + 15
        compilation = await compiler.compile(request, context=context, deadline=deadline)
        result = await execute_catalog(
            request,
            compilation,
            compiler=compiler,
            context=context,
            bindings=bindings,
            resolver=resolver,
            run_id="synthetic-empty-key",
            deadline=deadline,
            limits=ResourceLimits(max_rows=100, max_bytes=1_000_000),
        )
        assert result.table.to_pylist() == [{"energy": None}]
