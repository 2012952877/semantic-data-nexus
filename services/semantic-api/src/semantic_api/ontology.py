from __future__ import annotations

import json
from collections.abc import Iterable
from importlib.resources import files
from typing import Protocol, TypeVar

from semantic_api.models import (
    OntologyDocument,
    OntologyEntity,
    OntologyField,
    OntologyMember,
    OntologyMetric,
    OntologyRelation,
    QueryPolicy,
    ResolvedTerm,
    ResolvedTermKind,
    SemanticContext,
)


class _Identified(Protocol):
    id: str


_IdentifiedType = TypeVar("_IdentifiedType", bound=_Identified)


class OntologyRegistry:
    def __init__(self, document: OntologyDocument) -> None:
        self.document = document
        self.entities = self._unique_index(document.entities, "entity")
        self.fields = self._unique_index(document.fields, "field")
        self.metrics = self._unique_index(document.metrics, "metric")
        self.relations = self._unique_index(document.relations, "relation")
        self.members: dict[str, tuple[str, OntologyMember]] = {}
        for field in document.fields:
            for member in field.members:
                if member.id in self.members:
                    raise ValueError(f"duplicate ontology member ID: {member.id}")
                self.members[member.id] = (field.id, member)

    @staticmethod
    def _unique_index(items: Iterable[_IdentifiedType], kind: str) -> dict[str, _IdentifiedType]:
        index: dict[str, _IdentifiedType] = {}
        for item in items:
            item_id = item.id
            if item_id in index:
                raise ValueError(f"duplicate ontology {kind} ID: {item_id}")
            index[item_id] = item
        return index

    @classmethod
    def load_default(cls) -> OntologyRegistry:
        resource = files("semantic_api.resources").joinpath("ontology.v0.json")
        payload = json.loads(resource.read_text(encoding="utf-8"))
        return cls(OntologyDocument.model_validate(payload))

    def has_entity(self, machine_id: str) -> bool:
        return machine_id in self.entities

    def has_field(self, machine_id: str) -> bool:
        return machine_id in self.fields

    def has_metric(self, machine_id: str) -> bool:
        return machine_id in self.metrics

    def has_relation(self, machine_id: str) -> bool:
        return machine_id in self.relations

    def has_member(self, machine_id: str) -> bool:
        return machine_id in self.members

    def concept_state(self, machine_id: str) -> tuple[bool, QueryPolicy] | None:
        concept: OntologyEntity | OntologyField | OntologyMetric | OntologyRelation | None
        concept = self.entities.get(machine_id)
        if concept is None:
            concept = self.fields.get(machine_id)
        if concept is None:
            concept = self.metrics.get(machine_id)
        if concept is None:
            concept = self.relations.get(machine_id)
        if concept is not None:
            return concept.enabled, concept.query_policy
        member_entry = self.members.get(machine_id)
        if member_entry is not None:
            return member_entry[1].enabled, QueryPolicy.ALLOW
        return None

    def retrieve(
        self,
        question: str,
        resolved_terms: list[ResolvedTerm],
        ontology_scope: list[str],
    ) -> SemanticContext:
        question_folded = question.casefold()
        selected_entity_ids = {
            term.machine_id for term in resolved_terms if term.kind is ResolvedTermKind.ENTITY
        }
        selected_field_ids = {
            term.machine_id for term in resolved_terms if term.kind is ResolvedTermKind.FIELD
        }
        selected_metric_ids = {
            term.machine_id for term in resolved_terms if term.kind is ResolvedTermKind.METRIC
        }

        for scope_id in ontology_scope:
            if scope_id in self.entities:
                selected_entity_ids.add(scope_id)
            elif scope_id in self.fields:
                selected_field_ids.add(scope_id)
                selected_entity_ids.add(self.fields[scope_id].entity_id)
            elif scope_id in self.metrics:
                selected_metric_ids.add(scope_id)
                selected_entity_ids.add(self.metrics[scope_id].entity_id)

        for field in self.document.fields:
            if self._matches(question_folded, field.id, field.label, field.synonyms):
                selected_field_ids.add(field.id)
                selected_entity_ids.add(field.entity_id)
        for metric in self.document.metrics:
            if self._matches(question_folded, metric.id, metric.label, metric.synonyms):
                selected_metric_ids.add(metric.id)
                selected_entity_ids.add(metric.entity_id)
        for entity in self.document.entities:
            if self._matches(question_folded, entity.id, entity.label, entity.synonyms):
                selected_entity_ids.add(entity.id)

        for term in resolved_terms:
            if term.kind is ResolvedTermKind.MEMBER and term.machine_id in self.members:
                field_id = self.members[term.machine_id][0]
                selected_field_ids.add(field_id)
                selected_entity_ids.add(self.fields[field_id].entity_id)

        fields = [
            field
            for field in self.document.fields
            if field.id in selected_field_ids and field.entity_id in selected_entity_ids
        ]
        metrics = [
            metric
            for metric in self.document.metrics
            if metric.id in selected_metric_ids and metric.entity_id in selected_entity_ids
        ]
        relations = [
            relation
            for relation in self.document.relations
            if relation.from_entity_id in selected_entity_ids
            and relation.to_entity_id in selected_entity_ids
        ]
        entities = [entity for entity in self.document.entities if entity.id in selected_entity_ids]
        return SemanticContext(
            ontology_id=self.document.ontology_id,
            ontology_version=self.document.version,
            entities=entities,
            fields=fields,
            metrics=metrics,
            relations=relations,
        )

    @staticmethod
    def _matches(question: str, machine_id: str, label: str, synonyms: list[str]) -> bool:
        return any(
            candidate.casefold() in question
            for candidate in (machine_id, label, *synonyms)
            if candidate
        )

    def typed_entities(self) -> dict[str, OntologyEntity]:
        return {item.id: item for item in self.document.entities}

    def typed_fields(self) -> dict[str, OntologyField]:
        return {item.id: item for item in self.document.fields}

    def typed_metrics(self) -> dict[str, OntologyMetric]:
        return {item.id: item for item in self.document.metrics}

    def typed_relations(self) -> dict[str, OntologyRelation]:
        return {item.id: item for item in self.document.relations}
