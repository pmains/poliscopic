"""Compatibility alias for the canonical :mod:`poliscopic.db.tier` module.

Existing commands import ``db.tier`` after adding ``scripts/`` to their import
path. Keep that contract while Phase 4 moves implementation modules into the
installable package. The temporary path bridge is deleted once all supported
entry points install and import ``poliscopic`` directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_src = Path(__file__).resolve().parents[2] / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from poliscopic.db import tier as _canonical  # noqa: E402

# Make both import paths resolve to one module object. This preserves internal
# attributes as well as the public ``__all__`` contract.
sys.modules[__name__] = _canonical
