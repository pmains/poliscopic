"""Pure grouping for multiple result actions in one exact evidence scope."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any


CONNECTOR_RE = re.compile(r"^(?:\s|[,;/&+]|\band\b|\bor\b)*$", re.IGNORECASE)


def _line_bounds(text: str, relative_offset: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, relative_offset) + 1
    end = text.find("\n", relative_offset)
    return start, len(text) if end < 0 else end


def attach_compound_result_groups(
    events: list[dict[str, Any]],
    *,
    document_id: int,
    evidence_text: str,
    evidence_start: int,
    row_id: str | None,
) -> list[dict[str, Any]]:
    """Return copied events with stable compound membership where justified.

    Events group only when they occupy the same layout result region, the same
    visual row, or (when geometry is absent) the same exact logical line.
    Standalone events are returned without compound keys for compatibility.
    """
    copied = [dict(event) for event in events]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    contexts: dict[tuple[Any, ...], str] = {}

    for event in copied:
        region_id = event.get("layout_region_id")
        relative = int(event["text_offset_start"]) - evidence_start
        if region_id:
            key = ("region", str(region_id))
            context = str(event.get("raw_text", "")).strip()
        elif row_id:
            key = ("row", row_id)
            context = evidence_text.strip()
        else:
            line_start, line_end = _line_bounds(evidence_text, relative)
            key = ("line", evidence_start + line_start, evidence_start + line_end)
            context = evidence_text[line_start:line_end].strip()
        groups[key].append(event)
        contexts[key] = context

    eligible_groups: list[tuple[tuple[Any, ...], list[dict[str, Any]]]] = []
    for key, members in groups.items():
        members.sort(key=lambda event: (
            int(event["text_offset_start"]), int(event["text_offset_end"]),
            str(event["outcome"]),
        ))
        runs: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for member in members:
            if current:
                left = int(current[-1]["text_offset_end"]) - evidence_start
                right = int(member["text_offset_start"]) - evidence_start
                if not CONNECTOR_RE.fullmatch(evidence_text[left:right]):
                    if len(current) > 1:
                        runs.append(current)
                    current = []
            current.append(member)
        if len(current) > 1:
            runs.append(current)
        eligible_groups.extend((key, run) for run in runs)

    for key, members in eligible_groups:
        group_start = int(members[0]["text_offset_start"])
        group_end = int(members[-1]["text_offset_end"])
        payload = {
            "version": "compound-result/1.0",
            "document_id": document_id,
            "scope": list(key),
            "span": [group_start, group_end],
            "members": [
                [member["text_offset_start"], member["text_offset_end"], member["outcome"]]
                for member in members
            ],
            "qualifier_context": contexts[key],
        }
        identity = "crg:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        for index, member in enumerate(members, 1):
            member.update({
                "compound_result_group_id": identity,
                "compound_result_member_index": index,
                "compound_result_member_count": len(members),
                "compound_result_qualifier_context": contexts[key],
                "compound_result_span_start": group_start,
                "compound_result_span_end": group_end,
            })
    return copied
