from __future__ import annotations

from conftest import request_for, wait_for_terminal


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
    assert detail.compile_artifact is not None
    assert detail.physical_plan is not None
    assert detail.result is not None
    assert detail.lineage is not None
    assert detail.result.manifest.row_count == 4
    assert detail.result.manifest.storage == "inline"
    assert [row["region"] for row in detail.result.rows] == [
        "北辰区",
        "西岭区",
        "南港区",
        "东湖区",
    ]
    assert [row["profit"] for row in detail.result.rows] == [
        2334.0,
        2095.5,
        1863.0,
        1059.0,
    ]
    assert any(node.kind == "source" for node in detail.lineage.nodes)
    assert any(edge.relation == "produces" for edge in detail.lineage.edges)
