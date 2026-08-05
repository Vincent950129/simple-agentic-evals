# Vendored verbatim from the EnterpriseOps-Gym harness (evovle_skills/src/endpoints.py).
# Fix bugs upstream and re-copy; local edits would fork the grader.
"""
Auto-detect EOG gym MCP endpoints when this process is running inside a
container (e.g. a Kubernetes dev pod) with `/var/run/docker.sock` mounted
from the host but no shared network namespace.

In that topology `docker ps` succeeds, but `localhost:<port>` from inside
the pod doesn't reach the gym -- the gym is on the host's Docker bridge
network.  We discover each gym's bridge IP and the port the inner
process actually binds to (which is often *not* the port the run command
publishes -- e.g. `gym-itsm` publishes 8006 but the FastMCP process
actually listens on 8005, the image's EXPOSE'd port).

The logic mirrors `reference/EnterpriseOps-Gym/utils/patch_mcp_endpoints.py`
but is reimplemented here so this package stays self-contained.

Usage:

    from .endpoints import patch_row
    row = patch_row(row)            # idempotent, cached

If the original URLs are reachable, `patch_row` is a no-op.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
from copy import deepcopy
from typing import Any

from .dataset import TaskRow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Cache discovered endpoints for the lifetime of the process.
# Map: mcp_server_name -> "http://<host>:<port>"
_ENDPOINT_CACHE: dict[str, str] | None = None


def patch_row(row: TaskRow) -> TaskRow:
    """Return `row` with any gym URLs rewritten to a reachable endpoint.

    Idempotent and lazily memoised: the discovery probe runs at most
    once per process.  Honours `EVOVLE_NO_ENDPOINT_DISCOVERY=1` to
    disable rewriting entirely (useful on bare-metal where the dataset's
    `localhost:<port>` URLs already work).
    """
    if os.environ.get("EVOVLE_NO_ENDPOINT_DISCOVERY") == "1":
        return row

    endpoints = _resolve_endpoints()
    if not endpoints:
        return row

    rewrites: dict[str, str] = {}
    new_gyms: list[dict[str, Any]] = []
    changed = False
    for gym in row.gym_servers_config:
        gym2 = deepcopy(gym)
        name = gym2.get("mcp_server_name", "")
        new_url = endpoints.get(name)
        if new_url and new_url != gym2.get("mcp_server_url"):
            rewrites[name] = f"{gym2.get('mcp_server_url')!r} -> {new_url!r}"
            gym2["mcp_server_url"] = new_url
            changed = True
        new_gyms.append(gym2)

    if not changed:
        return row

    logger.info("[endpoints] rewrote URLs for %s: %s",
                row.task_id, "; ".join(rewrites.values()))
    return TaskRow(
        domain=row.domain, version=row.version, split=row.split,
        task_id=row.task_id, user_prompt=row.user_prompt,
        system_prompt=row.system_prompt,
        oracle_skills=row.oracle_skills,
        cumulative_oracle_skills=row.cumulative_oracle_skills,
        selected_tools=row.selected_tools,
        mcp_endpoint=row.mcp_endpoint,
        gym_servers_config=new_gyms,
        verifiers=row.verifiers,
        raw=row.raw,
    )


def discover_now() -> dict[str, str]:
    """Force a fresh discovery probe and return the resulting map.

    Useful for the preflight script.  Resets the process-wide cache.
    """
    global _ENDPOINT_CACHE
    _ENDPOINT_CACHE = None
    return _resolve_endpoints()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _resolve_endpoints() -> dict[str, str]:
    global _ENDPOINT_CACHE
    if _ENDPOINT_CACHE is not None:
        return _ENDPOINT_CACHE

    out: dict[str, str] = {}
    containers = _find_gym_containers()
    if not containers:
        logger.debug("[endpoints] no gym containers discovered via docker ps")
        _ENDPOINT_CACHE = out
        return out

    for c in containers:
        bridge_ip = _container_bridge_ip(c["id"])
        if not bridge_ip:
            continue
        port = _detect_listening_port(c["id"], c["ports_str"], bridge_ip)
        if port is None:
            continue
        # Map both the EOG canonical names (`gym-itsm-mcp`) and a few
        # aliases derived from the docker image suffix.
        url = f"http://{bridge_ip}:{port}"
        for alias in _name_aliases(c["domain_part"]):
            out.setdefault(alias, url)

    if out:
        # One INFO-level summary; the full table goes to DEBUG so it's
        # available with --verbose / EVOVLE_LOG_LEVEL=DEBUG but doesn't
        # spam the console on every trial.
        logger.info("[endpoints] discovered %d gym endpoint(s) (set EVOVLE_LOG_LEVEL=DEBUG for the full table)", len(out))
        for name, url in out.items():
            logger.debug("    %-25s %s", name, url)

    _ENDPOINT_CACHE = out
    return out


def _name_aliases(domain_part: str) -> list[str]:
    """The same gym appears under several `mcp_server_name`s in the dataset.

    For `domain_part="itsm"` the dataset uses `gym-itsm-mcp`; for `csm`
    it's `sn-csm-server`; for `hr` it's `sn-hr-internal`.  We err on the
    side of including all known forms so the rewriter is permissive.
    """
    d = domain_part.lower()
    out = [
        f"gym-{d}-mcp",       # itsm, teams, email, calendar, drive, hr (some)
        f"gym-{d}",
        d,
    ]
    if d == "csm":
        out.append("sn-csm-server")
    if d == "hr":
        out.append("sn-hr-internal")
    if d == "drive":
        out.append("gym-google-drive-mcp")
    if d == "calendar":
        out.append("gym-calendar")
    return out


# ---------------------------------------------------------------------------
# Docker probing helpers (subprocess-based; no docker SDK dep)
# ---------------------------------------------------------------------------

def _docker(*args: str, timeout: int = 10) -> str:
    try:
        r = subprocess.run(["docker", *args], capture_output=True,
                           text=True, timeout=timeout)
        if r.returncode != 0:
            return ""
        return r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _find_gym_containers() -> list[dict[str, str]]:
    raw = _docker("ps", "--format", "{{.ID}}\t{{.Image}}\t{{.Ports}}")
    if not raw:
        return []
    out: list[dict[str, str]] = []
    for line in raw.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        cid, image = parts[0], parts[1]
        ports = parts[2] if len(parts) > 2 else ""
        if "enterpriseops-gym-mcp-" not in image:
            continue
        domain_part = image.split("enterpriseops-gym-mcp-")[-1].split(":")[0]
        out.append({"id": cid, "image": image, "ports_str": ports,
                    "domain_part": domain_part})
    return out


def _container_bridge_ip(cid: str) -> str | None:
    raw = _docker(
        "inspect", cid,
        "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
    ).strip()
    if raw and raw not in ("<no value>",) and not raw.startswith("<"):
        return raw
    return None


def _detect_listening_port(cid: str, ports_str: str, bridge_ip: str) -> int | None:
    """Find the port the inner MCP process is actually bound to.

    Priority:
      1. Bare EXPOSE'd ports (entries in `docker ps Ports` with no `->`).
      2. Container-side of each `host:X->container:Y/tcp` mapping.
      3. `Config.ExposedPorts` from `docker inspect`.
    Then probe each candidate via TCP to confirm.
    """
    candidates: list[int] = []

    if ports_str:
        # (1) Bare EXPOSE'd
        for mapping in ports_str.split(","):
            mapping = mapping.strip()
            if not mapping or "->" in mapping:
                continue
            try:
                p = int(mapping.split("/")[0])
                if p not in candidates:
                    candidates.append(p)
            except ValueError:
                pass
        # (2) Container-side of each `host:X->container:Y` mapping
        for mapping in ports_str.split(","):
            mapping = mapping.strip()
            if "->" not in mapping:
                continue
            try:
                p = int(mapping.split("->")[-1].split("/")[0])
                if p not in candidates:
                    candidates.append(p)
            except ValueError:
                pass

    # (3) ExposedPorts from inspect
    raw = _docker("inspect", cid,
                  "--format", "{{json .Config.ExposedPorts}}")
    try:
        exposed = json.loads(raw.strip()) or {}
        for port_proto in exposed:
            try:
                p = int(port_proto.split("/")[0])
                if p not in candidates:
                    candidates.append(p)
            except ValueError:
                pass
    except Exception:
        pass

    if not candidates:
        return None

    # Probe each candidate via TCP from this process's network namespace.
    for p in candidates:
        try:
            with socket.create_connection((bridge_ip, p), timeout=1.5):
                return p
        except OSError:
            continue
    logger.warning(
        "[endpoints] no candidate port reachable on %s for container %s "
        "(tried %s); falling back to %s",
        bridge_ip, cid, candidates, candidates[0],
    )
    return candidates[0]
