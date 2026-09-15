"""Versioned, service-owned allowlist for ALE verifier result extraction.

The reference ALE checkout is intentionally treated as read-only.  Each task
listed here names only evaluator-local report objects that may be inspected
after the official evaluator returns.  The extractor applies a second,
field-level scoring vocabulary allowlist before anything reaches the wire.

Tasks without an inspectable report object are deliberately marked atomic:
their one authoritative measurement is the official ALE score.  This is more
honest than manufacturing rubric measurements the evaluator never recorded.
"""
from __future__ import annotations

import ast
from pathlib import Path

REGISTRY_VERSION = "2026-09-12.3"


def _lines(value: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in value.splitlines() if line.strip())


# The names below are evaluator-frame locals, not arbitrary object discovery.
# Grouping keeps the 96-task coverage auditable while every task still receives
# an explicit extraction policy.
_REPORT = _lines("""
business_finance/american_option_pricing_ls
business_finance/basel_operational_risk_bia_cn
business_finance/financial_stmt_reconstruction_aapl_fy2024
business_finance/llm_ecosystem_privacy_audit_realdata_1
computing_math/branch_bound_atsp
computing_math/k3_abelian_extensions
computing_math/particle_filter_nonlinear_tracking
computing_math/synthetic_causal_structure_inference
education_info/homework_grading_numerical_pdes_instance_02
education_info/marc_remediation_folio_overlay
engineering/aerospace_low_thrust_trajectory
health_medicine/healthcare_tcga_luad_survival_kras
life_sciences/gene_expression_differential_analysis_functional_enrichment_analysis_1
life_sciences/genomic_interval_processing_1
life_sciences/hg002_chr22_germline_variant_pipeline
life_sciences/protein_function_annotation_instance_1
life_sciences/pseudotime_de
life_sciences/tcga_brca_deg_analysis
social_sciences/atwood_2022_measles_vaccine_reproduction
""")

_RESULT = _lines("""
business_finance/bpmn_category_governance_restructuring_l3
business_finance/bpmn_supply_disruption_l3
business_finance/digital_marketing_audience_segmentation_1
business_finance/ff5_public_reconstruction
business_finance/legal_ma_consistency_audit_01
business_finance/pe_screening_memo_1
business_finance/sse_northbound_programmatic_trading_01
computing_math/cfr_game_theory_equilibrium
computing_math/clustered_cyclic_code_circuit_level_simulation
computing_math/cost_optimization_1
computing_math/data_pipeline_etl_instance_1
computing_math/ising_post_measurement_1
computing_math/k8s_payment_api_root_cause_analysis
computing_math/paper_reproduction_instance_1
computing_math/recsys_cold_start_instance_1
engineering/abb_irb6700_asset_to_urdf_instance_1
engineering/humanoid_wbc_policy_evaluation
health_medicine/causal_ihdp_ite_estimation_6a_v1
health_medicine/crf_sdtm_mapping_1
health_medicine/crf_sdtm_mapping_4
health_medicine/epidemiology_forecast
health_medicine/flusight_offline_hosp_forecast_2024_12_14
health_medicine/healthcare_bias_audit_27a_public_replication_v1
health_medicine/healthcare_sap_group_sequential_nsclc
health_medicine/healthcare_variant_annotation_pipeline
health_medicine/nhanes_confounder_sensitivity_analysis
health_medicine/obermeyer_bias_reproduction
health_medicine/public_health_mask_mandate_ratio
life_sciences/amber_minimization_script_prep_instance_1
life_sciences/amber_three_stage_mmgbsa_workflow_instance_1
life_sciences/cell_tracking_instance_1
life_sciences/cell_translocation_analysis
life_sciences/rgi_mcr1_colistin_v2
life_sciences/tms_marrow_cell_type_annotation_instance_1
life_sciences/tp53_locus_variant_histone_browser_svg
life_sciences/yeast_colony_detection
life_sciences/zdock_hiv_dimer_interface_scoring_v1
legal/agora_governance_classify_instance_1
legal/legal_dr_fees_01
physical_sciences/exact_diag_heisenberg_j1j2
physical_sciences/molecular_structure_plausibility
physical_sciences/mose2_bse_absorption_soc
physical_sciences/phonon_dispersion_thermodynamics
physical_sciences/silicon_bse_absorption
transport_safety/abm_hangzhou_metro
transport_safety/capacitated_vehicle_routing_problems
""")

_PAYLOAD = _lines("""
business_finance/digital_marketing_ab_test_analysis_1
business_finance/internal_employee_agent_instance_1
business_finance/sec_10k_financial_parsing
computing_math/k8s_migration_1
computing_math/ranking_node_feature_parity_recovery_instance_1
engineering/openroad_sky130_ibex_pnr_signoff
engineering/power_10kv_feeder_reliability_001
health_medicine/ltmle_targeted_bootstrap_simulation_study
health_medicine/prostate_imrt_matrad_reproduction
health_medicine/replicate_paper_1
health_medicine/simglucose_safe_basal_control_instance_1
life_sciences/idp_ensemble_scoring
physical_sciences/adapt_vqe_molecular_energy
physical_sciences/climate_prediction
physical_sciences/gillespie_gene_regulatory_network
physical_sciences/glm_lake_calibration
physical_sciences/hst_acs_wfc_visit_reduction
psychology_neuro/celegans_neuron_tracking
transport_safety/fds_single_compartment_detector_reconstruction
""")

_DATA = _lines("""
engineering/mpc_control_building_v1
health_medicine/nsclc_radiomics_cox_signature_v1
""")

# This task selects one metric per variant and its evaluator returns that
# metric directly.  It is the only genuinely scalar-only task in the current
# 96-task manifest.
_ATOMIC = _lines("""
health_medicine/wsi_tumor_localization_1
""")

TASK_SPECS: dict[str, dict[str, object]] = {}
for _task in _REPORT:
    TASK_SPECS[_task] = {"roots": ("report",)}
for _task in _RESULT:
    TASK_SPECS[_task] = {"roots": ("result",)}
for _task in _PAYLOAD:
    # A few verifier subprocesses call their decoded JSON ``report`` or
    # ``data``; those names are also explicitly allowed for this task group.
    TASK_SPECS[_task] = {"roots": ("payload", "report", "data")}
for _task in _DATA:
    TASK_SPECS[_task] = {"roots": ("data",)}
for _task in _ATOMIC:
    TASK_SPECS[_task] = {
        "roots": (),
        "atomic": True,
        "atomic_reason": "one task-selected scalar metric is the complete official measurement",
    }


# Evaluators in the original payload bucket do not use one common local name.
# Keep the exact final-frame root for every task so source drift is detectable.
_EXACT_ROOTS: dict[str, tuple[str, ...]] = {
    "business_finance/digital_marketing_ab_test_analysis_1": ("payload",),
    "business_finance/internal_employee_agent_instance_1": ("payload",),
    "business_finance/sec_10k_financial_parsing": ("payload",),
    "computing_math/k8s_migration_1": ("payload",),
    "computing_math/ranking_node_feature_parity_recovery_instance_1": ("payload",),
    "engineering/openroad_sky130_ibex_pnr_signoff": ("payload",),
    "engineering/power_10kv_feeder_reliability_001": ("payload",),
    "health_medicine/ltmle_targeted_bootstrap_simulation_study": (
        "public_result", "hidden_payload",
    ),
    "health_medicine/prostate_imrt_matrad_reproduction": ("payload",),
    "health_medicine/replicate_paper_1": ("payload",),
    "health_medicine/simglucose_safe_basal_control_instance_1": ("scored",),
    "life_sciences/idp_ensemble_scoring": ("payload",),
    "physical_sciences/adapt_vqe_molecular_energy": ("payload",),
    "physical_sciences/climate_prediction": ("data",),
    "physical_sciences/gillespie_gene_regulatory_network": ("payload",),
    "physical_sciences/glm_lake_calibration": ("payload",),
    "physical_sciences/hst_acs_wfc_visit_reduction": ("payload",),
    "psychology_neuro/celegans_neuron_tracking": ("payload",),
    "transport_safety/fds_single_compartment_detector_reconstruction": ("report",),
}
for _task, _roots in _EXACT_ROOTS.items():
    TASK_SPECS[_task] = {"roots": _roots}


def _check(name: str, weight: float, line: int, **extra: object) -> dict[str, object]:
    return {"name": name, "weight": weight, "line": line, **extra}


# Policies for evaluators whose aggregate was previously (and incorrectly)
# labelled atomic.  ``trace_checks`` records only whether an explicitly
# allowlisted score-increment line ran; it never serializes evaluator locals.
TASK_SPECS.update({
    "health_medicine/ltmle_targeted_bootstrap_simulation_study": {
        "roots": ("public_result", "hidden_payload"),
        "extractor": "ltmle",
        "expected": ("public_summary", "hidden_smoke"),
    },
    "engineering/chisel_verilog_alignment_seq_1": {
        "roots": ("score",),
        "extractor": "chisel_alignment",
        "expected": ("valid_output_schema", "target_signal", "chisel_sources"),
    },
    "computing_math/cp_test_gen_1": {
        "roots": ("verdicts",),
        "extractor": "cp_test_gen",
        "expected": (
            "gate_j_ac", "submission_i", "submission_a", "submission_c",
            "submission_e", "submission_f", "submission_h", "submission_b",
            "submission_d", "submission_g_wa", "submission_g_ac",
        ),
    },
    "computing_math/os_log_permission_guard_v1": {
        "roots": ("report",),
        "extractor": "os_log_permissions",
        "expected": (
            "reference_rule_consistency", "output_manifest", "path_set",
            "ownership", "group", "permission_modes", "workspace_files",
            "content_integrity",
        ),
    },
    "education_info/moodle_gradebook_closeout_reconciliation": {
        "roots": ("payload",),
        "expected_container": "gates",
    },
    "health_medicine/Clinical_Variant_Annotation": {
        "roots": (),
        "extractor": "trace_checks",
        "trace_checks": (
            _check("variant_count", 0.2, 128),
            _check("gnomad_annotation", 0.2, 149),
            _check("vep_annotation", 0.2, 164),
            _check("clinvar_annotation", 0.2, 179),
            _check("final_candidate", 0.2, 202),
        ),
        "expected": (
            "variant_count", "gnomad_annotation", "vep_annotation",
            "clinvar_annotation", "final_candidate",
        ),
    },
    "health_medicine/scene3_skullstrip_qc": {
        "roots": (),
        "capture_frames": ({
            "function": "evaluate_single_task",
            "file_suffix": "tasks/psychology_neuro/_shared/cognitive_science/neuro_runtime.py",
            "roots": ("payload",),
        },),
        "extractor": "scene3_skullstrip",
        "expected": ("chosen_mask", "verdict", "rationale"),
    },
    "health_medicine/simglucose_safe_basal_control_instance_1": {
        "roots": ("scored",),
        "extractor": "simglucose",
        "expected": ("time_in_range", "episode_safety"),
    },
    "life_sciences/WGS_Variant_Calling": {
        "roots": (),
        "extractor": "trace_checks",
        "trace_checks": (
            _check("qc_artifacts", 0.1, 219),
            _check("mapping_rate", 0.1, 230),
            _check("duplication_rate", 0.1, 241),
            _check("vcf_and_index", 0.1, 252),
            _check("snp_f1", 0.1, 272, name_from="key"),
            _check("snp_precision", 0.1, 272, name_from="key"),
            _check("snp_recall", 0.1, 272, name_from="key"),
            _check("indel_f1", 0.1, 272, name_from="key"),
            _check("indel_precision", 0.1, 272, name_from="key"),
            _check("indel_recall", 0.1, 272, name_from="key"),
        ),
        "expected": (
            "qc_artifacts", "mapping_rate", "duplication_rate", "vcf_and_index",
            "snp_f1", "snp_precision", "snp_recall", "indel_f1",
            "indel_precision", "indel_recall",
        ),
    },
    "physical_sciences/computational_materials_science": {
        "roots": ("failures",),
        "extractor": "computational_materials",
        "expected": ("silicon", "silicon-BSE", "MoSe2-BSE"),
        "subcase_files": {
            "silicon": ("bandstructure.dat", "eqp.dat", "bandstructure_inteqp.png"),
            "silicon-BSE": (
                "absorption_eh.dat", "absorption_noeh.dat", "bandstructure.dat",
                "eigenvalues.dat", "eigenvalues_noeh.dat", "eqp.dat", "eqp_q.dat",
                "absorption.png", "bandstructure_inteqp.png",
            ),
            "MoSe2-BSE": (
                "absorption_eh.dat", "MoSe2_bands.dat.gnu", "MoSe2_bands.png",
                "exciton_absorption_spectra_avg.png",
            ),
        },
    },
    "physical_sciences/mose2_bse_absorption_soc": {
        "roots": ("result",),
        "extractor": "material_files",
        "expected": (
            "absorption_eh.dat", "MoSe2_bands.dat.gnu", "MoSe2_bands.png",
            "exciton_absorption_spectra_avg.png",
        ),
    },
    "physical_sciences/silicon_bse_absorption": {
        "roots": ("result",),
        "extractor": "material_files",
        "expected": (
            "absorption_eh.dat", "absorption_noeh.dat", "bandstructure.dat",
            "eigenvalues.dat", "eigenvalues_noeh.dat", "eqp.dat", "eqp_q.dat",
            "absorption.png", "bandstructure_inteqp.png",
        ),
    },
    "psychology_neuro/scene2_resample": {
        "roots": (),
        "capture_frames": ({
            "function": "evaluate_single_task",
            "file_suffix": "tasks/psychology_neuro/_shared/cognitive_science/neuro_runtime.py",
            "roots": ("payload",),
        },),
        "extractor": "scene2_resample",
        "expected": (
            "required_artifacts", "settings_image", "nifti_readable",
            "mask_geometry", "nonempty_mask", "derived_statistics", "reported_csv",
        ),
    },
    "transport_safety/capacitated_vehicle_routing_problems": {
        "roots": ("result",),
        "extractor": "cvrp_instances",
        "expected": ("M-n101-k10", "X-n101-k25", "X-n106-k14"),
    },
})


# Stable scorer containers found in the evaluator-owned report schemas.  These
# are asserted at runtime; losing one is extraction drift, not an atomic score.
_EXPECTED_CONTAINERS = {
    "business_finance/basel_operational_risk_bia_cn": "details",
    "business_finance/bpmn_category_governance_restructuring_l3": "checks",
    "business_finance/bpmn_supply_disruption_l3": "checks",
    "business_finance/digital_marketing_audience_segmentation_1": "details",
    "business_finance/financial_stmt_reconstruction_aapl_fy2024": "details",
    "business_finance/sec_10k_financial_parsing": "component_scores",
    "computing_math/data_pipeline_etl_instance_1": "criteria",
    "computing_math/k8s_migration_1": "breakdown",
    "computing_math/recsys_cold_start_instance_1": "criteria",
    "education_info/moodle_gradebook_closeout_reconciliation": "gates",
    "engineering/openroad_sky130_ibex_pnr_signoff": "gates",
    "health_medicine/healthcare_sap_group_sequential_nsclc": "component_scores",
    "health_medicine/healthcare_tcga_luad_survival_kras": "details",
    "health_medicine/replicate_paper_1": "details",
    "legal/legal_dr_fees_01": "details",
    "life_sciences/genomic_interval_processing_1": "details",
    "life_sciences/tp53_locus_variant_histone_browser_svg": "checks",
    "physical_sciences/gillespie_gene_regulatory_network": "components",
    "physical_sciences/glm_lake_calibration": "checks",
    "physical_sciences/molecular_structure_plausibility": "details",
    "social_sciences/atwood_2022_measles_vaccine_reproduction": "details",
}
for _task, _container in _EXPECTED_CONTAINERS.items():
    TASK_SPECS[_task]["expected_container"] = _container


def normalize_task_id(task_id: str) -> str:
    normalized = task_id.strip().strip("/")
    return normalized[6:] if normalized.startswith("tasks/") else normalized


def task_spec(task_id: str) -> dict[str, object] | None:
    """Return the immutable-by-convention extraction policy for ``task_id``."""
    return TASK_SPECS.get(normalize_task_id(task_id))


def coverage(task_ids: set[str] | frozenset[str]) -> dict[str, object]:
    """Registry diagnostics used by health checks and tests."""
    expected = set(task_ids)
    actual = set(TASK_SPECS)
    atomic = {task for task, spec in TASK_SPECS.items() if spec.get("atomic") is True}
    invalid = sorted(
        task for task, spec in TASK_SPECS.items()
        if not spec.get("atomic")
        and not spec.get("roots")
        and not spec.get("capture_frames")
        and not spec.get("trace_checks")
    )
    return {
        "version": REGISTRY_VERSION,
        "n_registry": len(actual),
        "n_expected": len(expected),
        "n_mapped": len(actual & expected),
        "n_composite": len((actual & expected) - atomic),
        "n_atomic": len(actual & expected & atomic),
        "missing": sorted(expected - actual),
        "extra": sorted(actual - expected),
        "invalid_specs": invalid,
        "complete": actual == expected and not invalid,
    }


def source_coverage(task_root: str | Path) -> dict[str, object]:
    """Validate every capture policy against the checked-out evaluator source.

    This catches renamed locals, moved checkpoint lines, and missing shared
    helper functions before a request is graded.  It intentionally performs no
    imports and never executes reference code.
    """
    task_root = Path(task_root).resolve()
    failures: list[str] = []
    checked = 0

    def functions(path: Path) -> dict[str, ast.AST]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    for task_id, spec in sorted(TASK_SPECS.items()):
        main_path = task_root / task_id / "main.py"
        try:
            main_functions = functions(main_path)
            evaluate = main_functions["evaluate"]
        except Exception as exc:  # noqa: BLE001 - report all registry drift
            failures.append(f"{task_id}: cannot inspect evaluate ({type(exc).__name__}: {exc})")
            continue

        assigned = {
            node.id for node in ast.walk(evaluate)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        missing_roots = sorted(set(spec.get("roots", ())) - assigned)
        if missing_roots:
            failures.append(f"{task_id}: evaluate missing roots {missing_roots}")

        score_increment_lines = {
            int(node.lineno) for node in ast.walk(evaluate)
            if isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "score"
        }
        for check in spec.get("trace_checks", ()):
            line = int(check.get("line", -1))
            if line not in score_increment_lines:
                failures.append(
                    f"{task_id}: trace line {line} for {check.get('name')} "
                    "is not an official score increment"
                )

        for rule in spec.get("capture_frames", ()):
            suffix = str(rule.get("file_suffix", "")).replace("\\", "/")
            relative = suffix[6:] if suffix.startswith("tasks/") else suffix
            helper_path = task_root / relative
            try:
                helper_functions = functions(helper_path)
                helper = helper_functions[str(rule.get("function"))]
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    f"{task_id}: cannot inspect helper {suffix}:{rule.get('function')} "
                    f"({type(exc).__name__}: {exc})"
                )
                continue
            helper_assigned = {
                node.id for node in ast.walk(helper)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
            }
            missing_helper = sorted(set(rule.get("roots", ())) - helper_assigned)
            if missing_helper:
                failures.append(f"{task_id}: helper missing roots {missing_helper}")
        checked += 1

    return {
        "n_checked": checked,
        "n_valid": checked - len({failure.split(":", 1)[0] for failure in failures}),
        "failures": failures,
        "complete": checked == len(TASK_SPECS) and not failures,
    }
