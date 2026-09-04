from __future__ import annotations

from pathlib import Path

from conftest import request_for, wait_for_terminal
from semantic_api.models import CompilationMode
from semantic_eval.evaluator import evaluate_bundle, load_document

from semantic_backend.eval_adapter import candidate_bundle

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
