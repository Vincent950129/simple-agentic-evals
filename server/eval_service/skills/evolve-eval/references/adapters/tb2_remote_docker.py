"""Harbor Docker backend for a daemon in a separate mount namespace."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.environments.docker.compose_env import legacy_log_mount_env_vars
from harbor.environments.docker.docker import DockerEnvironment


class RemoteDockerEnvironment(DockerEnvironment):
    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return super().capabilities.model_copy(update={"mounted": False})

    def _write_mounts_compose_file(self) -> Path:
        self._cleanup_mounts_compose_file()
        self._mounts_compose_temp_dir = tempfile.TemporaryDirectory()
        path = Path(self._mounts_compose_temp_dir.name) / "docker-compose-mounts.json"
        path.write_text(json.dumps({"services": {"main": {}}}, indent=2) + "\n")
        return path

    def _compose_infra_env_vars(self) -> dict[str, str]:
        values = super()._compose_infra_env_vars()
        values.update(legacy_log_mount_env_vars(self._mounts, host_value="target"))
        return values

    async def prepare_logs_for_host(self) -> None:
        return None
