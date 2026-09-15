import hashlib
import importlib.util
import json
import sys
import ast
import asyncio
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk"))
sys.path.insert(0, str(ROOT.parent))
SKILL_ROOT = ROOT / "server" / "eval_service" / "skills" / "evolve-eval"

from simple_agentic_evals.leaderboard import aggregate_leaderboard
from simple_agentic_evals.local_server import LocalRuntime
from simple_agentic_evals.runtime_adapters import inspect_adapter, resolve_target, serve_local


def _controls(track: str) -> list[dict[str, object]]:
    ids = [
        "allowed_resource_available",
        "forbidden_resource_inaccessible",
        "ambient_bypass_blocked",
        "task_specific_oracle_only",
        "self_evolving_stage_isolation",
        {
            "evovling_tools": "forbidden_tool_invocation_rejected",
            "evovling_skills": "forbidden_skill_path_inaccessible",
            "evovling_agents": "forbidden_agent_spawn_rejected",
        }[track],
    ]
    return [
        {"id": control_id, "passed": True, "evidence": f"fixture::{control_id}"}
        for control_id in ids
    ]


def _bundle(tmp_path: Path) -> tuple[Path, str, str]:
    data = tmp_path / "data"
    for dataset in ("evovling_tools", "evovling_skills", "evovling_agents"):
        root = data / dataset
        (root / "v1").mkdir(parents=True)
        row = {
            "task_id": "fixture-task", "task_prompt": "write fixture output",
            "oracle_tools": ["fixture.write"], "cummulative_tools": ["fixture.write"],
            "oracle_skills": ["write-output"],
            "cummulative_oracle_skills": ["write-output"],
            "oracle_agents": ["fixture"], "cumulative_agents": ["fixture"],
            "software": ["fixture.write"], "version": "v1",
        }
        line = json.dumps(row, sort_keys=True) + "\n"
        (root / "v1" / "train.jsonl").write_text(line)
        (root / "v1" / "test.jsonl").write_text(line)
    skill = data / "evovling_skills" / "_oracle" / "skills" / "write-output"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Write output\n")
    agents = data / "evovling_agents" / "v1"
    (agents / "agents").mkdir()
    (agents / "agents" / "fixture.toml").write_text('name = "fixture"\n')
    (agents / "manifest.json").write_text(json.dumps({"agents": [{
        "name": "fixture", "owned_software": ["fixture.write"]
    }]}))

    plugin = tmp_path / "adapter.py"
    plugin.write_text(
        "from simple_agentic_evals.local_contract import ManagedRuntimeAction,GradeResult,VerifierView\n"
        "class Env:\n"
        " def health(self): return {'ok': True}\n"
        " def create(self,row,context): return {'context':context},ManagedRuntimeAction(runtime='fixture',task_id=row.task_id,resource_enforced=True)\n"
        " def run_agent(self,row,state,request):\n"
        "  grade={'overall_success':True,'pass_rate':1.0,'n_passed':1,'n_total':1,'per_verifier':[{'name':'fixture','passed':True,'score':1.0}]}\n"
        "  return {'completed':True,'_grade':grade}\n"
        " def grade(self,row,state): raise RuntimeError('run first')\n"
        " def teardown(self,row,state): state['closed']=True\n"
        "def create_environment(descriptor): return Env()\n"
    )
    entry_hash = hashlib.sha256(plugin.read_bytes()).hexdigest()
    evidence = tmp_path / "resource-enforcement.json"
    evidence.write_text(json.dumps({
        "format_version": 1,
        "adapter_id": "unseen-fixture-v1",
        "source_commit": "a" * 40,
        "tracks": {
            track: {
                "status": "passed",
                "modes_tested": ["deployment", "self_evolving", "task_specific"],
                "controls": _controls(track),
            }
            for track in ("evovling_tools", "evovling_skills", "evovling_agents")
        },
    }, sort_keys=True))
    evidence_hash = hashlib.sha256(evidence.read_bytes()).hexdigest()
    manifest = tmp_path / "adapter.json"
    manifest.write_text(json.dumps({
        "format_version": 1, "benchmark": "unseen-fixture",
        "adapter_id": "unseen-fixture-v1", "title": "Unseen fixture",
        "source_url": "https://example.invalid/fixture", "source_commit": "a" * 40,
        "dataset_roots": {name: f"data/{name}" for name in (
            "evovling_tools", "evovling_skills", "evovling_agents")},
        "entrypoint": "adapter.py:create_environment", "entrypoint_sha256": entry_hash,
        "runnable": True,
        "capabilities": {"tracks": ["evovling_tools", "evovling_skills", "evovling_agents"],
                         "action_types": ["managed_runtime"],
                         "harnesses": ["command"],
                         "modes": ["deployment", "self_evolving", "task_specific"]},
        "credentials": [], "smoke_task_ids": ["fixture-task"],
        "runtime": {"isolation": "fixture", "network": "disabled", "timeout_sec": 10,
                    "resource_enforcement": True,
                    "enforced_tracks": [
                        "evovling_tools", "evovling_skills", "evovling_agents"
                    ],
                    "enforcement_evidence": {
                        "path": "resource-enforcement.json", "sha256": evidence_hash
                    },
                    "grader_authority": "fixture verifier"},
        "deviations": [],
    }, sort_keys=True))
    return manifest, hashlib.sha256(manifest.read_bytes()).hexdigest(), entry_hash


def test_local_adapter_lifecycle_and_resources(tmp_path):
    manifest, manifest_hash, entry_hash = _bundle(tmp_path)
    inspected = inspect_adapter(manifest)
    assert inspected["manifest_sha256"] == manifest_hash
    runtime = LocalRuntime(manifest, {manifest_hash, entry_hash})
    assert runtime.catalog()["datasets"][0]["benchmarks"][0]["runnable"] is True
    session = runtime.create({
        "dataset": "evovling_tools", "benchmark": "unseen-fixture",
        "version": 1, "split": "test", "resource_mode": "accumulative",
    })
    assert session["task"]["resources"]["names"] == ["fixture.write"]
    result = runtime.run_agent(session["session_id"], {})
    assert result["completed"] is True
    grade = runtime.grade(session["session_id"], keep_alive=False)
    assert grade["pass_rate"] == 1.0


def test_adapter_tampering_and_escape_are_rejected(tmp_path):
    manifest, _manifest_hash, _entry_hash = _bundle(tmp_path)
    plugin = tmp_path / "adapter.py"
    plugin.write_text(plugin.read_text() + "# tampered\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        inspect_adapter(manifest)
    raw = json.loads(manifest.read_text())
    raw["entrypoint"] = "../outside.py:create_environment"
    manifest.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="escapes"):
        inspect_adapter(manifest)


def test_declared_enforcement_without_proof_is_diagnostic(tmp_path):
    manifest, _manifest_hash, _entry_hash = _bundle(tmp_path)
    raw = json.loads(manifest.read_text())
    raw["runtime"].pop("enforcement_evidence")
    manifest.write_text(json.dumps(raw, sort_keys=True))
    inspected = inspect_adapter(manifest)
    assert inspected["comparable_tracks"] == []
    assert inspected["enforcement_evidence"]["status"] == "missing"
    assert "no checksum-bound" in inspected["enforcement_errors"][0]


def test_failed_or_tampered_enforcement_proof_is_rejected(tmp_path):
    manifest, _manifest_hash, _entry_hash = _bundle(tmp_path)
    raw = json.loads(manifest.read_text())
    evidence = tmp_path / raw["runtime"]["enforcement_evidence"]["path"]
    report = json.loads(evidence.read_text())
    report["tracks"]["evovling_tools"]["controls"] = []
    evidence.write_text(json.dumps(report, sort_keys=True))
    with pytest.raises(ValueError, match="evidence checksum mismatch"):
        inspect_adapter(manifest)


def test_incomplete_checksum_valid_proof_keeps_only_failed_track_diagnostic(tmp_path):
    manifest, _manifest_hash, _entry_hash = _bundle(tmp_path)
    raw = json.loads(manifest.read_text())
    evidence = tmp_path / raw["runtime"]["enforcement_evidence"]["path"]
    report = json.loads(evidence.read_text())
    report["tracks"]["evovling_skills"]["controls"] = []
    evidence.write_text(json.dumps(report, sort_keys=True))
    raw["runtime"]["enforcement_evidence"]["sha256"] = hashlib.sha256(
        evidence.read_bytes()
    ).hexdigest()
    manifest.write_text(json.dumps(raw, sort_keys=True))
    inspected = inspect_adapter(manifest)
    assert inspected["comparable_tracks"] == ["evovling_tools", "evovling_agents"]
    assert any("evovling_skills: missing control" in item
               for item in inspected["enforcement_errors"])


def test_enforcement_proof_path_escape_is_rejected(tmp_path):
    manifest, _manifest_hash, _entry_hash = _bundle(tmp_path)
    outside = tmp_path.parent / "outside-enforcement.json"
    outside.write_text("{}")
    raw = json.loads(manifest.read_text())
    raw["runtime"]["enforcement_evidence"] = {
        "path": "../outside-enforcement.json",
        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
    }
    manifest.write_text(json.dumps(raw, sort_keys=True))
    with pytest.raises(ValueError, match="evidence escapes"):
        inspect_adapter(manifest)


def test_dynamic_resolution_prefers_only_healthy_hosted(tmp_path):
    manifest, _manifest_hash, _entry_hash = _bundle(tmp_path)

    class Client:
        def benchmarks(self):
            return {"datasets": [{"dataset": "evovling_tools", "benchmarks": [{
                "benchmark": "unseen-fixture", "runnable": True,
                "health": {"ok": False}, "tracks": ["evovling_tools"],
                "modes": ["deployment"], "harnesses": ["command"],
                "action_types": ["managed_runtime"],
            }]}]}

    resolved = resolve_target("unseen-fixture", runtime_adapter=manifest, client=Client())
    assert resolved["location"] == "local"
    assert "health" in resolved["hosted_rejections"]


def test_local_resolution_rejects_unadvertised_track_harness_and_mode(tmp_path):
    manifest, _manifest_hash, _entry_hash = _bundle(tmp_path)

    class EmptyCatalog:
        def benchmarks(self):
            return {"datasets": []}

    unsupported = resolve_target(
        "unseen-fixture", dataset="evovling_tools", mode="deployment",
        harness="react", runtime_adapter=manifest, client=EmptyCatalog(),
    )
    assert unsupported["location"] == "unavailable"
    assert unsupported["local_rejections"] == ["harness"]

    raw = json.loads(manifest.read_text())
    raw["capabilities"]["tracks"] = ["evovling_tools"]
    raw["runtime"]["enforced_tracks"] = ["evovling_tools"]
    evidence = tmp_path / raw["runtime"]["enforcement_evidence"]["path"]
    report = json.loads(evidence.read_text())
    report["tracks"] = {"evovling_tools": report["tracks"]["evovling_tools"]}
    evidence.write_text(json.dumps(report, sort_keys=True))
    raw["runtime"]["enforcement_evidence"]["sha256"] = hashlib.sha256(
        evidence.read_bytes()
    ).hexdigest()
    manifest.write_text(json.dumps(raw, sort_keys=True))
    skills = resolve_target(
        "unseen-fixture", dataset="evovling_skills", mode="task_specific",
        harness="command", runtime_adapter=manifest, client=EmptyCatalog(),
    )
    assert skills["location"] == "unavailable"
    assert "track" in skills["local_rejections"]


def test_loopback_requires_manifest_and_entrypoint_hash_approval(tmp_path):
    manifest, manifest_hash, entry_hash = _bundle(tmp_path)
    with pytest.raises(PermissionError, match="manifest and code are not approved"):
        serve_local(manifest, port=18998, approved_sha256=[manifest_hash])
    assert manifest_hash != entry_hash


def test_general_leaderboard_map_keeps_legacy_fields():
    report = {"success_rate": .5, "accuracy": .25, "n_tasks": 4, "n_success": 2,
              "n_errors": 0, "agent_s": 4, "total_tokens": 0,
              "n_with_tokens": 0, "complete": True}
    row = aggregate_leaderboard({"eog": [report], "unseen-fixture": [report]}, "agent")
    assert row["eog"] == 50.0
    assert row["benchmark_results"]["unseen-fixture"]["score"] == 25.0
    assert row["overall"] == 50.0


def test_evolve_eval_package_manifest_and_nested_path(monkeypatch):
    from eval_service import service

    monkeypatch.setattr(service, "_SKILL_SHARED_API_KEY", "test-eval-key")
    client = TestClient(service.app)
    headers = {"Authorization": "Bearer test-eval-key"}
    response = client.get("/resources/evolve-eval/manifest.json", headers=headers)
    assert response.status_code == 200
    files = response.json()["files"]
    assert any(item["path"] == "references/adapter-contract.md" for item in files)
    assert any(item["path"] == "references/resource-enforcement.md" for item in files)
    for item in files:
        body = client.get(item["url"], headers=headers).content
        assert hashlib.sha256(body).hexdigest() == item["sha256"]
    assert client.get(
        "/resources/evolve-eval/references/../../SKILL.md", headers=headers
    ).status_code == 404
    denial = client.get("/resources/evolve-eval/install.sh")
    assert denial.status_code == 200
    assert "Authentication required" in denial.text
    assert "exit 22" in denial.text
    assert "manifest.json" not in denial.text
    installer = client.get("/resources/evolve-eval/install.sh", headers=headers)
    assert installer.status_code == 200
    assert "/resources/evolve-eval/manifest.json" in installer.text
    assert "sha256" in installer.text


def test_reviewed_adapters_report_only_physically_enforced_tracks():
    adapters = SKILL_ROOT / "references" / "adapters"
    tracks = {}
    for name in ("tb2", "apex-agents", "hyper-tau"):
        manifest = json.loads((adapters / f"{name}.json").read_text())
        tracks[name] = manifest["runtime"]["enforced_tracks"]
    assert tracks == {
        "tb2": [], "apex-agents": ["evovling_tools"], "hyper-tau": [],
    }


def _load_apex_runner():
    path = (
        SKILL_ROOT / "references" / "adapters" / "run_apex.py"
    )
    spec = importlib.util.spec_from_file_location("evolve_eval_run_apex_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_pinned_apex_allowlist_middleware():
    path = (
        ROOT / "data_dry_run" / "apex_builder" / "cache" / "archipelago" /
        "environment" / "runner" / "gateway" / "gateway.py"
    )
    if not path.is_file():
        pytest.skip("the optional pinned APEX checkout is not part of this repository")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_AllowedToolsMiddleware"
    )
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
              class_node],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "Middleware": object,
        "override": lambda function: function,
        "tool_name_matches": lambda configured_tool_name, observed_tool_name:
            configured_tool_name == observed_tool_name,
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_AllowedToolsMiddleware"]


def test_apex_operation_allowlist_contains_selected_operation():
    runner = _load_apex_runner()
    assert runner._allowed_tool_names(["documents.read_document_content"]) == [
        "docs_server_read_document_content"
    ]


def test_apex_operation_allowlist_omits_unselected_and_rejects_unknown():
    runner = _load_apex_runner()
    selected = runner._allowed_tool_names(["documents.read_document_content"])
    assert "docs_server_create_document" not in selected
    with pytest.raises(RuntimeError, match="unknown APEX operation"):
        runner._allowed_tool_names(["unknown.execute"])

    middleware = _load_pinned_apex_allowlist_middleware()(selected)

    async def exercise():
        async def listed(_context):
            return [SimpleNamespace(name="docs_server_read_document_content"),
                    SimpleNamespace(name="docs_server_create_document")]

        visible = await middleware.on_list_tools(None, listed)
        assert [tool.name for tool in visible] == ["docs_server_read_document_content"]

        async def called(_context):
            return "called"

        allowed_context = SimpleNamespace(
            message=SimpleNamespace(name="docs_server_read_document_content")
        )
        assert await middleware.on_call_tool(allowed_context, called) == "called"
        forbidden_context = SimpleNamespace(
            message=SimpleNamespace(name="docs_server_create_document")
        )
        with pytest.raises(ValueError, match="not in the allowlist"):
            await middleware.on_call_tool(forbidden_context, called)

    asyncio.run(exercise())


def _apex_runtime(tmp_path: Path, monkeypatch) -> LocalRuntime:
    manifest = (
        SKILL_ROOT / "references" / "adapters" / "apex-agents.json"
    )
    try:
        inspected = inspect_adapter(manifest)
    except ValueError as exc:
        if "dataset root does not exist" in str(exc):
            pytest.skip("the generated APEX datasets are not part of this repository")
        raise
    monkeypatch.setenv("EVAL_LOCAL_OUTPUT_ROOT", str(tmp_path / "sessions"))
    return LocalRuntime(
        manifest, {inspected["manifest_sha256"], inspected["entrypoint_sha256"]}
    )


def test_apex_task_specific_context_is_exact_oracle_set(tmp_path, monkeypatch):
    runtime = _apex_runtime(tmp_path, monkeypatch)
    created = runtime.create({
        "dataset": "evovling_tools", "benchmark": "apex-agents",
        "version": 2, "split": "train",
        "task_id": "task_b78c4510be784e6a8b8f0394aafd785d",
        "resource_mode": "oracle",
    })
    assert created["task"]["resources"]["names"] == [
        "documents.read_document_content"
    ]
    assert created["action"]["resource_enforced"] is True
    state = runtime.sessions[created["session_id"]]["state"]
    assert state["context"]["effective_tools"] == ["documents.read_document_content"]


def test_apex_self_evolving_context_uses_only_stage_resources(tmp_path, monkeypatch):
    runtime = _apex_runtime(tmp_path, monkeypatch)
    created = runtime.create({
        "dataset": "evovling_tools", "benchmark": "apex-agents",
        "version": 2, "split": "train",
        "task_id": "task_b78c4510be784e6a8b8f0394aafd785d",
        "resource_mode": "accumulative",
    })
    selected = set(created["task"]["resources"]["names"])
    state = runtime.sessions[created["session_id"]]["state"]
    assert set(state["context"]["effective_tools"]) == selected
    final_rows = (
        runtime.roots["evovling_tools"] / "v4" / "test.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    final_tools = set(json.loads(final_rows[0])["cummulative_tools"])
    assert final_tools - selected
    assert not (final_tools - selected) & set(state["context"]["effective_tools"])


def test_apex_enforcement_evidence_is_reproducible():
    root = SKILL_ROOT / "references" / "adapters"
    script = root / "verify_apex_enforcement.py"
    spec = importlib.util.spec_from_file_location("evolve_eval_apex_enforcement_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    committed = json.loads((root / "apex-tools-resource-enforcement.json").read_text())
    checkout = ROOT / "data_dry_run" / "apex_builder" / "cache" / "archipelago"
    if not checkout.is_dir():
        assert committed["tracks"]["evovling_tools"]["status"] == "passed"
        pytest.skip("the optional pinned APEX checkout is not part of this repository")
    generated = module._report((root / "apex-agents.json").resolve())
    assert generated == committed
    inspected = inspect_adapter(root / "apex-agents.json")
    assert "verify_apex_enforcement.py" in inspected["runtime_artifacts"]


def test_hyper_adapter_defaults_to_openai_only_and_exposes_model_choice():
    manifest = json.loads((
        SKILL_ROOT / "references" / "adapters" / "hyper-tau.json"
    ).read_text(encoding="utf-8"))
    assert manifest["credentials"] == ["OPENAI_API_KEY"]
    assert "OPENROUTER_API_KEY" in manifest["optional_credentials"]
    selection = manifest["runtime"]["model_selection"]
    assert selection["developer_harness_default"] == "codex"
    assert selection["developer_model_default"] == "gpt-5"
    assert selection["constructed_agent_model_default"] == "gpt-5.6-sol"
    assert selection["provider_credentials_are_conditional"] is True


def test_all_service_owned_openai_defaults_are_gpt5(monkeypatch):
    monkeypatch.delenv("EVAL_SERVICE_OPENAI_MODEL", raising=False)
    from eval_service import ale_codex_runner, eog_react, service

    assert service.DEFAULT_OPENAI_MODEL == "gpt-5"
    assert ale_codex_runner.DEFAULT_OPENAI_MODEL == "gpt-5"
    assert eog_react.DEFAULT_REACT_MODEL == "gpt-5"

    apex = json.loads((
        SKILL_ROOT / "references" / "adapters" / "apex-agents.json"
    ).read_text(encoding="utf-8"))
    apex_models = apex["runtime"]["model_selection"]
    assert apex_models["agent_model_default"] == "openai/gpt-5"
    assert apex_models["judge_model_default"] == "openai/gpt-5"


def test_skill_documents_benchmark_general_per_verifier_access():
    reference = (
        SKILL_ROOT / "references" / "http-and-sdk.md"
    ).read_text(encoding="utf-8")
    assert "report.grade.per_verifier" in reference
    assert "report.verifier_results" in reference
    assert "report.task_results" in reference
    assert "built-in and local adapters" in reference


def test_skill_requires_user_approved_negative_controls_before_comparability():
    skill_root = SKILL_ROOT
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    workflow = (skill_root / "references" / "guided-workflow.md").read_text(
        encoding="utf-8"
    )
    enforcement = (skill_root / "references" / "resource-enforcement.md").read_text(
        encoding="utf-8"
    )
    assert "ask them to approve the implementation" in skill
    assert "Pause here and ask the user to approve" in workflow
    assert "forbidden_tool_invocation_rejected" in enforcement
    assert "forbidden_skill_path_inaccessible" in enforcement
    assert "forbidden_agent_spawn_rejected" in enforcement
    assert "Only tracks appearing in `inspect-adapter`'s `comparable_tracks`" in enforcement
