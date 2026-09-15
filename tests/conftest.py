"""Import the SDK and self-hosted service directly from this checkout."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "server" / "eval_service", ROOT / "server", ROOT):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)
