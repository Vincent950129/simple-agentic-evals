from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def skill_client(monkeypatch) -> TestClient:
    from eval_service import service

    monkeypatch.setattr(service, "_SKILL_SHARED_API_KEY", "test-eval-key")
    return TestClient(
        service.app,
        headers={"Authorization": "Bearer test-eval-key"},
    )


def test_evolve_benchmark_manifest_installs_complete_verified_package(
    skill_client: TestClient,
) -> None:
    client = skill_client

    response = client.get(
        "/resources/evolve-benchmark/manifest.json",
        headers={"host": "builder.example"},
    )
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["name"] == "evolve-benchmark"
    paths = {item["path"] for item in manifest["files"]}
    assert {
        "SKILL.md",
        "references/pipeline.md",
        "references/tracks.md",
        "references/validation.md",
        "references/interactive-workflow.md",
        "references/adapter-contract.md",
        "scripts/evolve_core.py",
        "references/terminal-bench-2.md",
        "references/tb2/v1/software-catalog.json",
        "references/tb2/v1/skill-catalog.json",
        "references/tb2/v1/expected.json",
        "references/apex-agents.md",
        "references/apex/v1/tool-catalog.json",
        "references/apex/v1/skill-catalog.json",
        "references/apex/v1/expected.json",
    } <= paths
    assert len(paths) == 15
    assert not any("/source/" in path for path in paths)

    for item in manifest["files"]:
        resource = client.get(item["url"].replace("http://builder.example", ""))
        assert resource.status_code == 200, item["path"]
        assert hashlib.sha256(resource.content).hexdigest() == item["sha256"]


def test_evolve_benchmark_resources_are_authenticated_rendered_and_path_safe(
    skill_client: TestClient,
) -> None:
    client = skill_client
    skill = client.get(
        "/resources/evolve-benchmark/SKILL.md",
        headers={"x-forwarded-host": "public.example", "x-forwarded-proto": "https"},
    )
    assert skill.status_code == 200
    assert skill.headers["content-type"].startswith("text/markdown")
    assert "name: evolve-benchmark" in skill.text
    assert "four explicit user approvals" in skill.text
    assert "__BASE_URL__" not in skill.text

    engine = client.get("/resources/evolve-benchmark/scripts/evolve_core.py")
    assert engine.status_code == 200
    assert "text/x-python" in engine.headers["content-type"]
    compile(engine.text, "evolve_core.py", "exec")

    for path in (
        "/resources/evolve-benchmark/%2e%2e/evolve-eval/SKILL.md",
        "/resources/evolve-benchmark/%2e%2e%2fevolve-eval%2fSKILL.md",
        "/resources/evolve-benchmark/references",
        "/resources/evolve-benchmark/unknown.md",
        "/resources/evolve-benchmark/.hidden.md",
    ):
        assert client.get(path).status_code == 404, path


def test_evolve_benchmark_compact_installer_is_authenticated_and_complete(
    skill_client: TestClient,
) -> None:
    client = skill_client
    response = client.get(
        "/resources/evolve-benchmark/install.sh",
        headers={"x-forwarded-host": "public.example", "x-forwarded-proto": "https"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-shellscript")
    assert "/resources/evolve-benchmark/manifest.json" in response.text
    assert "sha256(content).hexdigest()" in response.text
    assert "EVOLVE_BUILD_SKILL_DIR" in response.text
    assert 'os.environ.get("EVAL_SERVICE_API_KEY"' in response.text
    assert '"Authorization": f"Bearer {eval_key}"' in response.text
    assert "__BASE_URL__" not in response.text


def test_skill_resources_reject_anonymous_downloads(skill_client: TestClient) -> None:
    client = skill_client

    client.headers.clear()
    installer_denial = client.get("/resources/evolve-benchmark/install.sh")
    assert installer_denial.status_code == 200
    assert installer_denial.headers["x-eval-auth-required"] == "true"
    assert "MyAuthtoken" in installer_denial.text
    assert "exit 22" in installer_denial.text
    assert "/resources/evolve-benchmark/manifest.json" not in installer_denial.text
    assert client.get("/resources/skill.md").status_code == 401
    assert client.get("/resources/evolve-eval/SKILL.md").status_code == 401


def test_pinned_public_tb2_blocks_print_the_private_parity_oracle_in_full() -> None:
    repo = Path(__file__).resolve().parents[1]
    reference = (
        repo / "server/eval_service/skills/evolve-benchmark/references/terminal-bench-2.md"
    ).read_text(encoding="utf-8")
    sources = {
        "data_dry_run/tb_builder/__init__.py": repo / "data_dry_run/tb_builder/__init__.py",
        "data_dry_run/tb_builder/__main__.py": repo / "data_dry_run/tb_builder/__main__.py",
        "data_dry_run/tb_builder/builder.py": repo / "data_dry_run/tb_builder/builder.py",
        "data_dry_run/tb_builder/config.py": repo / "data_dry_run/tb_builder/config.py",
        "data_dry_run/tb_builder/remote_docker.py": repo / "data_dry_run/tb_builder/remote_docker.py",
        "evolve_tools/builder/frequency_config.py": repo / "evolve_tools/builder/frequency_config.py",
        "evovle_skills/builder/sequencer.py": repo / "evovle_skills/builder/sequencer.py",
        "evovle_skills/builder/shared/paths.py": repo / "evovle_skills/builder/shared/paths.py",
    }
    for relative, oracle in sources.items():
        chunks = re.findall(
            rf'cat (>>|>) "\$EVOLVE_TB2_WORKDIR/{re.escape(relative)}" '
            r"<<'__EVOHARNESS_TB2_FILE__'\n(.*?)__EVOHARNESS_TB2_FILE__\n",
            reference,
            flags=re.S,
        )
        assert chunks, relative
        assembled = ""
        for redirect, body in chunks:
            assembled = assembled + body if redirect == ">>" else body
        if oracle.is_file():
            assert assembled == oracle.read_text(encoding="utf-8"), relative
        else:
            assert assembled.strip(), relative
    assert "/export/" not in reference
    assert "references/tb2/v1/source" not in reference
    assert reference.count("EVAL_SERVICE_API_KEY is required to download the closed catalog") == 2
    assert reference.count('"Authorization": f"Bearer {eval_key}"') == 2


def test_pinned_public_apex_blocks_print_the_private_reference_in_full() -> None:
    repo = Path(__file__).resolve().parents[1]
    reference = (
        repo / "server/eval_service/skills/evolve-benchmark/references/apex-agents.md"
    ).read_text(encoding="utf-8")
    sources = {
        "data_dry_run/apex_builder/__init__.py": repo / "data_dry_run/apex_builder/__init__.py",
        "data_dry_run/apex_builder/__main__.py": repo / "data_dry_run/apex_builder/__main__.py",
        "data_dry_run/apex_builder/builder.py": repo / "data_dry_run/apex_builder/builder.py",
        "data_dry_run/apex_builder/config.py": repo / "data_dry_run/apex_builder/config.py",
        "evolve_tools/builder/frequency_config.py": repo / "evolve_tools/builder/frequency_config.py",
        "evovle_skills/builder/sequencer.py": repo / "evovle_skills/builder/sequencer.py",
        "evovle_skills/builder/shared/paths.py": repo / "evovle_skills/builder/shared/paths.py",
    }
    for relative, oracle in sources.items():
        chunks = re.findall(
            rf'cat (>>|>) "\$EVOLVE_APEX_WORKDIR/{re.escape(relative)}" '
            r"<<'__EVOHARNESS_APEX_FILE__'\n(.*?)__EVOHARNESS_APEX_FILE__\n",
            reference,
            flags=re.S,
        )
        assert chunks, relative
        assembled = ""
        for redirect, body in chunks:
            assembled = assembled + body if redirect == ">>" else body
        if oracle.is_file():
            assert assembled == oracle.read_text(encoding="utf-8"), relative
        else:
            assert assembled.strip(), relative
    assert "/export/" not in reference
    assert "references/apex/v1/source" not in reference
    assert "rubrics or gold answers" in reference
    assert "does not claim task rewards" in reference

    setup = re.search(r"```bash\n(.*?)\n```", reference, flags=re.S)
    assert setup is not None
    seed_python = re.search(
        r'"\$EVOLVE_APEX_PYTHON" - <<\'PY\'\n(.*?)\nPY',
        setup.group(1),
        flags=re.S,
    )
    assert seed_python is not None
    compile(seed_python.group(1), "apex-seed-download", "exec")
    assert reference.count("EVAL_SERVICE_API_KEY is required to download the closed catalog") == 2
    assert reference.count('"Authorization": f"Bearer {eval_key}"') == 2
