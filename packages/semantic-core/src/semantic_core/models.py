from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)

Identifier = Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DecimalLiteral(ContractModel):
    kind: Literal["decimal"]
    value: Annotated[
        str,
        Field(pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?(0|[1-9][0-9]*))?$"),
    ]


JsonScalar = str | StrictInt | StrictFloat | StrictBool | None | DecimalLiteral


class DataType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"


class Aggregation(StrEnum):
    SUM = "SUM"
    AVG = "AVG"
    MIN = "MIN"
    MAX = "MAX"
    COUNT = "COUNT"
    COUNT_DISTINCT = "COUNT_DISTINCT"


def aggregation_output_type(
    aggregation: Aggregation, source_type: DataType
) -> DataType:
    numeric = {DataType.INTEGER, DataType.NUMBER, DataType.DECIMAL}
    if aggregation in {Aggregation.SUM, Aggregation.AVG} and source_type not in numeric:
        raise ValueError(
            f"{aggregation.value} does not support source type {source_type.value!r}"
        )
    if aggregation in {Aggregation.MIN, Aggregation.MAX} and source_type == DataType.BOOLEAN:
        raise ValueError(
            f"{aggregation.value} does not support source type {source_type.value!r}"
        )
    if aggregation in {Aggregation.COUNT, Aggregation.COUNT_DISTINCT}:
        return DataType.INTEGER
    if aggregation == Aggregation.AVG and source_type == DataType.INTEGER:
        return DataType.NUMBER
    return source_type


class OntologyField(ContractModel):
    id: Identifier
    data_type: DataType
    nullable: StrictBool = True
    description: str | None = None


class Entity(ContractModel):
    id: Identifier
    description: str | None = None
    fields: list[OntologyField] = Field(min_length=1)

    @model_validator(mode="after")
    def fields_are_unique(self) -> Entity:
        ids = [field.id for field in self.fields]
        if len(ids) != len(set(ids)):
            raise ValueError(f"entity {self.id!r} contains duplicate field IDs")
        return self


class Metric(ContractModel):
    id: Identifier
    entity: Identifier
    field: Identifier
    aggregation: Aggregation
    description: str | None = None


class Relation(ContractModel):
    id: Identifier
    left_entity: Identifier
    right_entity: Identifier
    left_field: Identifier
    right_field: Identifier
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]


class Resolver(ContractModel):
    id: Identifier
    entity: Identifier
    field: Identifier
    kind: Literal["static", "search", "databricks"]


class Ontology(ContractModel):
    contract_version: Literal["ontology/v0"]
    ontology_id: Identifier
    version: Identifier
    entities: list[Entity] = Field(min_length=1)
    metrics: list[Metric] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    resolvers: list[Resolver] = Field(default_factory=list)

    @model_validator(mode="after")
    def concepts_are_consistent(self) -> Ontology:
        self._require_unique("entity", [entity.id for entity in self.entities])
        self._require_unique("metric", [metric.id for metric in self.metrics])
        self._require_unique("relation", [relation.id for relation in self.relations])
        self._require_unique("resolver", [resolver.id for resolver in self.resolvers])

        entities = {
            entity.id: {field.id: field for field in entity.fields}
            for entity in self.entities
        }
        for metric in self.metrics:
            self._require_field(entities, metric.entity, metric.field, f"metric {metric.id!r}")
            entity = next(entity for entity in self.entities if entity.id == metric.entity)
            source = next(field for field in entity.fields if field.id == metric.field)
            aggregation_output_type(metric.aggregation, source.data_type)
        for relation in self.relations:
            self._require_field(
                entities, relation.left_entity, relation.left_field, f"relation {relation.id!r}"
            )
            self._require_field(
                entities, relation.right_entity, relation.right_field, f"relation {relation.id!r}"
            )
            left_type = entities[relation.left_entity][relation.left_field].data_type
            right_type = entities[relation.right_entity][relation.right_field].data_type
            if left_type != right_type:
                raise ValueError(
                    f"relation {relation.id!r} joins incompatible key types "
                    f"{left_type.value!r} and {right_type.value!r}"
                )
        for resolver in self.resolvers:
            self._require_field(
                entities, resolver.entity, resolver.field, f"resolver {resolver.id!r}"
            )
        return self

    @staticmethod
    def _require_unique(kind: str, identifiers: list[str]) -> None:
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"ontology contains duplicate {kind} IDs")

    @staticmethod
    def _require_field(
        entities: dict[str, dict[str, OntologyField]],
        entity: str,
        field: str,
        owner: str,
    ) -> None:
        if entity not in entities:
            raise ValueError(f"{owner} references unknown entity {entity!r}")
        if field not in entities[entity]:
            raise ValueError(f"{owner} references unknown field {entity}.{field}")


class OutputField(ContractModel):
    name: Identifier
    data_type: DataType
    nullable: StrictBool = True


class SelectParams(ContractModel):
    entity: Identifier
    fields: list[Identifier] = Field(min_length=1)


class Predicate(ContractModel):
    field: Identifier
    op: Literal["EQ", "NEQ", "GT", "GTE", "LT", "LTE", "IN", "NOT_IN", "CONTAINS"]
    value: JsonScalar | list[JsonScalar]

    @model_validator(mode="after")
    def collection_operators_use_lists(self) -> Predicate:
        is_collection = self.op in {"IN", "NOT_IN"}
        if is_collection and (not isinstance(self.value, list) or not self.value):
            raise ValueError(f"{self.op} requires a non-empty list value")
        if not is_collection and isinstance(self.value, list):
            raise ValueError(f"{self.op} requires a scalar value")
        return self


class FilterParams(ContractModel):
    predicate: Predicate


class MeasureRef(ContractModel):
    metric: Identifier
    name: Identifier


class AggregateParams(ContractModel):
    group_by: list[Identifier] = Field(default_factory=list)
    measures: list[MeasureRef] = Field(min_length=1)


class PivotParams(ContractModel):
    index: list[Identifier] = Field(min_length=1)
    column: Identifier
    value: Identifier
    aggregation: Aggregation
    expected_columns: list[Identifier] = Field(min_length=1)


class FieldOperand(ContractModel):
    kind: Literal["field"]
    field: Identifier


class LiteralOperand(ContractModel):
    kind: Literal["literal"]
    value: JsonScalar

    @model_validator(mode="after")
    def floats_use_tagged_decimal(self) -> LiteralOperand:
        if isinstance(self.value, float):
            raise ValueError(
                "DERIVE floating-point literals must use the tagged decimal form"
            )
        return self


ScalarOperand = Annotated[Union[FieldOperand, LiteralOperand], Field(discriminator="kind")]


class DeriveExpression(ContractModel):
    name: Identifier
    op: Literal["ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"]
    left: ScalarOperand
    right: ScalarOperand


class DeriveParams(ContractModel):
    expressions: list[DeriveExpression] = Field(min_length=1)


class ProjectParams(ContractModel):
    fields: list[Identifier] = Field(min_length=1)


class SortKey(ContractModel):
    field: Identifier
    direction: Literal["ASC", "DESC"] = "ASC"


class SortParams(ContractModel):
    keys: list[SortKey] = Field(min_length=1)


class JoinProjection(ContractModel):
    source: Literal["left", "right"]
    field: Identifier
    name: Identifier


class JoinParams(ContractModel):
    relation: Identifier
    kind: Literal["INNER", "LEFT", "RIGHT", "FULL"]
    fields: list[JoinProjection] = Field(min_length=1)


class BaseNode(ContractModel):
    id: Identifier
    inputs: list[Identifier]
    outputs: list[OutputField] = Field(min_length=1)

    @model_validator(mode="after")
    def output_names_are_unique(self) -> BaseNode:
        names = [output.name for output in self.outputs]
        if len(names) != len(set(names)):
            raise ValueError(f"node {self.id!r} contains duplicate output names")
        return self


class SelectNode(BaseNode):
    operator: Literal["SELECT"]
    params: SelectParams


class FilterNode(BaseNode):
    operator: Literal["FILTER"]
    params: FilterParams


class AggregateNode(BaseNode):
    operator: Literal["AGGREGATE"]
    params: AggregateParams


class PivotNode(BaseNode):
    operator: Literal["PIVOT"]
    params: PivotParams


class DeriveNode(BaseNode):
    operator: Literal["DERIVE"]
    params: DeriveParams


class ProjectNode(BaseNode):
    operator: Literal["PROJECT"]
    params: ProjectParams


class SortNode(BaseNode):
    operator: Literal["SORT"]
    params: SortParams


class JoinNode(BaseNode):
    operator: Literal["JOIN"]
    params: JoinParams


SQGNode = Annotated[
    Union[
        SelectNode,
        FilterNode,
        AggregateNode,
        PivotNode,
        DeriveNode,
        ProjectNode,
        SortNode,
        JoinNode,
    ],
    Field(discriminator="operator"),
]


class SemanticQueryGraph(ContractModel):
    contract_version: Literal["sqg/v0"]
    query_id: Identifier
    ontology_version: Identifier
    nodes: list[SQGNode] = Field(min_length=1)
    root: Identifier

    @model_validator(mode="after")
    def graph_is_deterministic(self) -> SemanticQueryGraph:
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("SQG contains duplicate node IDs")
        if self.root not in set(ids):
            raise ValueError(f"root references missing node {self.root!r}")

        positions = {node_id: index for index, node_id in enumerate(ids)}
        outputs: dict[str, dict[str, OutputField]] = {}
        for index, node in enumerate(self.nodes):
            expected_inputs = 0 if node.operator == "SELECT" else 2 if node.operator == "JOIN" else 1
            if len(node.inputs) != expected_inputs:
                raise ValueError(
                    f"node {node.id!r} ({node.operator}) requires {expected_inputs} input(s)"
                )
            for dependency in node.inputs:
                if dependency == node.id:
                    raise ValueError(f"node {node.id!r} cannot depend on itself")
                if dependency not in positions:
                    raise ValueError(f"node {node.id!r} references missing input {dependency!r}")
                if positions[dependency] >= index:
                    raise ValueError(
                        f"node {node.id!r} has forward dependency {dependency!r}; "
                        "dependencies must appear first"
                    )

            parent_outputs = [outputs[dependency] for dependency in node.inputs]
            expected_outputs = self._validate_operator(node, parent_outputs)
            actual_outputs = [output.name for output in node.outputs]
            if actual_outputs != expected_outputs:
                raise ValueError(
                    f"node {node.id!r} outputs {actual_outputs!r}, "
                    f"expected {expected_outputs!r}"
                )
            expected_contracts = self._expected_output_contracts(node, parent_outputs)
            for output in node.outputs:
                expected = expected_contracts.get(output.name)
                if expected is not None and output.data_type != expected[0]:
                    raise ValueError(
                        f"node {node.id!r} output {output.name!r} has type "
                        f"{output.data_type.value!r}, expected {expected[0].value!r}"
                    )
                if expected is not None and output.nullable != expected[1]:
                    raise ValueError(
                        f"node {node.id!r} output {output.name!r} has nullable="
                        f"{output.nullable}, expected nullable={expected[1]}"
                    )
            outputs[node.id] = {output.name: output for output in node.outputs}

        nodes_by_id = {node.id: node for node in self.nodes}
        ancestors: set[str] = set()
        pending = [self.root]
        while pending:
            node_id = pending.pop()
            if node_id in ancestors:
                continue
            ancestors.add(node_id)
            pending.extend(nodes_by_id[node_id].inputs)
        unreachable = [node_id for node_id in ids if node_id not in ancestors]
        if unreachable:
            raise ValueError(
                f"root {self.root!r} does not consume all nodes; "
                f"outside root ancestor closure: {unreachable!r}"
            )
        return self

    @staticmethod
    def _require_fields(node_id: str, fields: list[str], available: set[str]) -> None:
        missing = [field for field in fields if field not in available]
        if missing:
            raise ValueError(f"node {node_id!r} references unavailable fields {missing!r}")

    @classmethod
    def _validate_operator(
        cls, node: SQGNode, parent_outputs: list[dict[str, OutputField]]
    ) -> list[str]:
        if isinstance(node, SelectNode):
            return list(node.params.fields)
        if isinstance(node, FilterNode):
            cls._require_fields(
                node.id, [node.params.predicate.field], set(parent_outputs[0])
            )
            cls._validate_predicate_type(
                node.id,
                node.params.predicate,
                parent_outputs[0][node.params.predicate.field].data_type,
            )
            return list(parent_outputs[0])
        if isinstance(node, AggregateNode):
            cls._require_fields(node.id, node.params.group_by, set(parent_outputs[0]))
            names = node.params.group_by + [measure.name for measure in node.params.measures]
            if len(names) != len(set(names)):
                raise ValueError(f"node {node.id!r} contains duplicate aggregate output names")
            return names
        if isinstance(node, PivotNode):
            cls._require_fields(
                node.id,
                node.params.index + [node.params.column, node.params.value],
                set(parent_outputs[0]),
            )
            names = node.params.index + node.params.expected_columns
            if len(names) != len(set(names)):
                raise ValueError(f"node {node.id!r} contains duplicate pivot output names")
            return names
        if isinstance(node, DeriveNode):
            names = [expression.name for expression in node.params.expressions]
            if len(names) != len(set(names)) or set(names) & set(parent_outputs[0]):
                raise ValueError(f"node {node.id!r} contains duplicate derived output names")
            for expression in node.params.expressions:
                operands = [expression.left, expression.right]
                fields = [
                    operand.field for operand in operands if isinstance(operand, FieldOperand)
                ]
                cls._require_fields(node.id, fields, set(parent_outputs[0]))
            return list(parent_outputs[0]) + names
        if isinstance(node, ProjectNode):
            cls._require_fields(node.id, node.params.fields, set(parent_outputs[0]))
            return list(node.params.fields)
        if isinstance(node, SortNode):
            cls._require_fields(
                node.id, [key.field for key in node.params.keys], set(parent_outputs[0])
            )
            return list(parent_outputs[0])
        if isinstance(node, JoinNode):
            for projection in node.params.fields:
                source_index = 0 if projection.source == "left" else 1
                cls._require_fields(
                    node.id, [projection.field], set(parent_outputs[source_index])
                )
            names = [projection.name for projection in node.params.fields]
            if len(names) != len(set(names)):
                raise ValueError(f"node {node.id!r} contains duplicate join output names")
            return names
        raise AssertionError(f"unhandled SQG node type: {type(node).__name__}")

    @staticmethod
    def _expected_output_contracts(
        node: SQGNode, parent_outputs: list[dict[str, OutputField]]
    ) -> dict[str, tuple[DataType, bool]]:
        if isinstance(node, (FilterNode, SortNode)):
            return {
                name: (output.data_type, output.nullable)
                for name, output in parent_outputs[0].items()
            }
        if isinstance(node, ProjectNode):
            return {
                field: (
                    parent_outputs[0][field].data_type,
                    parent_outputs[0][field].nullable,
                )
                for field in node.params.fields
            }
        if isinstance(node, AggregateNode):
            return {
                field: (
                    parent_outputs[0][field].data_type,
                    parent_outputs[0][field].nullable,
                )
                for field in node.params.group_by
            }
        if isinstance(node, PivotNode):
            value_type = aggregation_output_type(
                node.params.aggregation,
                parent_outputs[0][node.params.value].data_type,
            )
            return {
                **{
                    field: (
                        parent_outputs[0][field].data_type,
                        parent_outputs[0][field].nullable,
                    )
                    for field in node.params.index
                },
                **{field: (value_type, True) for field in node.params.expected_columns},
            }
        if isinstance(node, DeriveNode):
            return {
                **{
                    name: (output.data_type, output.nullable)
                    for name, output in parent_outputs[0].items()
                },
                **{
                    expression.name: (
                        SemanticQueryGraph._derive_output_type(
                            node.id, expression, parent_outputs[0]
                        ),
                        SemanticQueryGraph._derive_output_nullable(
                            expression, parent_outputs[0]
                        ),
                    )
                    for expression in node.params.expressions
                },
            }
        if isinstance(node, JoinNode):
            return {
                projection.name: SemanticQueryGraph._join_output_contract(
                    node,
                    projection,
                    parent_outputs[0 if projection.source == "left" else 1][
                        projection.field
                    ],
                )
                for projection in node.params.fields
            }
        return {}

    @staticmethod
    def _join_output_contract(
        node: JoinNode, projection: JoinProjection, source: OutputField
    ) -> tuple[DataType, bool]:
        outer_nullable = (
            node.params.kind == "FULL"
            or node.params.kind == "LEFT"
            and projection.source == "right"
            or node.params.kind == "RIGHT"
            and projection.source == "left"
        )
        return source.data_type, source.nullable or outer_nullable

    @staticmethod
    def _validate_predicate_type(
        node_id: str, predicate: Predicate, field_type: DataType
    ) -> None:
        values = predicate.value if isinstance(predicate.value, list) else [predicate.value]
        if predicate.op in {"GT", "GTE", "LT", "LTE", "CONTAINS"} and any(
            value is None for value in values
        ):
            raise ValueError(f"node {node_id!r} {predicate.op} does not accept null")
        if predicate.op == "CONTAINS" and field_type != DataType.STRING:
            raise ValueError(f"node {node_id!r} CONTAINS requires a string field")
        if predicate.op in {"GT", "GTE", "LT", "LTE"} and field_type == DataType.BOOLEAN:
            raise ValueError(f"node {node_id!r} cannot order a boolean field")
        if not all(
            SemanticQueryGraph._scalar_matches_type(value, field_type) for value in values
        ):
            raise ValueError(
                f"node {node_id!r} predicate value does not match "
                f"field {predicate.field!r} type {field_type.value!r}"
            )

    @staticmethod
    def _scalar_matches_type(value: JsonScalar, data_type: DataType) -> bool:
        if value is None:
            return True
        if data_type == DataType.STRING:
            return isinstance(value, str)
        if data_type == DataType.INTEGER:
            return isinstance(value, int) and not isinstance(value, bool)
        if data_type == DataType.NUMBER:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if data_type == DataType.DECIMAL:
            return (
                isinstance(value, int)
                and not isinstance(value, bool)
                or isinstance(value, DecimalLiteral)
            )
        if data_type == DataType.BOOLEAN:
            return isinstance(value, bool)
        if data_type == DataType.DATE and isinstance(value, str):
            try:
                return date.fromisoformat(value).isoformat() == value
            except ValueError:
                return False
        if data_type == DataType.DATETIME and isinstance(value, str):
            if "T" not in value:
                return False
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return False
            return parsed.tzinfo is not None
        return False

    @staticmethod
    def _derive_output_type(
        node_id: str,
        expression: DeriveExpression,
        available: dict[str, OutputField],
    ) -> DataType:
        operand_types: list[DataType] = []
        for operand in (expression.left, expression.right):
            if isinstance(operand, FieldOperand):
                operand_types.append(available[operand.field].data_type)
            elif isinstance(operand.value, bool) or operand.value is None:
                raise ValueError(
                    f"node {node_id!r} derive expression {expression.name!r} "
                    "requires numeric operands"
                )
            elif isinstance(operand.value, int):
                operand_types.append(DataType.INTEGER)
            elif isinstance(operand.value, float):
                operand_types.append(DataType.NUMBER)
            elif isinstance(operand.value, DecimalLiteral):
                operand_types.append(DataType.DECIMAL)
            else:
                raise ValueError(
                    f"node {node_id!r} derive expression {expression.name!r} "
                    "requires numeric operands"
                )

        numeric_types = {DataType.INTEGER, DataType.NUMBER, DataType.DECIMAL}
        if any(operand_type not in numeric_types for operand_type in operand_types):
            raise ValueError(
                f"node {node_id!r} derive expression {expression.name!r} "
                "requires numeric operands"
            )
        if DataType.DECIMAL in operand_types:
            return DataType.DECIMAL
        if expression.op == "DIVIDE" or DataType.NUMBER in operand_types:
            return DataType.NUMBER
        return DataType.INTEGER

    @staticmethod
    def _derive_output_nullable(
        expression: DeriveExpression, available: dict[str, OutputField]
    ) -> bool:
        return any(
            isinstance(operand, FieldOperand) and available[operand.field].nullable
            for operand in (expression.left, expression.right)
        )
