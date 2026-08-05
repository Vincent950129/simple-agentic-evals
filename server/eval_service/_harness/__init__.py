"""Vendored slice of the EnterpriseOps-Gym harness the service builds on.

Upstream these live in the research repo as ``evovle_skills.src.*``; only the
three modules the service actually needs at import time are carried here, so
``eval_service`` runs from this checkout without the rest of the harness:

  * :mod:`.dataset`      -- ``TaskRow`` + JSONL parsing
  * :mod:`.endpoints`    -- reachable-gym-endpoint discovery (``patch_row``)
  * :mod:`.eog_verifier` -- seed / SQL-verify / teardown + header construction

They are copied verbatim (only :mod:`.config` is trimmed to the two paths they
read), so grading stays bit-for-bit identical to the reference harness. The
*optional* server-side agent runners for the EOG skills/agents tracks still
import the full harness lazily and degrade to HTTP 501 when it is absent --
see ``service._svc_run_codex``.
"""
