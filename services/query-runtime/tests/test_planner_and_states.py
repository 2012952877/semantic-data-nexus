from __future__ import annotations

import pytest
from pydantic import ValidationError

from query_runtime.domain import (
    AggregateFunction,
    BoundColumn,
    BoundSource,
    CapabilityCatalog,
    ExecutionState,
    OperatorKind,
    OperatorSpec,
    PhysicalNode,
    PhysicalNodeKind,
    ScalarType,
    SourceFragment,
)
from query_runtime.errors import BindingFailure, RuntimeFailure
from query_runtime.events import StateMachine
from query_runtime.lineage import LineageRecorder
from query_runtime.planner import (
    CapabilityPlanner,
    ExactConceptBinder,
    LogicalNode,
    ValidatedLogicalGraph,
    topological_order,
)


def _column(concept: str, alias: str = "source") -> BoundColumn:
    return BoundColumn(
        concept=concept,
        source_alias=alias,
        column_name=concept.replace(".", "_"),
        data_type=ScalarType.STRING,
    )


def test_exact_binding_has_clear_missing_and_ambiguous_errors() -> None:
    binder = ExactConceptBinder(
        {"ok": (_column("ok"),), "ambiguous": (_column("a"), _column("b"))}
    )
    assert binder.bind("ok").concept == "ok"
    with pytest.raises(BindingFailure, match="No exact source binding") as missing:
        binder.bind("missing")
    assert missing.value.code == "BINDING_MISSING"
    with pytest.raises(BindingFailure, match="multiple exact") as ambiguous:
        binder.bind("ambiguous")
    assert ambiguous.value.code == "BINDING_AMBIGUOUS"


def test_capability_routing_and_deterministic_waves() -> None:
    graph = ValidatedLogicalGraph(
        id="routing",
        nodes=(
            LogicalNode(
                id="source",
                source_alias="source",
                concepts=("sales.region",),
                operation=OperatorSpec(kind=OperatorKind.SOURCE),
            ),
            LogicalNode(
                id="aggregate",
                dependencies=("source",),
                source_alias="source",
                operation=OperatorSpec(kind=OperatorKind.AGGREGATE),
            ),
            LogicalNode(
                id="pivot",
                dependencies=("aggregate",),
                source_alias="source",
                operation=OperatorSpec(kind=OperatorKind.PIVOT),
            ),
            LogicalNode(
                id="project",
                dependencies=("pivot",),
                source_alias="source",
                operation=OperatorSpec(kind=OperatorKind.PROJECT, columns=("region",)),
            ),
        ),
        output_node_id="project",
    )
    capability = CapabilityCatalog(
        source_alias="source",
        source_type="synthetic",
        operator_kinds=frozenset(
            {OperatorKind.SOURCE, OperatorKind.AGGREGATE, OperatorKind.PROJECT}
        ),
        aggregate_functions=frozenset({AggregateFunction.SUM}),
    )
    planner = CapabilityPlanner(
        binder=ExactConceptBinder({"sales.region": (_column("sales.region"),)}),
        sources={
            "source": BoundSource(
                alias="source", source_type="synthetic", object_name="facts"
            )
        },
        capabilities={"source": capability},
    )
    plan = planner.plan(graph)
    assert [node.wave for node in plan.nodes] == [0, 1, 2]
    assert [node.kind for node in plan.nodes] == [
        PhysicalNodeKind.SOURCE_FRAGMENT,
        PhysicalNodeKind.OPERATOR,
        PhysicalNodeKind.OPERATOR,
    ]
    assert plan.nodes[0].source_fragment is not None
    assert [item.kind for item in plan.nodes[0].source_fragment.operations] == [
        OperatorKind.SOURCE,
        OperatorKind.AGGREGATE,
    ]
    assert plan.nodes[0].source_fragment.bound_columns == (
        _column("sales.region"),
    )
    assert [item.operation for item in plan.nodes[0].logical_operations] == [
        OperatorKind.SOURCE,
        OperatorKind.AGGREGATE,
    ]
    lineage = LineageRecorder("run-routing", plan).graph()
    logical_operations = {
        node.id: node.operation for node in lineage.nodes if node.kind == "logical"
    }
    assert logical_operations == {
        "logical:source": OperatorKind.SOURCE.value,
        "logical:aggregate": OperatorKind.AGGREGATE.value,
        "logical:pivot": OperatorKind.PIVOT.value,
        "logical:project": OperatorKind.PROJECT.value,
    }
    assert topological_order(tuple(reversed(graph.nodes))) == graph.nodes


def test_finite_state_transitions() -> None:
    machine = StateMachine()
    machine.transition(ExecutionState.READY)
    machine.transition(ExecutionState.RUNNING)
    machine.transition(ExecutionState.SUCCEEDED)
    with pytest.raises(RuntimeFailure) as error:
        machine.transition(ExecutionState.RUNNING)
    assert error.value.code == "STATE_TRANSITION_INVALID"


def test_capability_specific_limits_retain_operator_locally() -> None:
    aggregate = LogicalNode(
        id="aggregate",
        source_alias="source",
        estimated_rows=2_000,
        operation=OperatorSpec(
            kind=OperatorKind.AGGREGATE,
            aggregates=(),
        ),
    )
    graph = ValidatedLogicalGraph(
        id="limited", nodes=(aggregate,), output_node_id=aggregate.id
    )
    planner = CapabilityPlanner(
        binder=ExactConceptBinder({}),
        sources={
            "source": BoundSource(
                alias="source", source_type="synthetic", object_name="facts"
            )
        },
        capabilities={
            "source": CapabilityCatalog(
                source_alias="source",
                source_type="synthetic",
                operator_kinds=frozenset({OperatorKind.AGGREGATE}),
                aggregate_functions=frozenset({AggregateFunction.COUNT}),
                max_rows=100,
            )
        },
    )
    assert planner.plan(graph).nodes[0].kind is PhysicalNodeKind.OPERATOR


def test_physical_node_operation_must_match_payload() -> None:
    with pytest.raises(ValidationError, match="must match"):
        PhysicalNode(
            id="mismatch",
            kind=PhysicalNodeKind.OPERATOR,
            operation=OperatorKind.SORT,
            wave=0,
            logical_node_ids=("logical",),
            operator=OperatorSpec(kind=OperatorKind.SELECT, columns=("x",)),
        )


def test_fused_physical_node_requires_each_logical_operation() -> None:
    with pytest.raises(ValidationError, match="fused nodes require"):
        PhysicalNode(
            id="fused-missing-lineage",
            kind=PhysicalNodeKind.SOURCE_FRAGMENT,
            operation=OperatorKind.FILTER,
            wave=0,
            logical_node_ids=("source", "filter"),
            source_fragment=SourceFragment(
                source=BoundSource(
                    alias="source",
                    source_type="synthetic",
                    object_name="facts",
                ),
                operations=(
                    OperatorSpec(kind=OperatorKind.SOURCE),
                    OperatorSpec(kind=OperatorKind.FILTER),
                ),
            ),
        )


def test_cross_source_binding_is_rejected() -> None:
    graph = ValidatedLogicalGraph(
        id="cross-source",
        nodes=(
            LogicalNode(
                id="source",
                source_alias="primary",
                concepts=("sales.amount",),
                operation=OperatorSpec(kind=OperatorKind.SOURCE),
            ),
        ),
        output_node_id="source",
    )
    planner = CapabilityPlanner(
        binder=ExactConceptBinder(
            {"sales.amount": (_column("sales.amount", alias="secondary"),)}
        ),
        sources={
            "primary": BoundSource(
                alias="primary", source_type="synthetic", object_name="facts"
            )
        },
        capabilities={
            "primary": CapabilityCatalog(
                source_alias="primary",
                source_type="synthetic",
                operator_kinds=frozenset({OperatorKind.SOURCE}),
            )
        },
    )
    with pytest.raises(BindingFailure) as error:
        planner.plan(graph)
    assert error.value.code == "BINDING_SOURCE_MISMATCH"


def test_exact_binding_rewrites_concept_to_physical_column() -> None:
    graph = ValidatedLogicalGraph(
        id="physical-column",
        nodes=(
            LogicalNode(
                id="select",
                source_alias="source",
                concepts=("sales.region",),
                operation=OperatorSpec(
                    kind=OperatorKind.SELECT,
                    columns=("sales.region",),
                ),
            ),
        ),
        output_node_id="select",
    )
    planner = CapabilityPlanner(
        binder=ExactConceptBinder({"sales.region": (_column("sales.region"),)}),
        sources={
            "source": BoundSource(
                alias="source", source_type="synthetic", object_name="facts"
            )
        },
        capabilities={
            "source": CapabilityCatalog(
                source_alias="source",
                source_type="synthetic",
                operator_kinds=frozenset({OperatorKind.SELECT}),
            )
        },
    )
    plan = planner.plan(graph)
    fragment = plan.nodes[0].source_fragment
    assert fragment is not None
    assert fragment.operations[0].columns == ("sales_region",)


def test_supported_operation_after_local_node_stays_local() -> None:
    graph = ValidatedLogicalGraph(
        id="local-boundary",
        nodes=(
            LogicalNode(
                id="source",
                source_alias="source",
                operation=OperatorSpec(kind=OperatorKind.SOURCE),
            ),
            LogicalNode(
                id="pivot",
                source_alias="source",
                dependencies=("source",),
                operation=OperatorSpec(kind=OperatorKind.PIVOT),
            ),
            LogicalNode(
                id="sort",
                source_alias="source",
                dependencies=("pivot",),
                operation=OperatorSpec(kind=OperatorKind.SORT),
            ),
            LogicalNode(
                id="join",
                source_alias="source",
                dependencies=("source", "sort"),
                operation=OperatorSpec(kind=OperatorKind.JOIN),
            ),
        ),
        output_node_id="join",
    )
    capability = CapabilityCatalog(
        source_alias="source",
        source_type="synthetic",
        operator_kinds=frozenset(
            {
                OperatorKind.SOURCE,
                OperatorKind.SORT,
                OperatorKind.JOIN,
            }
        ),
        joins=True,
    )
    plan = CapabilityPlanner(
        binder=ExactConceptBinder({}),
        sources={
            "source": BoundSource(
                alias="source", source_type="synthetic", object_name="facts"
            )
        },
        capabilities={"source": capability},
    ).plan(graph)
    by_operation = {node.operation: node for node in plan.nodes}
    assert by_operation[OperatorKind.SORT].kind is PhysicalNodeKind.OPERATOR
    assert by_operation[OperatorKind.JOIN].kind is PhysicalNodeKind.OPERATOR
