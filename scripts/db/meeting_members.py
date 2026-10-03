"""Compatibility alias for :mod:`poliscopic.db.meeting_members`."""

from __future__ import annotations

import sys
from pathlib import Path

_src = Path(__file__).resolve().parents[2] / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from poliscopic.db import meeting_members as _canonical  # noqa: E402

sys.modules[__name__] = _canonical
