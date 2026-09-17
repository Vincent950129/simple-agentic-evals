import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk"))
sys.path.insert(0, str(ROOT.parent))

from simple_agentic_evals.command_agent import CommandAgent, materialize_task_workspace
from simple_agentic_evals.leaderboard import aggregate_leaderboard
from simple_agentic_evals import skill_cli
from simple_agentic_evals import leaderboard as leaderboard_module


def test_sdk_017_keeps_the_entire_016_public_surface():
    import simple_agentic_evals as sdk

    legacy = {
        "run_benchmark", "BenchmarkReport", "CohortResult", "EvalClient", "Task",
        "GradeResult", "EvalReport", "McpServer", "MCPSession", "MissingAPIKey",
        "ServiceError", "react_agent", "acp_codex_agent", "AgentRun", "CodexRun",
        "run_eog_agent", "run_codex_agent", "to_openai_tools",
        "sanitize_tool_schema", "ContinualMetrics", "StageResult",
    }
    assert legacy <= set(sdk.__all__)


def _report(success, score, tasks=10, tokens=None, complete=True, errors=0):
    return {
        "success_rate": success,
        "accuracy": score,
        "n_tasks": tasks,
        "n_success": round(success * tasks),
        "n_errors": errors,
        "agent_s": 3600,
        "total_tokens": tokens or 0,
        "n_with_tokens": tasks if tokens is not None else 0,
        "complete": complete,
    }


def test_leaderboard_aggregation_is_pooled_and_preserves_missing_usage():
    row = aggregate_leaderboard(
        {
            "eog": [_report(.5, .6, 10), _report(.7, .8, 10)],
            "ale": [_report(1, .9, 2, 1_000_000), _report(.5, .7, 2, 3_000_000)],
        },
        "agent",
    )
    assert row["eog"] == 60.0
    assert row["eogSd"] == 10.0
    assert row["eogScoreSd"] == 10.0
    assert row["eogTok"] is None
    assert row["aleTok"] == 2.0
    # (5+7+2+1) solved / (10+10+2+2), not a macro-average of benchmarks.
    assert row["overall"] == 62.5
    assert "partial" not in row


def test_leaderboard_marks_subsampled_and_missing_benchmark_partial():
    row = aggregate_leaderboard(
        {"eog": [_report(1, 1, complete=False)]}, "smoke", force_partial=True
    )
    assert row["partial"] is True
    assert row["overall"] == 100.0
    assert "ale" not in row


def test_leaderboard_retains_per_task_per_verifier_results():
    report = {
        **_report(.5, .25, tasks=1, complete=False),
        "task_results": [{
            "task_id": "domain/task",
            "score": .25,
            "per_verifier": [{
                "name": "content", "score": .25, "passed": False,
            }],
        }],
    }
    row = aggregate_leaderboard({"ale": [report]}, "diagnostic")
    task = row["task_results"]["ale"][0]["tasks"][0]
    assert task["task_id"] == "domain/task"
    assert task["per_verifier"] == [{
        "name": "content", "score": .25, "passed": False,
    }]


def test_run_leaderboard_resumes_completed_repeat(monkeypatch):
    calls = []

    def fake_run(_agent, _mode, **kwargs):
        calls.append(kwargs["benchmark"])
        return SimpleNamespace(benchmark=kwargs["benchmark"], accuracy=.5, success_rate=.5,
                               n_tasks=2, n_success=1, n_errors=0, agent_s=1,
                               total_tokens=0, n_with_tokens=0, complete=False)

    monkeypatch.setattr(leaderboard_module, "run_benchmark", fake_run)
    existing = {"eog": [_report(.5, .5, tasks=2, complete=False)], "ale": []}
    checkpoints = []
    row = leaderboard_module.run_leaderboard(
        lambda task: None, "resume", seeds=1, limit=2, existing_reports=existing,
        on_report=lambda benchmark, index, report: checkpoints.append((benchmark, index, report)),
        progress=False,
    )
    assert calls == ["ale"]
    assert checkpoints[0][:2] == ("ale", 0)
    assert row["partial"] is True


def test_sdk_full_run_needs_explicit_cost_confirmation():
    with pytest.raises(ValueError, match="confirm_full_cost"):
        leaderboard_module.run_leaderboard(lambda task: None, "full", progress=False)


class _Server:
    name = "gym"
    path = "/v1/sessions/s/mcp/gym"
    url = "http://service/v1/sessions/s/mcp/gym"
    headers = {}


class _Client:
    base_url = "http://service"
    api_key = "secret-eval-key"

    def __init__(self, resource):
        self.resource = resource

    def resources(self, *_args, **_kwargs):
        return self.resource


class _Task:
    dataset = "evovling_skills"
    benchmark = "eog"
    task_id = "demo/task"
    session_id = "s"
    system_prompt = "system"
    user_prompt = "user"
    required_steps = ["inspect inputs", "write outputs"]
    evaluation = "Public scoring rubric."
    output_path = ""
    mcp_servers = [_Server()]
    _selector = {
        "dataset": "evovling_skills", "benchmark": "eog", "version": "full",
        "split": "test", "domain": "hr", "resource_mode": "accumulative",
    }

    def __init__(self, resource):
        self._client = _Client(resource)

    def mcp_url(self, server):
        return server.url


def test_resource_materialization_skills_and_agents(tmp_path):
    skill = {
        "kind": "skills", "mode": "accumulative", "names": ["calendar"],
        "items": [{"name": "calendar", "files": [{
            "path": "evovling_skills/eog/hr/_oracle/skills/calendar/SKILL.md",
            "content": "# Calendar",
        }]}],
    }
    paths = materialize_task_workspace(_Task(skill), tmp_path / "skill")
    assert (paths["resource_dir"] / "skills/calendar/SKILL.md").read_text() == "# Calendar"
    task_doc = json.loads(paths["task_json"].read_text())
    assert task_doc["required_steps"] == ["inspect inputs", "write outputs"]
    assert task_doc["evaluation"] == "Public scoring rubric."

    agents = {
        "kind": "agents", "mode": "accumulative", "names": ["helper"],
        "items": [{"name": "helper", "files": [
            {"path": "evovling_agents/eog/hr/v1/agents/helper.toml", "content": "model='x'"},
            {"path": "evovling_agents/eog/hr/v1/agent_skills/helper/SKILL.md", "content": "# H"},
        ]}],
    }
    task = _Task(agents)
    task.dataset = "evovling_agents"
    task._selector = {**task._selector, "dataset": "evovling_agents"}
    paths = materialize_task_workspace(task, tmp_path / "agent", codex=True)
    assert (paths["resource_dir"] / "agents/helper.toml").is_file()
    assert (paths["resource_dir"] / "agent_skills/helper/SKILL.md").is_file()
    assert "multi_agent = true" in (paths["root"] / ".codex/config.toml").read_text()

    tools = {"kind": "tools", "mode": "accumulative", "names": ["calendar_search"], "items": []}
    task = _Task(tools)
    task.dataset = "evovling_tools"
    task._selector = {**task._selector, "dataset": "evovling_tools"}
    paths = materialize_task_workspace(task, tmp_path / "tools")
    assert json.loads(paths["task_json"].read_text())["allowed_tools"] == ["calendar_search"]


def test_remote_ale_workspace_uses_env_auth_and_keeps_inputs_remote(tmp_path):
    resource = {"kind": "agents", "mode": "accumulative", "names": [], "items": []}
    task = _Task(resource)
    task.dataset = "evovling_agents"
    task.benchmark = "ale"
    remote = SimpleNamespace(
        name="ale_sandbox", path="/v1/sessions/s/ale/mcp",
        url="https://eval.example/v1/sessions/s/ale/mcp", headers={},
    )
    paths = materialize_task_workspace(
        task, tmp_path / "remote", codex=True, fetch_sandbox_inputs=False,
        remote_mcp_server=remote,
    )
    config = (paths["root"] / ".codex/config.toml").read_text()
    state = paths["state_json"].read_text()
    assert '[mcp_servers.ale_sandbox]' in config
    assert 'bearer_token_env_var = "EVAL_SERVICE_API_KEY"' in config
    assert 'default_tools_approval_mode = "approve"' in config
    assert "secret-eval-key" not in config + state
    assert list(paths["input_dir"].iterdir()) == []


def test_command_agent_ale_execution_is_additive_and_opt_in():
    assert CommandAgent(["true"]).ale_execution == "artifact"
    assert CommandAgent(["true"], ale_execution="remote_mcp").ale_execution == "remote_mcp"
    with pytest.raises(ValueError, match="ale_execution"):
        CommandAgent(["true"], ale_execution="replace")


def test_command_agent_isolates_tasks_reports_usage_and_redacts_keys(tmp_path):
    resource = {"kind": "skills", "mode": "none", "names": [], "items": []}
    command = [
        sys.executable, "-c",
        "import json,os,pathlib,sys; "
        "pathlib.Path('seen.txt').write_text(os.getcwd()); "
        "pathlib.Path(os.environ['EVAL_USAGE_JSON']).write_text(json.dumps({'total_tokens': 7, 'n_steps': 2})); "
        "print(os.environ['EVAL_SERVICE_API_KEY']); sys.stdin.read()",
    ]
    agent = CommandAgent(command, output_dir=tmp_path)
    first = agent(_Task(resource))
    second = agent(_Task(resource))
    workspaces = sorted((tmp_path / "tasks").iterdir())
    assert len(workspaces) == 2 and workspaces[0] != workspaces[1]
    assert first["total_tokens"] == 7 and first["n_steps"] == 2
    assert second["total_tokens"] == 7
    assert all("secret-eval-key" not in (w / "stdout.log").read_text() for w in workspaces)
    assert all("[REDACTED]" in (w / "stdout.log").read_text() for w in workspaces)


def test_command_agent_nonzero_exit_has_task_log(tmp_path):
    resource = {"kind": "skills", "mode": "none", "names": [], "items": []}
    agent = CommandAgent([sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(4)"], output_dir=tmp_path)
    with pytest.raises(RuntimeError, match="exited 4"):
        agent(_Task(resource))
    assert next((tmp_path / "tasks").iterdir()).joinpath("stderr.log").read_text().strip() == "bad"


def test_tool_allowlist_rejects_unselected_tool(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "base_url": "http://service", "servers": [{"name": "gym", "url": "http://gym", "path": ""}],
        "allowed_tools": ["allowed"], "enforce_allowlist": True,
    }))

    class FakeClient:
        def __init__(self, base_url=None):
            self.base_url, self.headers = base_url, {}

    class FakeMCP:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(skill_cli, "EvalClient", FakeClient)
    monkeypatch.setattr(skill_cli, "MCPSession", FakeMCP)
    args = type("Args", (), {
        "state": str(state), "server": None, "tool_command": "call",
        "name": "forbidden", "arguments": "{}",
    })()
    with pytest.raises(SystemExit, match="not in.*allowlist"):
        skill_cli._tool(args)


def test_full_run_needs_explicit_cost_confirmation():
    args = SimpleNamespace(full=True, confirm_full_cost=False)
    with pytest.raises(SystemExit, match="confirm-full-cost"):
        skill_cli._leaderboard(args)


def test_abort_closes_session_and_cleans_workspace(tmp_path, monkeypatch):
    state = {
        "status": "active", "session_id": "session-1", "task_id": "task-1",
    }
    (tmp_path / ".interactive.json").write_text(json.dumps(state))
    (tmp_path / "task").mkdir()
    (tmp_path / "task" / "artifact").write_text("x")
    deleted = []

    class FakeClient:
        def _delete(self, path):
            deleted.append(path)
            return {}

    monkeypatch.setattr(skill_cli, "EvalClient", FakeClient)
    args = SimpleNamespace(output=str(tmp_path), cleanup=True)
    skill_cli._abort(args)
    assert deleted == ["/v1/sessions/session-1"]
    assert not (tmp_path / "task").exists()
    assert json.loads((tmp_path / ".interactive.json").read_text())["status"] == "aborted"


def test_skill_routes_require_key_are_markdown_and_path_safe(monkeypatch):
    from eval_service import service

    monkeypatch.setattr(service, "_SKILL_SHARED_API_KEY", "test-eval-key")
    client = TestClient(service.app)

    for path in ("/resources/skill.md", "/resources/evolve-eval/SKILL.md"):
        denial = client.get(path)
        assert denial.status_code == 401
        assert denial.headers["www-authenticate"] == "Bearer"
        assert "MyAuthtoken" in denial.json()["detail"]
        assert "mas-orchestra.salesforceresearch.ai" in denial.json()["detail"]
        assert client.get(path, headers={"x-api-key": "wrong"}).status_code == 401
        response = client.get(
            path, headers={"Authorization": "Bearer test-eval-key"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert "name: evolve-eval" in response.text
    assert client.get(
        "/resources/evolve-eval/../SKILL.md",
        headers={"Authorization": "Bearer test-eval-key"},
    ).status_code == 404


def test_sdk_manifest_and_wheel_require_key(monkeypatch):
    from eval_service import service

    monkeypatch.setattr(service, "_SKILL_SHARED_API_KEY", "test-eval-key")
    with TestClient(service.app) as client:
        denial = client.get("/sdk")
        assert denial.status_code == 401
        assert "MyAuthtoken" in denial.json()["detail"]

        headers = {"Authorization": "Bearer test-eval-key"}
        manifest_response = client.get("/sdk", headers=headers)
        assert manifest_response.status_code == 200
        manifest = manifest_response.json()
        assert manifest["requires_api_key"] is True
        assert client.get(manifest["path"]).status_code == 401
        wheel = client.get(manifest["path"], headers=headers)
        assert wheel.status_code == 200
        assert wheel.content.startswith(b"PK")
