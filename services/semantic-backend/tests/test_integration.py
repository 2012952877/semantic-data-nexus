from __future__ import annotations

from conftest import request_for, wait_for_terminal
from semantic_api.models import CompilationMode


async def test_simple_compiler_adapter_runtime_commits_profit_result(service) -> None:
    request = request_for("run_00000000000000000000000000000001")
    started = await service.start(request)
    assert started.run_id == request.run_id

    terminal = await wait_for_terminal(service, request.run_id)
    assert terminal.state.value == "Succeeded"
    assert [stage.name for stage in terminal.stages] == [
        "Initialize",
        "Compile",
        "Optimize",
        "Execute",
        "Generate",
    ]
    assert all(stage.state.value == "Succeeded" for stage in terminal.stages)

    detail = await service.get_detail(request.run_id)
    artifact = await service.get_integrated_artifact(request.run_id)
    assert artifact.compile_response.normalized_sqg is not None
    assert detail.physical_nodes
    assert detail.result is not None
    assert detail.manifest is not None
    assert detail.manifest.row_count == 4
    assert detail.manifest.storage.value == "inline"
    rows = [
        {column.key: value for column, value in zip(detail.result.columns, row, strict=True)}
        for row in detail.result.rows
    ]
    assert [row["region"] for row in rows] == [
        "北辰区",
        "西岭区",
        "南港区",
        "东湖区",
    ]
    assert [row["profit"] for row in rows] == [
        2334.0,
        2095.5,
        1863.0,
        1059.0,
    ]
    assert any(node.kind == "source" for node in detail.lineage.nodes)
    assert any(edge.relation == "produces" for edge in detail.lineage.edges)
    lineage_kinds = {node.id: node.kind.value for node in detail.lineage.nodes}
    reads_from = next(edge for edge in detail.lineage.edges if edge.relation == "reads_from")
    assert lineage_kinds[reads_from.source] == "physical"
    assert lineage_kinds[reads_from.target] == "source"
    depends_on = next(edge for edge in detail.lineage.edges if edge.relation == "depends_on")
    assert depends_on.source == "physical:physical-project_result"
    assert depends_on.target == "physical:physical-sort_profit"


async def test_governed_member_id_maps_to_synthetic_source_value(service) -> None:
    request = request_for("run_00000000000000000000000000000008").model_copy(
        update={"question": "上季度 region.central_north 区域利润是多少?"}
    )
    await service.start(request)
    terminal = await wait_for_terminal(service, request.run_id)
    assert terminal.state.value == "Succeeded"
    detail = await service.get_detail(request.run_id)
    assert detail.result is not None
    rows = [
        {column.key: value for column, value in zip(detail.result.columns, row, strict=True)}
        for row in detail.result.rows
    ]
    assert [row["region"] for row in rows] == ["北辰区"]


async def test_oversized_serialized_detail_fails_before_publication(
    service,
    monkeypatch,
) -> None:
    import semantic_backend.service as service_module

    monkeypatch.setattr(service_module, "MAX_SERIALIZED_DETAIL_BYTES", 1)
    request = request_for("run_00000000000000000000000000000123")
    await service.start(request)
    terminal = await wait_for_terminal(service, request.run_id)
    assert terminal.state.value == "Failed"
    assert terminal.diagnostics[0].code == "DETAIL_SERIALIZATION_LIMIT"
    detail = await service.get_detail(request.run_id)
    assert detail.result is None
    assert detail.manifest is None


async def test_complex_compiler_path_runs_pivot_derive_project(service) -> None:
    request = request_for(
        "run_00000000000000000000000000000002",
        mode=CompilationMode.MONTHLY_REGIONAL_COMPARISON,
    )
    await service.start(request)
    terminal = await wait_for_terminal(service, request.run_id)
    assert terminal.state is terminal.state.SUCCEEDED, [
        diagnostic.model_dump() for diagnostic in terminal.diagnostics
    ]

    detail = await service.get_detail(request.run_id)
    artifact = await service.get_integrated_artifact(request.run_id)
    assert artifact.compile_response.normalized_sqg is not None
    operators = [node.operator.value for node in artifact.compile_response.normalized_sqg.nodes]
    pivot_index = operators.index("PIVOT")
    assert operators[pivot_index : pivot_index + 3] == ["PIVOT", "DERIVE", "PROJECT"]

    operations = [node.kind.value for node in detail.physical_nodes]
    assert operations[-3:] == ["PIVOT", "DERIVE", "PROJECT"]
    execute = next(stage for stage in terminal.stages if stage.name == "Execute")
    assert all(node.state.value == "Succeeded" for node in execute.nodes)
    started = [node.started_at for node in execute.nodes]
    assert all(value is not None for value in started)
    assert len(set(started)) > 1
    assert all(
        node.completed_at is not None
        and node.started_at is not None
        and node.completed_at >= node.started_at
        for node in execute.nodes
    )
    assert detail.result is not None
    assert detail.result.row_count == 4
    assert {column.key for column in detail.result.columns} == {
        "region",
        "profit_current",
        "profit_previous",
        "profit_change",
    }
    for values in detail.result.rows:
        row = {
            column.key: value for column, value in zip(detail.result.columns, values, strict=True)
        }
        assert row["profit_change"] == row["profit_current"] - row["profit_previous"]
