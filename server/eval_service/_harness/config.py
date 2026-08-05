"""Paths the vendored harness modules read.

Trimmed to the two roots :mod:`.dataset` and :mod:`.eog_verifier` need; the
upstream ``evovle_skills.src.config`` also carries Codex/patcher/job settings
that belong to the research harness, not to this service.

Both roots are anchored at the server checkout and overridable by env, since a
deployment usually keeps the datasets and the gym sources outside the repo:

    EVAL_SERVICE_SKILLS_DATA_ROOT   <server>/data/evovling_skills
    EOG_ROOT                        <server>/reference/EnterpriseOps-Gym
"""

from __future__ import annotations

import os
from pathlib import Path

_THIS_FILE = Path(__file__).resolve()
SRC_DIR = _THIS_FILE.parent                  # .../eval_service/_harness
PKG_DIR = SRC_DIR.parent                     # .../eval_service
REPO_ROOT = PKG_DIR.parent                   # .../server

# Only the vendored ``dataset`` helpers the service does not call read this;
# ``eval_service.loader`` resolves dataset paths itself from
# ``EVAL_SERVICE_DATA_ROOT``.
DATA_ROOT = Path(
    os.environ.get("EVAL_SERVICE_SKILLS_DATA_ROOT", "")
    or REPO_ROOT / "data" / "evovling_skills"
).resolve()

# The EnterpriseOps-Gym checkout. Seed-SQL paths in the dataset rows are
# relative to it, so EOG session creation needs this to point at real sources.
EOG_ROOT = Path(
    os.environ.get("EOG_ROOT", "") or REPO_ROOT / "reference" / "EnterpriseOps-Gym"
).resolve()
