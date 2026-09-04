"""Original synthetic fixtures for offline runtime development."""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa

from query_runtime.domain import (
    AggregateFunction,
    AggregateSpec,
    BoundSource,
    CapabilityCatalog,
    ExpressionKind,
    JoinKey,
    JoinType,
    NamedExpression,
    OperatorKind,
    OperatorSpec,
    PhysicalNode,
    PhysicalNodeKind,
    PhysicalPlan,
    ScalarType,
    SortDirection,
    SortSpec,
    SourceFragment,
    TypedExpression,
)
from query_runtime.resolver import FakeResolver


@dataclass(frozen=True)
class Fixture:
    plan: PhysicalPlan
    resolver: FakeResolver


def _source(alias: str) -> BoundSource:
    return BoundSource(alias=alias, source_type="synthetic", object_name=f"{alias}_facts")


def _catalog(alias: str) -> CapabilityCatalog:
    return CapabilityCatalog(
        source_alias=alias,
        source_type="synthetic",
        operator_kinds=frozenset(
            {
                OperatorKind.SOURCE,
                OperatorKind.SELECT,
                OperatorKind.FILTER,
                OperatorKind.AGGREGATE,
                OperatorKind.SORT,
                OperatorKind.LIMIT,
            }
        ),
        aggregate_functions=frozenset(AggregateFunction),
        max_rows=100_000,
        max_bytes=32 * 1024 * 1024,
    )


def _profit_expression() -> TypedExpression:
    return TypedExpression(
        kind=ExpressionKind.SUBTRACT,
        data_type=ScalarType.FLOAT,
        args=(
            TypedExpression.col("revenue", ScalarType.FLOAT),
            TypedExpression.col("cost", ScalarType.FLOAT),
        ),
    )


def simple_profit_fixture() -> Fixture:
    alias = "regional_source"
    source = _source(alias)
    aggregate = OperatorSpec(
        kind=OperatorKind.AGGREGATE,
        group_by=("region", "quarter"),
        aggregates=(
            AggregateSpec(
                name="profit",
                function=AggregateFunction.SUM,
                expression=_profit_expression(),
            ),
        ),
    )
    node = PhysicalNode(
        id="source-profit",
        kind=PhysicalNodeKind.SOURCE_FRAGMENT,
        operation=OperatorKind.AGGREGATE,
        wave=0,
        logical_node_ids=("logical-profit",),
        source_fragment=SourceFragment(
            source=source,
            operations=(OperatorSpec(kind=OperatorKind.SOURCE), aggregate),
        ),
    )
    plan = PhysicalPlan(id="simple-profit", nodes=(node,), output_node_id=node.id)
    table = pa.table(
        {
            "region": ["North", "North", "South", "South"],
            "quarter": ["Q1", "Q2", "Q1", "Q2"],
            "revenue": [120.0, 150.0, 90.0, 110.0],
            "cost": [80.0, 100.0, 95.0, 70.0],
        }
    )
    resolver = FakeResolver(tables={alias: table}, catalogs={alias: _catalog(alias)})
    return Fixture(plan, resolver)


def complex_profit_fixture() -> Fixture:
    base = simple_profit_fixture()
    source_node = base.plan.nodes[0]
    pivot = PhysicalNode(
        id="local-pivot",
        kind=PhysicalNodeKind.OPERATOR,
        operation=OperatorKind.PIVOT,
        dependencies=(source_node.id,),
        wave=1,
        logical_node_ids=("logical-pivot",),
        operator=OperatorSpec(
            kind=OperatorKind.PIVOT,
            pivot_index=("region",),
            pivot_column="quarter",
            pivot_value="profit",
            pivot_values=("Q1", "Q2"),
        ),
    )
    zero = TypedExpression.literal(0.0, ScalarType.FLOAT)
    q1 = TypedExpression(
        kind=ExpressionKind.COALESCE,
        data_type=ScalarType.FLOAT,
        args=(TypedExpression.col("Q1", ScalarType.FLOAT), zero),
    )
    q2 = TypedExpression(
        kind=ExpressionKind.COALESCE,
        data_type=ScalarType.FLOAT,
        args=(TypedExpression.col("Q2", ScalarType.FLOAT), zero),
    )
    derive = PhysicalNode(
        id="local-derive",
        kind=PhysicalNodeKind.OPERATOR,
        operation=OperatorKind.DERIVE,
        dependencies=(pivot.id,),
        wave=2,
        logical_node_ids=("logical-derive",),
        operator=OperatorSpec(
            kind=OperatorKind.DERIVE,
            expressions=(
                NamedExpression(
                    name="total_profit",
                    expression=TypedExpression(
                        kind=ExpressionKind.ADD,
                        data_type=ScalarType.FLOAT,
                        args=(q1, q2),
                    ),
                ),
            ),
        ),
    )
    project = PhysicalNode(
        id="local-project",
        kind=PhysicalNodeKind.OPERATOR,
        operation=OperatorKind.PROJECT,
        dependencies=(derive.id,),
        wave=3,
        logical_node_ids=("logical-project",),
        operator=OperatorSpec(
            kind=OperatorKind.PROJECT,
            columns=("region", "Q1", "Q2", "total_profit"),
        ),
    )
    return Fixture(
        PhysicalPlan(
            id="complex-profit",
            nodes=(source_node, pivot, derive, project),
            output_node_id=project.id,
        ),
        base.resolver,
    )


def join_sort_fixture() -> Fixture:
    region_alias = "region_source"
    score_alias = "score_source"
    region_node = PhysicalNode(
        id="source-regions",
        kind=PhysicalNodeKind.SOURCE_FRAGMENT,
        operation=OperatorKind.SOURCE,
        wave=0,
        logical_node_ids=("logical-regions",),
        source_fragment=SourceFragment(
            source=_source(region_alias),
            operations=(OperatorSpec(kind=OperatorKind.SOURCE),),
        ),
    )
    score_node = PhysicalNode(
        id="source-scores",
        kind=PhysicalNodeKind.SOURCE_FRAGMENT,
        operation=OperatorKind.SOURCE,
        wave=0,
        logical_node_ids=("logical-scores",),
        source_fragment=SourceFragment(
            source=_source(score_alias),
            operations=(OperatorSpec(kind=OperatorKind.SOURCE),),
        ),
    )
    join = PhysicalNode(
        id="local-join",
        kind=PhysicalNodeKind.OPERATOR,
        operation=OperatorKind.JOIN,
        dependencies=(region_node.id, score_node.id),
        wave=1,
        logical_node_ids=("logical-join",),
        operator=OperatorSpec(
            kind=OperatorKind.JOIN,
            join_type=JoinType.INNER,
            join_keys=(JoinKey(left="region_key", right="score_region_key"),),
        ),
    )
    sort = PhysicalNode(
        id="local-sort",
        kind=PhysicalNodeKind.OPERATOR,
        operation=OperatorKind.SORT,
        dependencies=(join.id,),
        wave=2,
        logical_node_ids=("logical-sort",),
        operator=OperatorSpec(
            kind=OperatorKind.SORT,
            sort=(SortSpec(column="score", direction=SortDirection.DESC),),
        ),
    )
    tables = {
        region_alias: pa.table(
            {"region_key": [1, 2], "region_name": ["North", "South"]}
        ),
        score_alias: pa.table(
            {"score_region_key": [2, 1], "score": [7.0, 9.0]}
        ),
    }
    catalogs = {
        region_alias: _catalog(region_alias),
        score_alias: _catalog(score_alias),
    }
    return Fixture(
        PhysicalPlan(
            id="join-sort",
            nodes=(region_node, score_node, join, sort),
            output_node_id=sort.id,
        ),
        FakeResolver(tables=tables, catalogs=catalogs),
    )


def zero_rows_fixture() -> Fixture:
    fixture = simple_profit_fixture()
    fixture.resolver._tables["regional_source"] = fixture.resolver._tables[
        "regional_source"
    ].slice(0, 0)
    return fixture


def source_failure_fixture() -> Fixture:
    fixture = simple_profit_fixture()
    fixture.resolver._failures["regional_source"] = "Synthetic source failure"
    return fixture


def local_operator_failure_fixture() -> Fixture:
    fixture = complex_profit_fixture()
    failing = fixture.plan.nodes[-1].model_copy(
        update={
            "operator": OperatorSpec(
                kind=OperatorKind.PROJECT, columns=("missing_column",)
            )
        }
    )
    return Fixture(
        fixture.plan.model_copy(
            update={"nodes": (*fixture.plan.nodes[:-1], failing)}
        ),
        fixture.resolver,
    )


def delayed_fixture(delay_seconds: float = 1.0) -> Fixture:
    fixture = simple_profit_fixture()
    fixture.resolver._delays["regional_source"] = delay_seconds
    return fixture


def invalid_dag_fixture() -> PhysicalPlan:
    node = PhysicalNode(
        id="invalid-output",
        kind=PhysicalNodeKind.OPERATOR,
        operation=OperatorKind.PROJECT,
        dependencies=("missing-node",),
        wave=1,
        logical_node_ids=("logical-invalid",),
        operator=OperatorSpec(kind=OperatorKind.PROJECT, columns=("value",)),
    )
    return PhysicalPlan(id="invalid-dag", nodes=(node,), output_node_id=node.id)


def fixture_by_name(name: str) -> Fixture:
    fixtures = {
        "simple": simple_profit_fixture,
        "complex": complex_profit_fixture,
        "join": join_sort_fixture,
        "zero": zero_rows_fixture,
    }
    return fixtures[name]()
