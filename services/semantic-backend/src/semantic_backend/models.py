from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from semantic_api.models import SQG, CompilationMode

_RUN_ID = re.compile(r"^run_[0-9a-f]{32}$")


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
        allow_inf_nan=False,
    )


class RunState(StrEnum):
    QUEUED = "Queued"
    STARTING = "Starting"
    RUNNING = "Running"
    CANCELLED = "Cancelled"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"

    @property
    def terminal(self) -> bool:
        return self in {self.CANCELLED, self.SUCCEEDED, self.FAILED}


class ExecutionOptions(ApiModel):
    max_rows: int = Field(default=1_000, ge=1, le=10_000)
    max_bytes: int = Field(default=4 * 1024 * 1024, ge=1_024, le=32 * 1024 * 1024)
    timeout_seconds: float = Field(default=30.0, gt=0, le=60)
    output_mode: Literal["preview"] = "preview"


class StartRunRequest(ApiModel):
    run_id: str
    question: str = Field(min_length=1, max_length=1_000)
    requested_by: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=128)
    evaluation_clock: datetime
    evaluation_timezone: str = Field(default="UTC", min_length=1, max_length=100)
    compilation_mode: CompilationMode = CompilationMode.REGIONAL_QUARTERLY_PROFIT
    execution_options: ExecutionOptions = Field(default_factory=ExecutionOptions)
    workload: str | None = Field(default=None, max_length=128)

    @field_validator("run_id")
    @classmethod
    def canonical_run_id(cls, value: str) -> str:
        if not _RUN_ID.fullmatch(value):
            raise ValueError("run_id must use the canonical run_<32 lowercase hex> form")
        return value

    @field_validator("question", "requested_by", "trace_id", "evaluation_timezone")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        return value

    @field_validator("evaluation_clock")
    @classmethod
    def aware_clock(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluation_clock must include an explicit UTC offset")
        return value


class TokenUsage(ApiModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class DiagnosticSummary(ApiModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=512)
    stage: str | None = Field(default=None, max_length=64)
    occurred_at: datetime


class NodeSummary(ApiModel):
    node_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=64)
    state: RunState
    started_at: datetime | None = None
    completed_at: datetime | None = None


class StageSummary(ApiModel):
    stage_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    state: RunState
    started_at: datetime | None = None
    completed_at: datetime | None = None
    nodes: list[NodeSummary] = Field(default_factory=list, max_length=1_000)


class RunStatus(ApiModel):
    run_id: str
    state: RunState
    started_at: datetime | None = None
    finalized_at: datetime | None = None
    stages: list[StageSummary] = Field(default_factory=list, max_length=100)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    diagnostics: list[DiagnosticSummary] = Field(default_factory=list, max_length=100)


class CompileArtifact(ApiModel):
    status: Literal["succeeded"] = "succeeded"
    normalized_sqg: SQG
    resolved_terms: list[dict[str, Any]]
    timing_ms: dict[str, float]


class PhysicalNodeSummary(ApiModel):
    node_id: str
    operation: str
    kind: str
    dependencies: list[str]
    logical_node_ids: list[str]
    source_alias: str | None = None


class PhysicalPlanSummary(ApiModel):
    plan_id: str
    output_node_id: str
    nodes: list[PhysicalNodeSummary]


JsonScalar = str | int | float | bool | None


class ResultColumn(ApiModel):
    name: str
    data_type: str
    nullable: bool


class ResultManifestSummary(ApiModel):
    result_id: str
    storage: str
    row_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    committed_at: datetime


class ResultDetail(ApiModel):
    columns: list[ResultColumn]
    rows: list[dict[str, JsonScalar]]
    manifest: ResultManifestSummary


class LineageNodeDetail(ApiModel):
    id: str
    kind: str
    operation: str | None = None
    source_alias: str | None = None
    source_type: str | None = None
    result_id: str | None = None
    parameter_metadata: list[dict[str, str]] = Field(default_factory=list)


class LineageEdgeDetail(ApiModel):
    source: str
    target: str
    relation: str


class LineageDetail(ApiModel):
    nodes: list[LineageNodeDetail]
    edges: list[LineageEdgeDetail]


class RunDetail(ApiModel):
    run_id: str
    question: str
    requested_by: str
    trace_id: str
    evaluation_clock: datetime
    evaluation_timezone: str
    compilation_mode: CompilationMode
    status: RunStatus
    compile_artifact: CompileArtifact | None = None
    physical_plan: PhysicalPlanSummary | None = None
    result: ResultDetail | None = None
    lineage: LineageDetail | None = None
