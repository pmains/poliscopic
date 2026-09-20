#!/usr/bin/env python3
"""``stage2_s1_policy.py`` — authoritative policy for Stage 2 S1 body parentage.

Stage 2 slice S1 assigns a canonical ``public_body_id`` to meetings whose parent
is currently unknown.  The policy — which source keys map to which body, which
streams are held, and how many rows each bucket must contain — is *data*, and it
lives here alone.  Plan building, applying, and verification all import it, so a
plan cannot be built against one policy and applied against another.

Design rules
------------
* **Fail closed.**  A meeting whose ``body`` is neither an explicit alias, an
  explicit collision resolution, a hold, nor a uniquely resolvable registry key
  is *unhandled*.  Unhandled rows raise; they are never guessed.
* **Collisions are explicit.**  ``chandler-cf`` matches two registry rows through
  different namespaces (one body's ``slug``, another's ``body_code``).  That is
  resolved by an explicit, reviewed decision — never by "pick the first".
* **Holds are not failures.**  ``phoenix-gp`` is a human decision and ``__skip__``
  is an extraction sentinel.  Both are held and counted.
* **No I/O.**  This module reads no database and writes no file.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "ALGORITHM_VERSION",
    "ALIAS_TARGETS",
    "COLLISION_TARGETS",
    "EXPECTED_COUNTS",
    "HOLD_STREAMS",
    "STRATEGY_ALIAS",
    "STRATEGY_COLLISION",
    "STRATEGY_DIRECT",
    "STRATEGY_HOLD",
    "Decision",
    "PolicyError",
    "counts_problem",
    "decide",
    "meeting_fingerprint",
    "policy_snapshot",
]

#: Plan format version.  Bumped when the plan's shape or meaning changes.
ALGORITHM_VERSION = "kg-stage2-s1/1.0"

#: Short source codes that bind to a body their long-form code already names.
ALIAS_TARGETS: Mapping[str, int] = {
    "chandler-pz": 17,   # Chandler Planning & Zoning Commission
    "mesa-pz": 37,       # Mesa Planning & Zoning Board
    "peoria-planning-zoning": 87,     # Peoria Planning & Zoning Commission
}

#: Collision streams resolved by explicit review (asset-edge evidence).
COLLISION_TARGETS: Mapping[str, int] = {
    "chandler-cf": 334,   # Chandler Cultural Foundation (not 308, a district)
    "chandler-pdc": 333,  # Chandler Mayor's Committee for People w/ Disabilities
}

#: Streams deliberately held, with the reason they are not mapped.
HOLD_STREAMS: Mapping[str, str] = {
    "phoenix-gp": "human_decision",
    "__skip__": "sentinel_unmappable",
}

#: Every deterministic bucket must land on exactly these counts.
EXPECTED_COUNTS: Mapping[str, int] = {
    "direct": 978,
    "alias": 126,
    "collision": 108,
    "assignments": 1212,
    "hold_phoenix_gp": 217,
    "hold_sentinel": 1,
    "holds": 218,
    "null_parent_total": 1430,
}

STRATEGY_DIRECT = "direct"
STRATEGY_ALIAS = "alias"
STRATEGY_COLLISION = "collision_resolved"
STRATEGY_HOLD = "hold"


class PolicyError(RuntimeError):
    """A row could not be classified, or the counts did not reconcile."""


@dataclass(frozen=True)
class Decision:
    """The policy verdict for one source ``body`` value."""

    body: str
    strategy: str
    target_public_body_id: int | None
    reason: str

    @property
    def assigned(self) -> bool:
        """Whether this decision writes a parent."""
        return self.target_public_body_id is not None


def meeting_fingerprint(row: Mapping[str, Any]) -> str:
    """Fingerprint the identity-bearing fields of one meeting row.

    The fingerprint is intentionally independent of ``public_body_id`` so the
    same row can be re-checked before and after a write.
    """
    parts = [
        f"id={int(row['id'])}",
        f"body={row.get('body') or ''}",
        f"meeting_id={row.get('meeting_id') or ''}",
        f"meeting_date={row.get('meeting_date') or ''}",
        f"jurisdiction_id={row.get('jurisdiction_id')}",
    ]
    encoded = "\n".join(parts)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def decide(body: Any, candidate_ids: Sequence[int]) -> Decision:
    """Classify one source ``body`` value against the policy.

    ``candidate_ids`` are the registry rows that match the value through
    ``body_code`` or ``slug`` (in that order of authority).  A body that is not
    covered by the policy and does not match exactly one candidate raises
    :class:`PolicyError` — unhandled rows are never silently skipped.
    """
    key = "" if body is None else str(body)
    unique = sorted(set(int(i) for i in candidate_ids))

    if key in HOLD_STREAMS:
        return Decision(key, STRATEGY_HOLD, None, HOLD_STREAMS[key])

    if key in COLLISION_TARGETS:
        target = int(COLLISION_TARGETS[key])
        if len(unique) < 2:
            raise PolicyError(
                f"{key!r} is a collision stream but matched {len(unique)} "
                "registry row(s); the expected ambiguity is gone"
            )
        if target not in unique:
            raise PolicyError(
                f"{key!r} resolves to body {target}, which is not among the "
                f"matching registry rows {unique}"
            )
        return Decision(key, STRATEGY_COLLISION, target, "explicit_collision_resolution")

    if key in ALIAS_TARGETS:
        target = int(ALIAS_TARGETS[key])
        if len(unique) > 1:
            raise PolicyError(
                f"alias {key!r} is ambiguous: {unique}"
            )
        return Decision(key, STRATEGY_ALIAS, target, "explicit_alias")

    if len(unique) == 1:
        return Decision(key, STRATEGY_DIRECT, unique[0], "unique_registry_match")

    if not unique:
        raise PolicyError(f"unhandled body {key!r}: no registry match")
    raise PolicyError(f"unhandled body {key!r}: ambiguous registry matches {unique}")


def counts_problem(counts: Mapping[str, int]) -> str | None:
    """Return a human-readable problem if counts differ, else ``None``.

    The reconciliation is checked as three independent equations so a tampered
    plan cannot satisfy one by breaking another.
    """
    for name, expected in EXPECTED_COUNTS.items():
        actual = int(counts.get(name, -1))
        if actual != expected:
            return f"count {name}={actual} expected {expected}"

    buckets = int(counts["direct"]) + int(counts["alias"]) + int(counts["collision"])
    if buckets != int(counts["assignments"]):
        return f"direct+alias+collision={buckets} != assignments={counts['assignments']}"

    holds = int(counts["hold_phoenix_gp"]) + int(counts["hold_sentinel"])
    if holds != int(counts["holds"]):
        return f"hold buckets={holds} != holds={counts['holds']}"

    total = buckets + holds
    if total != int(counts["null_parent_total"]):
        return f"assignments+holds={total} != null_parent_total={counts['null_parent_total']}"
    return None


def policy_snapshot() -> dict[str, Any]:
    """A serialisable copy of the policy, embedded in every plan."""
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "alias_targets": dict(sorted(ALIAS_TARGETS.items())),
        "collision_targets": dict(sorted(COLLISION_TARGETS.items())),
        "hold_streams": dict(sorted(HOLD_STREAMS.items())),
        "expected_counts": dict(sorted(EXPECTED_COUNTS.items())),
    }
