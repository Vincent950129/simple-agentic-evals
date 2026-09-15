"""Evolving-benchmarks Evaluation-as-a-Service.

A thin, model-agnostic HTTP service that hosts the *executable environment*
(EnterpriseOps-Gym MCP gyms + per-task DB seeding + SQL verifiers) behind a
session API, so a caller can run an evaluation without installing Docker, the
gyms, or the grading wiring.

Bring-Your-Own-Agent (BYOA): the caller's agent acts on the environment through
the action surface the service hands back (an MCP endpoint for EOG; a file
sandbox for ALE), then asks the service to grade the resulting state.

The heavy lifting is reused from the existing harness. Monorepo deployments
import it directly; standalone clones fall back to the compatibility modules
under :mod:`eval_service._harness`:

  * ``dataset``      -- TaskRow + JSONL parsing
  * ``endpoints``    -- reachable-endpoint discovery (patch_row)
  * ``eog_verifier`` -- seed / verify / teardown + headers

This package only adds the multi-tenant session/lease layer and the wire
contract on top.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
