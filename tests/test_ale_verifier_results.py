import json
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk"))
sys.path.insert(0, str(ROOT.parent))

from eval_service import ale_codex_runner, ale_docker_grader, ale_grader, ale_manifest
from eval_service._ale_grader_driver import _normalize_evaluate_result
from eval_service._ale_grader_driver import LocalSession
from eval_service.ale_verifier_capture import (
    build_verifier_payload,
    invoke_with_capture,
    merge_sidecar,
    write_sidecar,
)
from eval_service.ale_verifier_registry import TASK_SPECS, coverage, source_coverage
from simple_agentic_evals import benchmark as benchmark_module
from simple_agentic_evals.benchmark import BenchmarkReport, CohortResult


def test_ale_aggregate_fallback_is_explicit():
    rows = ale_grader.verifier_records(
        {"score": 0.6, "raw_scores": [0.6]}, grading_path="local_evaluate"
    )
    assert rows == [{
        "name": "ale_score",
        "passed": False,
        "score": 0.6,
        "expected": ">= 1.0",
        "actual": 0.6,
        "comparison_type": "ale_score:local_evaluate",
        "description": "",
        "details": {"granularity": "aggregate"},
        "error": None,
    }]


def test_registry_exactly_covers_served_ale_tasks():
    state = coverage(ale_manifest.runnable_ids())
    assert len(TASK_SPECS) == 96
    assert state["complete"] is True
    assert state["n_composite"] == 95
    assert state["n_atomic"] == 1
    assert state["missing"] == []
    assert state["extra"] == []
    assert state["invalid_specs"] == []
    source = source_coverage(ale_grader.ALE_ROOT / "tasks")
    if (ale_grader.ALE_ROOT / "tasks").is_dir():
        assert source == {
            "n_checked": 96, "n_valid": 96, "failures": [], "complete": True,
        }
    else:
        assert source["complete"] is False
        assert source["n_checked"] == 0


def test_public_ale_task_metadata_uses_versioned_steps_and_task_card_evaluation():
    row = SimpleNamespace(
        task_id="transport_safety/capacitated_vehicle_routing_problems",
        raw={
            "source_repo_path": "tasks/transport_safety/capacitated_vehicle_routing_problems",
            "agent_must_do": ["versioned dataset step"],
        },
    )
    metadata = ale_grader.public_task_metadata(row)
    assert metadata["required_steps"] == ["versioned dataset step"]
    if (ale_grader.ALE_ROOT / "tasks").is_dir():
        assert "three required `.sol` files" in metadata["evaluation"]
    else:
        assert metadata["evaluation"] == ""


def test_public_ale_task_metadata_rejects_paths_outside_task_root():
    row = SimpleNamespace(
        task_id="future/task",
        raw={"source_repo_path": "../../../../secret"},
    )
    assert ale_grader.public_task_metadata(row) == {
        "required_steps": [], "evaluation": "",
    }


def test_async_capture_uses_final_return_and_keeps_nested_safe_checks():
    async def evaluator(_cfg, _session):
        report = {
            "score": 0.75,
            "components": {
                "format": {
                    "score": 1.0,
                    "passed": True,
                    "weight": 0.5,
                    "checks": {"schema_passed": True},
                },
                "content": {
                    "score": 0.5,
                    "passed": False,
                    "checks": [{"name": "accuracy", "score": 0.5, "passed": False}],
                    "reference_secret": "do-not-return",
                },
            },
            "reference_token": "sk-super-secret-value",
        }
        await asyncio.sleep(0)
        return [report["score"]]

    raw, payload = asyncio.run(invoke_with_capture(
        evaluator, "business_finance/american_option_pricing_ls", None, None,
    ))
    assert raw == [0.75]
    assert [(row["name"], row["score"], row["passed"])
            for row in payload["per_verifier"]] == [
        ("format", 1.0, True), ("content", 0.5, False),
    ]
    assert payload["per_verifier"][0]["details"]["checks"] == [{
        "name": "schema_passed", "passed": True, "score": 1.0, "details": {},
    }]
    assert "secret" not in json.dumps(payload).lower()


def test_flat_metrics_are_components_and_per_item_checks_are_anonymized():
    payload = build_verifier_payload(
        "legal/agora_governance_classify_instance_1",
        [0.4],
        {"result": {
            "score": 0.4,
            "classification_score": 0.4,
            "gap_analysis_valid": True,
            "cross_document_matrix_valid": False,
            "legislative_mean": 0.5,
            "per_doc": {
                "hidden-document-id": {
                    "missing": True,
                    "scope_score": 0.0,
                    "legislative_truth": "Hidden Law",
                },
            },
        }},
    )
    names = [row["name"] for row in payload["per_verifier"]]
    assert names == [
        "classification_score", "gap_analysis_valid",
        "cross_document_matrix_valid", "legislative_mean",
    ]
    nested = payload["per_verifier"][0]["details"]["checks"]
    assert nested[0]["name"] == "per_doc"
    assert nested[0]["details"]["checks"][0]["name"] == "per_doc_item_1"
    assert nested[0]["details"]["checks"][0]["details"]["checks"][0]["name"] == "missing"
    assert nested[0]["details"]["checks"][0]["details"]["checks"][0]["passed"] is False
    rendered = json.dumps(payload)
    assert "hidden-document-id" not in rendered
    assert "Hidden Law" not in rendered


def test_atomic_and_unmapped_evaluators_fall_back_without_changing_score():
    atomic = build_verifier_payload(
        "health_medicine/wsi_tumor_localization_1", [0.4], {},
    )
    assert atomic["score"] == 0.4
    assert atomic["per_verifier"][0]["name"] == "ale_score"
    assert atomic["per_verifier"][0]["details"]["granularity"] == "atomic"

    unmapped = build_verifier_payload("future/task", [0.25], {})
    assert unmapped["score"] == 0.25
    assert unmapped["per_verifier"][0]["details"]["granularity"] == "aggregate_fallback"


def test_precondition_exit_is_explicit_and_not_a_false_atomic_result():
    payload = build_verifier_payload(
        "business_finance/american_option_pricing_ls", [0.0], {},
    )
    row = payload["per_verifier"][0]
    assert row["name"] == "evaluation_preconditions"
    assert row["details"]["granularity"] == "precondition_failure"
    assert row["details"]["components_reached"] is False
    assert "extraction_error" not in row["details"]


def test_nonzero_missing_capture_remains_a_health_visible_error():
    payload = build_verifier_payload(
        "business_finance/american_option_pricing_ls", [0.5], {},
    )
    row = payload["per_verifier"][0]
    assert row["details"]["granularity"] == "aggregate_fallback"
    assert "were not captured" in row["details"]["extraction_error"]


def test_previously_atomic_tasks_return_their_real_components():
    chisel = build_verifier_payload(
        "engineering/chisel_verilog_alignment_seq_1", [0.9], {"score": 0.9},
    )
    assert [(row["name"], row["score"]) for row in chisel["per_verifier"]] == [
        ("valid_output_schema", 1.0), ("target_signal", 0.0), ("chisel_sources", 1.0),
    ]

    cp = build_verifier_payload(
        "computing_math/cp_test_gen_1", [0.2],
        {"verdicts": {"j": {"AC"}, "i": {"AC"}, "a": {"WA"}}},
    )
    assert cp["per_verifier"][0]["details"]["gate"] is True
    assert sum(
        row["details"].get("weighted_contribution") or 0.0
        for row in cp["per_verifier"]
    ) == 0.2

    moodle = build_verifier_payload(
        "education_info/moodle_gradebook_closeout_reconciliation", [0.2],
        {"payload": {"score": 20, "gates": [{
            "name": "category_weights_drop_empty", "points": 20,
            "max_points": 20, "passed": True,
        }]}},
    )
    assert moodle["per_verifier"][0]["name"] == "category_weights_drop_empty"
    assert moodle["per_verifier"][0]["score"] == 1.0

    materials = build_verifier_payload(
        "physical_sciences/computational_materials_science", [0.0],
        {"failures": ["silicon: missing eqp.dat"]},
    )
    assert [(row["name"], row["passed"]) for row in materials["per_verifier"]] == [
        ("silicon", False), ("silicon-BSE", True), ("MoSe2-BSE", True),
    ]
    assert materials["per_verifier"][0]["details"]["checks"][0]["name"] == "bandstructure.dat"

    mose2 = build_verifier_payload(
        "physical_sciences/mose2_bse_absorption_soc", [0.0],
        {"result": {"score": 0.0, "passed": False,
                    "failures": ["absorption_eh.dat: failed to read agent output"]}},
    )
    assert [(row["name"], row["score"]) for row in mose2["per_verifier"]] == [
        ("absorption_eh.dat", 0.0), ("MoSe2_bands.dat.gnu", None),
        ("MoSe2_bands.png", None), ("exciton_absorption_spectra_avg.png", None),
    ]


def test_ltmle_and_simglucose_capture_the_objects_actually_scored():
    ltmle = build_verifier_payload(
        "health_medicine/ltmle_targeted_bootstrap_simulation_study", [1.0],
        {
            "public_result": {"score": 1.0, "passed": True, "reason": "public_summary_passed"},
            "hidden_payload": {"passed": True, "reason": "hidden_smoke_passed"},
        },
    )
    assert [row["name"] for row in ltmle["per_verifier"]] == [
        "public_summary", "hidden_smoke",
    ]

    simglucose = build_verifier_payload(
        "health_medicine/simglucose_safe_basal_control_instance_1", [0.64],
        {"scored": {
            "mean_tir_70_180": 0.8, "completion_ratio": 1.0,
            "episodes": 4, "catastrophic_episode_count": 0,
        }},
    )
    assert [(row["name"], row["score"]) for row in simglucose["per_verifier"]] == [
        ("time_in_range", 0.8), ("episode_safety", 1.0),
    ]


def test_shared_helper_final_frame_is_captured_without_rerunning_it():
    namespace: dict[str, object] = {}
    source = """\
async def evaluate_single_task(_cfg, _session, _task_dir):
    payload = {"score": 0.0, "passed": False, "reasons": ["verdict_mismatch"]}
    return [payload["score"]]
"""
    helper_path = (
        "/tmp/tasks/psychology_neuro/_shared/cognitive_science/neuro_runtime.py"
    )
    exec(compile(source, helper_path, "exec"), namespace)
    helper = namespace["evaluate_single_task"]

    async def evaluator(cfg, session):
        return await helper(cfg, session, None)

    raw, payload = asyncio.run(invoke_with_capture(
        evaluator, "health_medicine/scene3_skullstrip_qc", None, None,
    ))
    assert raw == [0.0]
    assert [(row["name"], row["passed"]) for row in payload["per_verifier"]] == [
        ("chosen_mask", True), ("verdict", False), ("rationale", True),
    ]


def test_checkpoint_line_capture_returns_every_declared_component():
    namespace: dict[str, object] = {}
    # Clinical Variant Annotation's first official score increment is line 128.
    source = "\n" * 126 + "async def evaluator(_cfg, _session):\n    return [0.2]\n"
    exec(compile(source, "/tmp/clinical/main.py", "exec"), namespace)
    raw, payload = asyncio.run(invoke_with_capture(
        namespace["evaluator"], "health_medicine/Clinical_Variant_Annotation", None, None,
    ))
    assert raw == [0.2]
    assert len(payload["per_verifier"]) == 5
    assert payload["per_verifier"][0]["name"] == "variant_count"
    assert payload["per_verifier"][0]["passed"] is True
    assert all(row["passed"] is False for row in payload["per_verifier"][1:])


def test_dynamic_checkpoint_line_uses_only_the_allowlisted_metric_name():
    namespace: dict[str, object] = {}
    source = (
        "\n" * 268
        + "async def evaluator(_cfg, _session):\n"
        + "    key = 'snp_precision'\n"
        + "    score = 0.0\n"
        + "    score += 0.10\n"
        + "    return [score]\n"
    )
    exec(compile(source, "/tmp/wgs/main.py", "exec"), namespace)
    _, payload = asyncio.run(invoke_with_capture(
        namespace["evaluator"], "life_sciences/WGS_Variant_Calling", None, None,
    ))
    passed = [row["name"] for row in payload["per_verifier"] if row["passed"]]
    assert passed == ["snp_precision"]


def test_ale_runner_tasks_prefix_is_normalized():
    payload = build_verifier_payload(
        "tasks/health_medicine/wsi_tumor_localization_1", [0.2], {},
    )
    assert payload["task_id"] == "health_medicine/wsi_tumor_localization_1"
    assert payload["per_verifier"][0]["details"]["granularity"] == "atomic"


def test_sidecar_requires_matching_task_and_score_and_is_cleaned(tmp_path):
    sidecar = tmp_path / "verifier.json"
    payload = build_verifier_payload("health_medicine/wsi_tumor_localization_1", [0.5], {})
    write_sidecar(payload, sidecar)
    merged = merge_sidecar({"score": 0.5}, sidecar, payload["task_id"])
    assert merged["per_verifier"][0]["score"] == 0.5
    assert not sidecar.exists()

    write_sidecar(payload, sidecar)
    unchanged = merge_sidecar({"score": 0.6}, sidecar, payload["task_id"])
    assert "per_verifier" not in unchanged
    assert not sidecar.exists()


def test_codex_runner_uses_overlay_and_merges_component_sidecar(tmp_path, monkeypatch):
    task_id = "legal/agora_governance_classify_instance_1"
    output = tmp_path / "output"
    output.mkdir()
    task = SimpleNamespace(
        domain="legal", task="agora_governance_classify_instance_1",
        session_root=tmp_path, output_dir=output,
    )
    launched = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, cmd, **kwargs):
            launched["cmd"] = cmd
            launched["env"] = kwargs["env"]

        def communicate(self, timeout=None):
            payload = {
                "schema": 1,
                "registry_version": "test",
                "task_id": task_id,
                "score": 0.5,
                "per_verifier": [{
                    "name": "classification_score", "score": 0.5,
                    "passed": False, "details": {"checks": []},
                }],
            }
            write_sidecar(payload, launched["env"]["EVAL_SERVICE_ALE_VERIFIER_OUTPUT"])
            return "", ""

    monkeypatch.setattr(ale_grader, "venv_python", lambda: Path("/usr/bin/python"))
    monkeypatch.setattr(ale_docker_grader, "image_present", lambda: True)
    direct_config = tmp_path / "codex.yaml"
    direct_config.write_text("model: gpt-5\n", encoding="utf-8")
    monkeypatch.setattr(ale_codex_runner, "_DIRECT_CONFIG", direct_config)
    monkeypatch.setattr(ale_grader, "list_containers", lambda _prefix: set())
    monkeypatch.setattr(ale_grader, "reap_containers", lambda *_a, **_k: [])
    monkeypatch.setattr(ale_codex_runner.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        ale_docker_grader, "_find_unit_grade",
        lambda *_a, **_k: {"score": 0.5, "raw_scores": [0.5]},
    )
    monkeypatch.setattr(ale_docker_grader, "find_unit_usage", lambda *_a, **_k: {})
    monkeypatch.setattr(ale_docker_grader, "find_unit_spawns", lambda *_a, **_k: None)

    result = ale_codex_runner.run_codex_ale(task, timeout_s=5)
    assert result["ok"] is True
    assert result["score"] == 0.5
    assert result["per_verifier"][0]["name"] == "classification_score"
    assert Path(launched["cmd"][1]) == ale_docker_grader._ALE_OVERLAY_LAUNCHER
    assert launched["env"]["EVAL_SERVICE_ALE_ROOT"] == str(ale_grader.ALE_ROOT)
    assert not (tmp_path / "_codex_run" / "verifier_result.json").exists()


def test_ale_raw_components_and_names_are_preserved():
    rows = ale_grader.verifier_records(
        {
            "score": 0.5,
            "raw_scores": [1.0, 0.0],
            "verifier_names": ["format", "content"],
        },
        grading_path="docker_evaluate",
    )
    assert [(row["name"], row["score"], row["passed"]) for row in rows] == [
        ("format", 1.0, True), ("content", 0.0, False),
    ]
    assert all(row["details"]["granularity"] == "component" for row in rows)


def test_ale_named_verifier_payload_keeps_description_and_details():
    rows = ale_grader.verifier_records(
        {
            "score": 0.75,
            "per_verifier": [{
                "name": "numerical_accuracy",
                "score": 0.75,
                "description": "Matches the hidden reference within tolerance.",
                "details": {"max_error": 0.02},
            }],
        },
        grading_path="codex_inline",
    )
    assert rows[0]["name"] == "numerical_accuracy"
    assert rows[0]["score"] == 0.75
    assert rows[0]["description"].startswith("Matches")
    assert rows[0]["details"] == {"max_error": 0.02}


def test_local_driver_preserves_rich_and_list_evaluator_results():
    assert _normalize_evaluate_result([0.25, 1.0]) == {
        "score": 0.25, "raw_scores": [0.25, 1.0],
    }
    rich = _normalize_evaluate_result({
        "score": 0.5,
        "per_verifier": [{"name": "a", "score": 0.5}],
    })
    assert rich["raw_scores"] == [0.5]
    assert rich["per_verifier"][0]["name"] == "a"


def test_local_session_list_file_matches_real_provider(tmp_path):
    target = tmp_path / "manifest.json"
    target.write_text("{}")
    try:
        asyncio.run(LocalSession().list_dir(str(target)))
    except NotADirectoryError:
        pass
    else:
        raise AssertionError("listing a file must not make it look like an empty directory")


def test_cvrp_returns_one_component_per_visible_instance():
    payload = build_verifier_payload(
        "transport_safety/capacitated_vehicle_routing_problems", [0.33],
        {"result": {
            "score": 0.33,
            "per_instance": [
                {"name": "M-n101-k10", "exists": True, "feasible": True,
                 "gap": 0.01, "passed": True},
                {"name": "X-n101-k25", "exists": False, "feasible": False,
                 "gap": None, "passed": False},
                {"name": "X-n106-k14", "exists": False, "feasible": False,
                 "gap": None, "passed": False},
            ],
        }},
    )
    assert [(row["name"], row["passed"]) for row in payload["per_verifier"]] == [
        ("M-n101-k10", True), ("X-n101-k25", False), ("X-n106-k14", False),
    ]


def test_sandbox_eval_result_reader_preserves_verifier_payload(tmp_path):
    run_dir = tmp_path / "agent" / "model" / "domain__task" / "v0" / "run"
    run_dir.mkdir(parents=True)
    payload = {
        "score": 0.5,
        "raw_scores": [1.0, 0.0],
        "verifier_names": ["first", "second"],
        "per_verifier": [{"name": "first", "score": 1.0}],
    }
    (run_dir / "eval_result.json").write_text(json.dumps(payload))
    assert ale_docker_grader._find_unit_grade(tmp_path, "domain/task") == payload
    assert ale_docker_grader._find_unit_score(tmp_path, "domain/task") == 0.5


class _Task:
    task_id = "domain/task"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def grade(self, keep_alive=False):
        assert keep_alive is True
        return SimpleNamespace(
            pass_rate=0.5,
            overall_success=False,
            per_verifier=[{"name": "content", "score": 0.5, "passed": False}],
        )


def test_benchmark_report_returns_task_and_verifier_rows():
    score, solved, cost = benchmark_module._score_task(
        _Task(), lambda _task: {"total_tokens": 7}, {}
    )
    assert score == 0.5 and solved is False
    assert cost["per_verifier"][0]["name"] == "content"

    cohort = CohortResult(per_task=[{
        "task_id": "domain/task", "score": score,
        "per_verifier": cost["per_verifier"],
    }], domain=None, stage=-1, at_stage="full")
    report = BenchmarkReport(
        mode="deployment", dataset="evovling_tools", benchmark="ale",
        resource_mode="accumulative", domains=[None], last_row=[cohort],
    )
    assert report.task_results[0]["task_id"] == "domain/task"
    assert report.verifier_results == [{
        "task_id": "domain/task", "name": "content",
        "score": 0.5, "passed": False,
    }]
