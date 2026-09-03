from __future__ import annotations

from datetime import datetime

import pytest

from semantic_api.initializer import DeterministicInitializer
from semantic_api.models import (
    CompilationMode,
    CompileRequest,
    InitializeRequest,
    SemanticContext,
)
from semantic_api.ontology import OntologyRegistry
from semantic_api.provider import StaticFixtureProvider, StructuredCompileContext, UntrustedQuestion


@pytest.fixture
def registry() -> OntologyRegistry:
    return OntologyRegistry.load_default()


@pytest.fixture
def compile_request() -> CompileRequest:
    return CompileRequest(
        question="Show regional quarterly profit for 上季度",
        evaluation_clock=datetime.fromisoformat("2026-08-15T09:00:00+08:00"),
        evaluation_timezone="Asia/Shanghai",
    )


@pytest.fixture
def semantic_context(
    registry: OntologyRegistry, compile_request: CompileRequest
) -> SemanticContext:
    initializer = DeterministicInitializer(registry)
    initialized = initializer.initialize(
        InitializeRequest.model_validate(compile_request.model_dump()), "test-correlation"
    )
    return initialized.selected_semantic_context


@pytest.fixture
def valid_candidate() -> dict[str, object]:
    return StaticFixtureProvider._quarterly_profit([])


@pytest.fixture
def compile_context(
    compile_request: CompileRequest, semantic_context: SemanticContext
) -> StructuredCompileContext:
    return StructuredCompileContext(
        question=UntrustedQuestion(value=compile_request.question),
        compilation_mode=CompilationMode.REGIONAL_QUARTERLY_PROFIT,
        resolved_terms=[],
        time_windows=[],
        semantic_context=semantic_context,
    )
