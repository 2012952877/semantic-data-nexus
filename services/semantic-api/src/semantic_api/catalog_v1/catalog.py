from __future__ import annotations

import hashlib
import json
from typing import Protocol

from semantic_api.catalog_v1.models import (
    CatalogDocument,
    CompilerFailure,
    MemberPredicate,
    ResourceVersion,
    Scalar,
    Value,
)
from semantic_api.catalog_v1.trust import CatalogAccess


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
    ).hexdigest()


def pin_for(document: CatalogDocument) -> ResourceVersion:
    return ResourceVersion(
        scope=document.scope,
        resource_id=document.resource_id,
        revision=document.revision,
        content_sha256=fingerprint(document.model_dump(mode="json")),
    )


def value_matches(value: Value, data_type: Scalar) -> bool:
    return (
        (data_type == "string" and isinstance(value, str))
        or (data_type == "boolean" and type(value) is bool)
        or (data_type == "integer" and type(value) is int)
        or (data_type == "number" and type(value) in (int, float))
    )


def validate_catalog(document: CatalogDocument) -> None:
    if len(document.model_dump_json().encode()) > 128_000:
        raise CompilerFailure("CATALOG_SIZE_LIMIT")
    terms = [
        *document.entities,
        *document.fields,
        *document.metrics,
        *document.relations,
        *document.time_windows,
        *(m for f in document.fields for m in f.members),
    ]
    if len({term.id.casefold() for term in terms}) != len(terms):
        raise CompilerFailure("CATALOG_DUPLICATE_ID")
    if any(
        not text.strip() or len(text) > 200
        for term in terms
        for text in (term.label, *term.synonyms)
    ):
        raise CompilerFailure("CATALOG_TERM_INVALID")
    entities = {e.id for e in document.entities}
    fields = {f.id: f for f in document.fields}
    for field in document.fields:
        if field.entity_id not in entities or any(
            not value_matches(m.value, field.data_type) for m in field.members
        ):
            raise CompilerFailure("CATALOG_FIELD_INVALID")
    for metric in document.metrics:
        metric_field = fields.get(metric.field_id)
        if (
            metric_field is None
            or metric_field.entity_id != metric.entity_id
            or (
                metric.function in ("sum", "avg")
                and metric_field.data_type not in ("number", "integer")
            )
        ):
            raise CompilerFailure("CATALOG_METRIC_INVALID")
    for relation in document.relations:
        left, right = fields.get(relation.from_field), fields.get(relation.to_field)
        if (
            left is None
            or right is None
            or left.entity_id != relation.from_entity
            or right.entity_id != relation.to_entity
            or left.data_type != right.data_type
            or relation.from_entity == relation.to_entity
        ):
            raise CompilerFailure("CATALOG_RELATION_INVALID")
    for window in document.time_windows:
        if window.start >= window.end_exclusive:
            raise CompilerFailure("CATALOG_TIME_INVALID")
    for entity in document.entities:
        for predicate in entity.required_filters:
            policy_field = fields.get(predicate.column)
            if policy_field is None or policy_field.entity_id != entity.id:
                raise CompilerFailure("CATALOG_POLICY_INVALID")


class CatalogRepository(Protocol):
    async def get(self, pin: ResourceVersion) -> CatalogDocument: ...


class PublishedCatalogs:
    """Immutable publication interface for authoring adapters; no default ontology."""

    def __init__(self, documents: tuple[CatalogDocument, ...]) -> None:
        self._documents: dict[tuple[str, str, str, int], tuple[ResourceVersion, str]] = {}
        for document in documents:
            validate_catalog(document)
            pin = pin_for(document)
            key = self._key(pin)
            if key in self._documents:
                raise CompilerFailure("CATALOG_REVISION_CONFLICT")
            self._documents[key] = (pin, document.model_dump_json())

    @staticmethod
    def _key(pin: ResourceVersion) -> tuple[str, str, str, int]:
        return (pin.scope.tenant_id, pin.scope.workspace_id, pin.resource_id, pin.revision)

    async def get(self, pin: ResourceVersion) -> CatalogDocument:
        stored = self._documents.get(self._key(pin))
        if stored is None or stored[0] != pin:
            raise CompilerFailure("RESOURCE_NOT_AVAILABLE")
        return CatalogDocument.model_validate_json(stored[1])


def authorized_view(document: CatalogDocument, access: CatalogAccess) -> CatalogDocument:
    entities = tuple(e for e in document.entities if e.id in access.entity_ids)
    entity_ids = {e.id for e in entities}
    fields = tuple(
        f.model_copy(update={"members": tuple(m for m in f.members if m.id in access.member_ids)})
        for f in document.fields
        if f.id in access.field_ids and f.entity_id in entity_ids
    )
    field_ids = {f.id for f in fields}
    metrics = tuple(
        m
        for m in document.metrics
        if m.id in access.metric_ids and m.field_id in field_ids and m.entity_id in entity_ids
    )
    relations = tuple(
        r
        for r in document.relations
        if r.id in access.relation_ids and r.from_field in field_ids and r.to_field in field_ids
    )
    if not entities or not fields:
        raise CompilerFailure("RESOURCE_NOT_AVAILABLE")
    # Do not drop a hidden policy field and silently widen the query.
    if any(p.column not in field_ids for e in entities for p in e.required_filters):
        raise CompilerFailure("CATALOG_POLICY_UNAVAILABLE")
    if any(
        isinstance(p, MemberPredicate) and not set(p.member_ids) <= access.member_ids
        for e in entities
        for p in e.required_filters
    ):
        raise CompilerFailure("CATALOG_POLICY_UNAVAILABLE")
    return document.model_copy(
        update={
            "entities": entities,
            "fields": fields,
            "metrics": metrics,
            "relations": relations,
        }
    )
