from __future__ import annotations

from typing import Literal

from pydantic import Field
from semantic_api.catalog_v1.models import Compilation, Frozen, Id, ResourceVersion

from semantic_backend.models import ResultSet


class CatalogAnswerRequest(Frozen):
    contract_version: Literal["catalog-answer/v1"]
    catalog: ResourceVersion
    revision: int = Field(strict=True, ge=1)
    choice_id: Id


class CatalogQueryResponse(Frozen):
    contract_version: Literal["catalog-query-result/v1"] = "catalog-query-result/v1"
    request_id: Id
    status: Literal["succeeded", "clarification_required", "blocked"]
    compilation: Compilation
    run_id: str | None = None
    result: ResultSet | None = None
    provenance: dict[str, str] = Field(default_factory=dict)
