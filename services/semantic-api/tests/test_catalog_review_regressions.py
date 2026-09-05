from __future__ import annotations

import asyncio
import copy

import pytest
from pydantic import ValidationError
from test_catalog_v1 import (
    CASES,
    Authority,
    InjectedProvider,
    MemoryClarifications,
    deadline,
    setup,
)

from semantic_api.catalog_v1.catalog import authorized_view, pin_for
from semantic_api.catalog_v1.models import CatalogDocument, CompilerFailure


@pytest.mark.parametrize(
    "members",
    [frozenset(), frozenset({"status.active"}), frozenset({"status.active", "status.retired"})],
)
async def test_governed_field_never_becomes_raw_scalar_after_member_filtering(members):
    candidate = copy.deepcopy(CASES[0]["candidate"])
    candidate["graph"]["nodes"][3]["operation"]["predicate"] = {
        "kind": "comparison",
        "column": "grid.site.status",
        "operator": "eq",
        "value": "R",
    }
    compiler, request, context, authority = setup(
        0, provider=InjectedProvider(candidate, candidate)
    )
    authority.access = authority.access.model_copy(update={"member_ids": members})
    request = request.model_copy(update={"question": "Total energy by site name in June 2026"})
    result = await compiler.compile(request, context=context, deadline=deadline())
    assert result.status == "blocked" and result.graph is None
    assert result.diagnostics == ("GOVERNED_PREDICATE_REQUIRED", "REPAIR_FAILED")
    view = compiler.provider.calls[0][0].semantic_catalog
    field = next(f for f in view.fields if f.id == "grid.site.status")
    assert field.member_governed is True
    assert {m.id for m in field.members} == members


@pytest.mark.parametrize(
    "members",
    [frozenset(), frozenset({"status.active"}), frozenset({"status.active", "status.retired"})],
)
async def test_member_id_authorization_with_empty_and_nonempty_grants(members):
    provider = InjectedProvider(CASES[0]["candidate"], CASES[0]["candidate"])
    compiler, request, context, authority = setup(0, provider=provider)
    authority.access = authority.access.model_copy(update={"member_ids": members})
    result = await compiler.compile(request, context=context, deadline=deadline())
    if not members:
        assert result.status == "blocked"
        assert result.diagnostics == ("MEMBER_NOT_AVAILABLE", "REPAIR_FAILED")
    else:
        assert result.status == "compiled"
        assert not result.repair_attempted


async def test_ungoverned_numeric_comparison_still_compiles():
    compiler, request, context, _ = setup()
    result = await compiler.compile(request, context=context, deadline=deadline())
    assert result.status == "compiled" and not result.repair_attempted
    field = next(
        f
        for f in compiler.provider.calls[0][0].semantic_catalog.fields
        if f.id == "lab.assay.score"
    )
    assert not field.member_governed and not field.members


def test_governance_is_pinned_and_cannot_be_inconsistent_with_members():
    data = copy.deepcopy(CASES[1]["catalog"])
    data["fields"][0]["member_governed"] = False
    with pytest.raises(ValidationError):
        CatalogDocument.model_validate(data)
    data["fields"][0]["members"] = []
    ungoverned = CatalogDocument.model_validate(data)
    data["fields"][0]["member_governed"] = True
    governed = CatalogDocument.model_validate(data)
    assert pin_for(governed) != pin_for(ungoverned)
    authority = Authority(governed)
    view = authorized_view(governed, authority.access)
    assert view.fields[0].member_governed and not view.fields[0].members


@pytest.mark.parametrize(
    "order",
    [
        ["batch.alpha", "batch.beta"],
        ["batch.beta", "batch.alpha"],
    ],
)
@pytest.mark.parametrize("policy", [False, True])
async def test_member_predicate_order_is_semantically_irrelevant(order, policy):
    data = copy.deepcopy(CASES[1]["catalog"])
    if policy:
        data["entities"][0]["required_filters"] = [
            {
                "kind": "members",
                "column": "lab.assay.batch",
                "member_ids": ["batch.alpha", "batch.beta"],
            }
        ]
    document = CatalogDocument.model_validate(data)
    candidate = copy.deepcopy(CASES[1]["candidate"])
    candidate["graph"]["catalog"] = pin_for(document).model_dump(mode="json")
    candidate["graph"]["nodes"][1]["operation"]["predicate"]["member_ids"] = order
    compiler, request, context, _ = setup(document=document, provider=InjectedProvider(candidate))
    request = request.model_copy(
        update={
            "question": "Mean yield above score 1"
            if policy
            else "Mean yield for alpha and beta above score 1",
        }
    )
    result = await compiler.compile(request, context=context, deadline=deadline())
    assert result.status == "compiled" and not result.repair_attempted
    assert result.graph.model_dump(mode="json") == candidate["graph"]


@pytest.mark.parametrize(
    "duplicates",
    [
        ["batch.alpha", "batch.alpha", "batch.beta"],
        ["batch.beta", "batch.alpha", "batch.beta"],
    ],
)
async def test_duplicate_member_ids_are_not_normalized_into_valid_constraints(duplicates):
    candidate = copy.deepcopy(CASES[1]["candidate"])
    candidate["graph"]["nodes"][1]["operation"]["predicate"]["member_ids"] = duplicates
    compiler, request, context, _ = setup(provider=InjectedProvider(candidate, candidate))
    request = request.model_copy(update={"question": "Mean yield for alpha and beta above score 1"})
    result = await compiler.compile(request, context=context, deadline=deadline())
    assert result.status == "blocked"
    assert result.diagnostics == ("MEMBER_NOT_AVAILABLE", "REPAIR_FAILED")


def fanout_case(metric, cardinality):
    data = copy.deepcopy(CASES[0]["catalog"])
    data["relations"][0]["cardinality"] = cardinality
    for entity, label, source in [
        ("grid.profile", "Profile", "grid.site"),
        ("grid.tier", "Tier", "grid.profile"),
    ]:
        data["entities"].append({"id": entity, "label": label})
        data["fields"].extend(
            [
                {
                    "id": entity + ".id",
                    "label": label + " identifier",
                    "entity_id": entity,
                    "data_type": "integer",
                },
                {
                    "id": entity + ".capacity",
                    "label": label + " value",
                    "entity_id": entity,
                    "data_type": "number",
                },
            ]
        )
        data["metrics"].append(
            {
                "id": entity + ".total",
                "label": label + " capacity",
                "entity_id": entity,
                "field_id": entity + ".capacity",
                "function": "sum",
            }
        )
        data["relations"].append(
            {
                "id": source + ".to." + entity,
                "label": label + " association",
                "from_entity": source,
                "to_entity": entity,
                "from_field": source + ".id",
                "to_field": entity + ".id",
                "cardinality": "one_to_one",
            }
        )
    document = CatalogDocument.model_validate(data)
    candidate = copy.deepcopy(CASES[0]["candidate"])
    graph = candidate["graph"]
    graph["catalog"] = pin_for(document).model_dump(mode="json")
    extra = []
    dependency = "locations"
    for relation in data["relations"][1:]:
        entity = relation["to_entity"]
        extra.extend(
            [
                {
                    "id": entity,
                    "dependencies": [],
                    "operation": {
                        "kind": "SELECT",
                        "entity_id": entity,
                        "columns": [entity + ".id", entity + ".capacity"],
                    },
                },
                {
                    "id": "join." + entity,
                    "dependencies": [dependency, entity],
                    "operation": {
                        "kind": "JOIN",
                        "relation_id": relation["id"],
                        "join_type": "inner",
                    },
                },
            ]
        )
        dependency = "join." + entity
    graph["nodes"][5]["dependencies"] = [dependency]
    graph["nodes"][5]["operation"]["measures"][0]["metric_id"] = metric
    graph["nodes"][5:5] = extra
    return document, candidate


@pytest.mark.parametrize(
    ("metric", "cardinality", "allowed"),
    [
        ("grid.profile.total", "many_to_one", False),
        ("grid.tier.total", "many_to_one", False),
        ("grid.profile.total", "one_to_one", True),
        ("grid.tier.total", "one_to_one", True),
        ("metric.energy", "many_to_one", True),
    ],
)
async def test_fanout_propagates_through_one_to_one_chains(metric, cardinality, allowed):
    document, candidate = fanout_case(metric, cardinality)
    compiler, request, context, _ = setup(
        0, document=document, provider=InjectedProvider(candidate, candidate)
    )
    label = next(m.label for m in document.metrics if m.id == metric)
    request = request.model_copy(
        update={
            "question": f"{label} by site name for active in June 2026",
        }
    )
    result = await compiler.compile(request, context=context, deadline=deadline())
    if allowed:
        assert result.status == "compiled", result.diagnostics
        assert not result.repair_attempted
    else:
        assert result.status == "blocked" and result.graph is None
        assert result.diagnostics == ("METRIC_JOIN_FANOUT", "REPAIR_FAILED")


async def test_existing_clarification_id_cannot_enter_direct_generation():
    compiler, request, context, _ = setup()
    ambiguous = request.model_copy(update={"question": "yield for alpha above score 1"})
    first = await compiler.compile(ambiguous, context=context, deadline=deadline())
    assert first.status == "clarification"
    from semantic_api.catalog_v1.models import CompilerFailure

    with pytest.raises(CompilerFailure, match="IDEMPOTENCY_CONFLICT"):
        await compiler.compile(request, context=context, deadline=deadline())
    assert not compiler.provider.calls


async def test_direct_request_same_hash_replays_without_another_provider_call():
    compiler, request, context, _ = setup()
    first = await compiler.compile(request, context=context, deadline=deadline())
    second = await compiler.compile(request, context=context, deadline=deadline())
    assert first == second and first.status == "compiled"
    assert len(compiler.provider.calls) == 1


async def assert_queued_compile_refreshes_authority(
    first, second, request, context, authority, change
):
    first.provider.pause = asyncio.Event()
    second.authorization = authority
    queued_authorized = asyncio.Event()
    original_require = authority.require
    second_task = None

    async def observed_require(current_context, pin, permission):
        access = await original_require(current_context, pin, permission)
        if asyncio.current_task() is second_task:
            queued_authorized.set()
        return access

    authority.require = observed_require
    first_task = asyncio.create_task(first.compile(request, context=context, deadline=deadline()))
    await asyncio.wait_for(first.provider.entered.wait(), 2)
    second_task = asyncio.create_task(second.compile(request, context=context, deadline=deadline()))
    try:
        await asyncio.wait_for(queued_authorized.wait(), 2)
        assert not second_task.done()
        if change == "none":
            first.provider.pause.set()
            first_result, second_result = await asyncio.wait_for(
                asyncio.gather(first_task, second_task), 5
            )
            assert first_result == second_result and first_result.status == "compiled"
        else:
            if change == "membership":
                authority.revoked = True
                code = "MEMBERSHIP_REVOKED"
            else:
                authority.access = authority.access.model_copy(update={"member_ids": frozenset()})
                code = "CLARIFICATION_CONTEXT_CHANGED"
            first_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_task
            with pytest.raises(CompilerFailure, match=code):
                await asyncio.wait_for(second_task, 5)
        assert not second.provider.calls
    finally:
        first.provider.pause.set()
        for task in (first_task, second_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(first_task, second_task, return_exceptions=True)


@pytest.mark.parametrize("change", ["membership", "grants", "none"])
async def test_memory_queued_direct_request_refreshes_authorization(change):
    store = MemoryClarifications()
    first, request, context, authority = setup(store=store)
    second, _, _, _ = setup(store=store)
    await assert_queued_compile_refreshes_authority(
        first, second, request, context, authority, change
    )


def test_configuration_snapshot_copies_frozen_values():
    from dataclasses import FrozenInstanceError

    from semantic_api.catalog_v1.compiler import CompilerLimits

    compiler, _, _, _ = setup()
    snapshot = compiler.configuration()
    assert snapshot.limits is not compiler.limits
    with pytest.raises(FrozenInstanceError):
        snapshot.capabilities = ()
    with pytest.raises(ValidationError):
        snapshot.limits.max_total_output_tokens = 1
    compiler.limits = CompilerLimits(max_total_output_tokens=1)
    compiler.capabilities = ()
    assert snapshot.limits.max_total_output_tokens == 8_192
    assert snapshot.capabilities
    assert not compiler.configuration_matches(snapshot)


async def test_real_catalog_provider_configuration_is_readonly():
    from dataclasses import FrozenInstanceError

    from test_structured_provider import SECRET, settings

    from semantic_api.catalog_v1.provider import CatalogHTTPProvider

    provider = CatalogHTTPProvider(settings("http://127.0.0.1:12345/v1/chat/completions"), SECRET)
    with pytest.raises(FrozenInstanceError):
        provider.configuration_fingerprint = "replacement"
    with pytest.raises(FrozenInstanceError):
        provider.settings = settings("http://127.0.0.1:12346/v1/chat/completions")
    await provider.aclose()


@pytest.mark.parametrize(
    "question",
    [
        "line one\nline two\tvalue\r\n",
        "a" * 3998 + "\U0001f642\U0001f643",
    ],
)
def test_catalog_request_preserves_valid_whitespace_and_scalar_boundary(question):
    from semantic_api.catalog_v1.models import CatalogCompileRequest

    _, request, _, _ = setup()
    parsed = CatalogCompileRequest.model_validate(
        {
            **request.model_dump(mode="json"),
            "question": question,
        }
    )
    assert parsed.question == question


@pytest.mark.parametrize("question", ["a" * 3999 + "\U0001f642\U0001f643", "a\ud800", "a\udc00"])
def test_catalog_request_rejects_excess_scalars_and_surrogates(question):
    from semantic_api.catalog_v1.models import CatalogCompileRequest

    _, request, _, _ = setup()
    with pytest.raises(ValidationError):
        CatalogCompileRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "question": question,
            }
        )
