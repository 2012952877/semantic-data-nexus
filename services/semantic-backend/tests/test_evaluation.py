from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from conftest import request_for, wait_for_terminal
from query_runtime.domain import PhysicalPlan
from semantic_api.models import CompilationMode
from semantic_eval.evaluator import evaluate_bundle, load_document

from semantic_backend.eval_adapter import candidate_bundle, candidate_from_run
from semantic_backend.models import (
    DetailDiagnostic,
    DiagnosticScope,
    DiagnosticSeverity,
    LineageDetail,
    LineageEdgeDetail,
)

FIXTURES = Path(__file__).parent / "fixtures"


async def test_actual_integrated_artifacts_pass_supported_m0_evaluation(service) -> None:
    requests = {
        "integrated-regional-quarterly-profit": request_for("run_00000000000000000000000000000071"),
        "integrated-monthly-profit-comparison": request_for(
            "run_00000000000000000000000000000072",
            mode=CompilationMode.MONTHLY_REGIONAL_COMPARISON,
        ),
    }
    artifacts = {}
    for case_id, request in requests.items():
        await service.start(request)
        terminal = await wait_for_terminal(service, request.run_id)
        assert terminal.state.value == "Succeeded"
        artifacts[case_id] = await service.get_integrated_artifact(request.run_id)

    report = evaluate_bundle(
        load_document(FIXTURES / "m0_golden_subset.json"),
        candidate_bundle(artifacts),
    )
    assert report.score == 100, [
        (
            case.case_id,
            case.dimension_scores,
            [
                (difference.path, difference.expected, difference.actual)
                for difference in case.differences
            ],
        )
        for case in report.cases
    ]
    assert report.dimension_scores == {
        "semantic": 100.0,
        "plan": 100.0,
        "execution_result": 100.0,
        "governance": 100.0,
        "observability": 100.0,
    }


async def test_integrated_evaluation_detects_artifact_mutations(service) -> None:
    request = request_for("run_00000000000000000000000000000073")
    await service.start(request)
    terminal = await wait_for_terminal(service, request.run_id)
    assert terminal.state.value == "Succeeded"
    artifact = await service.get_integrated_artifact(request.run_id)
    suite = load_document(FIXTURES / "m0_golden_subset.json")
    golden = {
        "suite_version": "semantic-backend-mutation-v0",
        "weights": suite["weights"],
        "cases": [suite["cases"][0]],
    }
    case_id = "integrated-regional-quarterly-profit"

    plan = artifact.physical_plan
    changed_plan = PhysicalPlan(
        id=plan.id,
        nodes=plan.nodes[:-1],
        output_node_id=plan.nodes[-2].id,
    )
    plan_report = evaluate_bundle(
        golden,
        candidate_bundle({case_id: replace(artifact, physical_plan=changed_plan)}),
    )
    assert plan_report.dimension_scores["plan"] < 100

    output_report = evaluate_bundle(
        golden,
        candidate_bundle(
            {
                case_id: replace(
                    artifact,
                    physical_plan=artifact.physical_plan.model_copy(
                        update={"output_node_id": artifact.physical_plan.nodes[0].id}
                    ),
                )
            }
        ),
    )
    assert output_report.dimension_scores["plan"] < 100

    changed_result_detail = artifact.detail.model_copy(deep=True)
    assert changed_result_detail.result is not None
    changed_result_detail.result.rows[0][-1] = 999999.0
    result_report = evaluate_bundle(
        golden,
        candidate_bundle({case_id: replace(artifact, detail=changed_result_detail)}),
    )
    assert result_report.dimension_scores["execution_result"] < 100

    changed_result_metadata = artifact.detail.model_copy(deep=True)
    assert changed_result_metadata.result is not None
    changed_result_metadata.result.row_count += 1
    changed_result_metadata.result.truncated = True
    result_metadata_report = evaluate_bundle(
        golden,
        candidate_bundle({case_id: replace(artifact, detail=changed_result_metadata)}),
    )
    assert result_metadata_report.dimension_scores["execution_result"] < 100

    changed_lineage_detail = artifact.detail.model_copy(
        update={"lineage": LineageDetail(run_id=request.run_id)},
        deep=True,
    )
    lineage_report = evaluate_bundle(
        golden,
        candidate_bundle({case_id: replace(artifact, detail=changed_lineage_detail)}),
    )
    assert lineage_report.dimension_scores["observability"] < 100

    changed_artifact_detail = artifact.detail.model_copy(deep=True)
    assert changed_artifact_detail.manifest is not None
    changed_artifact_detail.manifest.checksum = "sha256:" + ("0" * 64)
    artifact_report = evaluate_bundle(
        golden,
        candidate_bundle({case_id: replace(artifact, detail=changed_artifact_detail)}),
    )
    assert artifact_report.dimension_scores["observability"] < 100

    changed_edge_detail = artifact.detail.model_copy(deep=True)
    reads_from = next(
        edge for edge in changed_edge_detail.lineage.edges if edge.relation.value == "reads_from"
    )
    changed_edge_detail.lineage.edges[changed_edge_detail.lineage.edges.index(reads_from)] = (
        LineageEdgeDetail(
            source="physical:physical-project_result",
            target=reads_from.target,
            relation=reads_from.relation,
        )
    )
    edge_report = evaluate_bundle(
        golden,
        candidate_bundle({case_id: replace(artifact, detail=changed_edge_detail)}),
    )
    assert edge_report.dimension_scores["observability"] < 100

    changed_node_detail = artifact.detail.model_copy(deep=True)
    physical_node = next(
        node for node in changed_node_detail.lineage.nodes if node.kind.value == "physical"
    )
    physical_node.operation = "MUTATED_OPERATION"
    node_metadata_report = evaluate_bundle(
        golden,
        candidate_bundle({case_id: replace(artifact, detail=changed_node_detail)}),
    )
    assert node_metadata_report.dimension_scores["observability"] < 100

    changed_result_identity = artifact.detail.model_copy(deep=True)
    result_node = next(
        node for node in changed_result_identity.lineage.nodes if node.kind.value == "result"
    )
    result_node.result_id = "corrupted-result-id"
    result_identity_report = evaluate_bundle(
        golden,
        candidate_bundle({case_id: replace(artifact, detail=changed_result_identity)}),
    )
    assert result_identity_report.dimension_scores["observability"] < 100

    provenance_report = evaluate_bundle(
        golden,
        candidate_bundle(
            {
                case_id: replace(
                    artifact,
                    connector_provenance=(
                        {
                            "resolver": "mutated",
                            "run_id": request.run_id,
                        },
                    ),
                )
            }
        ),
    )
    assert provenance_report.dimension_scores["observability"] < 100

    changed_diagnostic_detail = artifact.detail.model_copy(deep=True)
    changed_diagnostic_detail.diagnostics.append(
        DetailDiagnostic(
            sequence=0,
            run_id=request.run_id,
            scope=DiagnosticScope.RUN,
            scope_id=request.run_id,
            code="SYNTHETIC_FAILURE",
            title="Synthetic mutation",
            message="Mutation for evaluator regression coverage.",
            recovery="Remove the synthetic mutation.",
            severity=DiagnosticSeverity.ERROR,
            occurred_at=datetime.now(UTC),
        )
    )
    governance_report = evaluate_bundle(
        golden,
        candidate_bundle({case_id: replace(artifact, detail=changed_diagnostic_detail)}),
    )
    assert governance_report.dimension_scores["governance"] < 100

    info_diagnostic_detail = artifact.detail.model_copy(deep=True)
    info_diagnostic_detail.diagnostics.append(
        DetailDiagnostic(
            sequence=0,
            run_id=request.run_id,
            scope=DiagnosticScope.RUN,
            scope_id=request.run_id,
            code="SYNTHETIC_INFO",
            title="Synthetic informational mutation",
            message="Informational mutation for evaluator regression coverage.",
            recovery="Remove the synthetic mutation.",
            severity=DiagnosticSeverity.INFO,
            occurred_at=datetime.now(UTC),
        )
    )
    diagnostic_report = evaluate_bundle(
        golden,
        candidate_bundle({case_id: replace(artifact, detail=info_diagnostic_detail)}),
    )
    assert diagnostic_report.dimension_scores["governance"] < 100

    type_corrupted = candidate_bundle({case_id: artifact})
    candidate = type_corrupted["cases"][case_id]
    candidate["result"]["truncated"] = 0
    candidate["result"]["row_count"] = 4.0
    candidate["lineage"]["edge_semantics_valid"] = 1
    candidate["lineage"]["result_identity_valid"] = 1
    type_report = evaluate_bundle(golden, type_corrupted)
    assert type_report.dimension_scores["execution_result"] < 100
    assert type_report.dimension_scores["observability"] < 100


async def test_member_candidate_selects_period_filter_for_time_range(service) -> None:
    request = request_for("run_00000000000000000000000000000074").model_copy(
        update={"question": "上季度 region.central_north 区域利润是多少?"}
    )
    await service.start(request)
    terminal = await wait_for_terminal(service, request.run_id)
    assert terminal.state.value == "Succeeded"
    candidate = candidate_from_run(
        "member-time-range",
        await service.get_integrated_artifact(request.run_id),
    )
    assert candidate["time_range"] == {
        "start": "2024-01-01T00:00:00+00:00",
        "end_exclusive": "2024-04-01T00:00:00+00:00",
        "source": "validated_sqg_filter",
    }
    assert candidate["member_normalization"] == {"region.central_north": "北辰区"}
