from __future__ import annotations

import pytest
from conftest import request_for
from semantic_api.compiler import SemanticCompiler
from semantic_api.models import CompilationMode, CompileRequest

from semantic_backend.adapter import AdapterFailure, CompilerRuntimeAdapter


async def test_adapter_rejects_unmapped_concept() -> None:
    request = request_for("run_00000000000000000000000000000006")
    compiler = SemanticCompiler.default()
    response = await compiler.compile(
        CompileRequest(
            question=request.question,
            evaluation_clock=request.evaluation_clock,
            evaluation_timezone=request.evaluation_timezone,
            compilation_mode=request.compilation_mode,
        ),
        request.trace_id,
    )
    assert response.normalized_sqg is not None
    changed = response.normalized_sqg.model_copy(deep=True)
    select = changed.nodes[0]
    changed.nodes[0] = select.model_copy(
        update={
            "parameters": select.parameters.model_copy(
                update={"columns": [*select.parameters.columns, "metric.unmapped"]}
            )
        }
    )
    response = response.model_copy(update={"normalized_sqg": changed})

    with pytest.raises(AdapterFailure, match="No reviewed source mapping"):
        CompilerRuntimeAdapter().adapt(
            response,
            run_id=request.run_id,
            compilation_mode=CompilationMode.REGIONAL_QUARTERLY_PROFIT,
        )


async def test_adapter_rejects_unmapped_member_value() -> None:
    request = request_for("run_00000000000000000000000000000009").model_copy(
        update={"question": "上季度 region.central_north 区域利润是多少?"}
    )
    response = await SemanticCompiler.default().compile(
        CompileRequest(
            question=request.question,
            evaluation_clock=request.evaluation_clock,
            evaluation_timezone=request.evaluation_timezone,
            compilation_mode=request.compilation_mode,
        ),
        request.trace_id,
    )
    assert response.normalized_sqg is not None
    changed = response.normalized_sqg.model_copy(deep=True)
    region_filter = next(
        node
        for node in changed.nodes
        if node.operator.value == "FILTER"
        and node.parameters.predicate.column == "commerce.sales_record.region"
    )
    region_filter.parameters.predicate.value = "region.unknown"
    response = response.model_copy(update={"normalized_sqg": changed})
    with pytest.raises(AdapterFailure, match="No reviewed source value mapping"):
        CompilerRuntimeAdapter().adapt(
            response,
            run_id=request.run_id,
            compilation_mode=CompilationMode.REGIONAL_QUARTERLY_PROFIT,
        )
