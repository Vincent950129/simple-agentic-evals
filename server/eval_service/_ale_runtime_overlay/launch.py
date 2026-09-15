#!/usr/bin/env python3
"""Launch ALE with eval-service-owned module overrides first on ``sys.path``.

The process keeps ALE_ROOT as its working directory, but executing this file
places the overlay ahead of that checkout.  ``ale_run`` is a namespace package;
our extended ``ale_run.tasks`` package overrides only ``driver.py`` and falls
through to the reference checkout for every other module and asset.
"""
from __future__ import annotations

import os
import runpy
import sys
import importlib
from pathlib import Path

OVERLAY_ROOT = Path(__file__).resolve().parent
EVAL_SERVICE_ROOT = OVERLAY_ROOT.parent
ALE_ROOT = Path(os.environ["EVAL_SERVICE_ALE_ROOT"]).resolve()

ordered = [str(OVERLAY_ROOT), str(EVAL_SERVICE_ROOT), str(ALE_ROOT)]
seen = set(ordered)
sys.path[:] = ordered + [entry for entry in sys.path if entry not in seen]

# Fail closed if Python's namespace-package resolution ever stops selecting the
# service copy.  Running unmodified ALE would still produce an aggregate and
# could otherwise make component coverage silently disappear.
driver = importlib.import_module("ale_run.tasks.driver")
driver_path = Path(driver.__file__).resolve()
if OVERLAY_ROOT not in driver_path.parents:
    raise RuntimeError(f"ALE verifier overlay was not selected: {driver_path}")

if __name__ == "__main__":
    runpy.run_module("ale_run", run_name="__main__", alter_sys=True)
