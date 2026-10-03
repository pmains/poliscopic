"""Compatibility alias for :mod:`poliscopic.db.core`."""

from __future__ import annotations

import sys
from pathlib import Path

_src = Path(__file__).resolve().parents[2] / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from poliscopic.db import core as _canonical  # noqa: E402

sys.modules[__name__] = _canonical
