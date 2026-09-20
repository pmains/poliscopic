#!/usr/bin/env python3
"""``stage2_s2_adjudication_markdown.py`` — render the decision packet for humans.

The JSON packet is the artifact of record; this renders the same groups as a
readable brief.  It adds nothing: no candidate is preferred, no link is
proposed, and every group carries explicit choose-or-hold fields for a human to
fill in.  Regenerating it from the packet is deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402

__all__ = ["render", "write_markdown"]

RENDER_VERSION = "kg-stage2-s2-adjudication-md/1.0"


def _cell(value: Any) -> str:
    """Table-safe text: no pipes, no newlines, never empty."""
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\n", " ").strip()
    return text or "—"


def _group_section(index: int, group: Mapping[str, Any]) -> list[str]:
    key = group["group_key"]
    lines = [
        f"### Group {index}: `{_cell(key['body'])}` · meeting `{key['meeting_db_id']}` "
        f"· item number `{_cell(key['agenda_item_number'])}`",
        "",
        f"**{len(group['documents'])} document(s)** face **{group['candidate_count']} "
        f"candidate(s)** — nothing stored on the rows separates them.",
        "",
        "| candidate `agenda_item_db_id` | source key | section level | sort order | item type | title |",
        "|---|---|---|---|---|---|",
    ]
    for candidate in group["candidates"]:
        lines.append(
            f"| `{candidate['agenda_item_db_id']}` | `{_cell(candidate['source_key'])}` "
            f"| {_cell(candidate['section_level'])} | {_cell(candidate['sort_order'])} "
            f"| {_cell(candidate['item_type'])} | {_cell(candidate['title'])} |"
        )
    lines += ["", "Documents awaiting the decision:", "",
              "| document id | source key | type | title |", "|---|---|---|---|"]
    for document in group["documents"]:
        lines.append(
            f"| `{document['document_id']}` | `{_cell(document['source_key'])}` "
            f"| {_cell(document['document_type'])} | {_cell(document['document_title'])} |"
        )
    lines += [
        "",
        "**Decision** (fill one; no option is preselected):",
        "",
        "- [ ] choose `agenda_item_db_id` = `________` (must be one of the candidates above)",
        "- [ ] hold — reason: `________________________________`",
        "",
        f"- decided by: `________`  ·  date: `________`  ·  group status: "
        f"`{group['status']}`",
        "",
    ]
    return lines


def render(packet: Mapping[str, Any], packet_path: str) -> str:
    """Render every group in the packet.  Deterministic and complete."""
    counts = packet["counts"]
    lines = [
        "# Stage 2 S2 — agenda-item attachment decisions",
        "",
        f"Packet `{packet.get('packet_id')}` · renderer `{RENDER_VERSION}`",
        "",
        f"- plan `{packet['plan']['plan_id']}` digest `{packet['plan']['digest']}`",
        f"- packet `{Path(packet_path).name}` digest `{artifacts.recorded_digest(packet)}`",
        f"- **{counts['ambiguous_documents']} documents** in **{counts['groups']} groups** "
        f"across **{counts['meetings']} meetings** and **{counts['bodies']} bodies**, "
        f"**{counts['candidates_in_total']} candidates** in total",
        "",
        "Every group below is a genuine ambiguity: the item number matches several",
        "canonical items and the document's own source key names none of them. This",
        "document proposes no link. `decisions` in the packet is empty by construction.",
        "",
        "**Decision contract**",
        "",
    ]
    lines += [f"- required: {item}" for item in packet["decision_contract"]["required"]]
    lines += [f"- forbidden: {item}" for item in packet["decision_contract"]["forbidden"]]
    lines += ["", "---", ""]
    for index, group in enumerate(packet["groups"], start=1):
        lines += _group_section(index, group)
    lines += [
        "---",
        "",
        f"Groups rendered: **{len(packet['groups'])}** of {counts['groups']}.",
        "A completed decision set belongs in a new packet revision, not an edit of this one.",
        "",
    ]
    return "\n".join(lines)


def write_markdown(packet: Mapping[str, Any], packet_path: str, out_dir: str | Path) -> tuple[Path, str]:
    """Write the summary immutably beside the packet."""
    stem = Path(packet_path).stem
    directory = Path(out_dir)
    body = render(packet, packet_path)
    digest = artifacts.write_immutable(directory / f"{stem}.md", {"markdown": body})
    return directory / f"{stem}.md", digest


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator path
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", required=True)
    parser.add_argument("--out-dir", default="data/kg-plans")
    args = parser.parse_args(argv)

    packet = artifacts.load_verified(args.packet)
    path, digest = write_markdown(packet, args.packet, args.out_dir)
    print(json.dumps({"markdown": str(path), "digest": digest,
                      "groups": len(packet["groups"])}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
