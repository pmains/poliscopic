#!/usr/bin/env python3
"""
bos_exec_session_analysis.py — Read-only analysis of Maricopa County Board of
Supervisors executive session agenda items (Jan 2024 → present).

Input : data/analysis/bos-exec-items.csv (extracted from dev PostgreSQL)
Output: data/analysis/bos-exec-analysis.json + printed summary

Classifies each item by:
  - statutory basis parsed from the item text (A.R.S. §38-431.03 subsections)
  - subject category (litigation, personnel, land, procurement, policy/admin,
    other) via keyword heuristics
Flags items cited under (A)(3) LEGAL ADVICE whose subject does not look like
litigation / legal-advice work — candidates for "not bona fide legal advice."

Read-only: never writes to the database.
"""
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

CSV_PATH = Path("data/analysis/bos-exec-items.csv")
OUT_PATH = Path("data/analysis/bos-exec-analysis.json")

# ── Statutory citation parsing ────────────────────────────────────────
CITE_RE = re.compile(r"38-431\.03\(A\)\((\d+)\)", re.IGNORECASE)

def parse_cites(text: str) -> list[str]:
    return sorted({m.group(1) for m in CITE_RE.finditer(text)})

# ── Subject classification ────────────────────────────────────────────
LITIGATION_PAT = re.compile(
    r"\bV\s*\.|\bVS\s*\.|LITIGATION|LAWSUIT|APPEAL|SETTLEMENT|INJUNCTION|"
    r"PETITION|CLAIM(S)?\b|SUBPOENA|DEPOSITION|DISCOVERY|ARBITRATION|"
    r"JUDGMENT|LIEN|TAKINGS|CONDEMNATION|EMINENT DOMAIN|"
    r"DISTRICT COURT|SUPERIOR COURT|COURT OF APPEALS|NO\.\s*\d{2}-",
    re.IGNORECASE,
)
PERSONNEL_PAT = re.compile(
    r"SALARY|PERSONNEL|EMPLOYMENT|HIRING|DISMISSAL|DISCIPLINE|"
    r"PERFORMANCE EVALUATION|APPOINTMENT|PROMOTION|DEMOTION|RESIGNATION|"
    r"EMPLOYEE|COMPENSATION|CHIEF DEPUTY|RECRUITMENT",
    re.IGNORECASE,
)
LAND_PAT = re.compile(
    r"REAL PROPERTY|PURCHASE.*(PROPERTY|LAND)|SALE.*(PROPERTY|LAND)|"
    r"LEASE|ACQUISITION|EASEMENT|APN\b|DEED|RIGHTS?[- ]OF[- ]WAY|"
    r"PROPERTY.*(ACQUISITION|SALE|PURCHASE)",
    re.IGNORECASE,
)
PROCUREMENT_PAT = re.compile(
    r"PROCUREMENT|CONTRACT|RFP\b|RFQ\b|BID\b|COMPETITION IMPRACTICABLE|"
    r"SOLE SOURCE|VENDOR|AGREEMENT",
    re.IGNORECASE,
)
POLICY_PAT = re.compile(
    r"POLICY|TAX\b|TAXES|LEVY|BUDGET|EXPENDITURE|AUTHORITY AND RESPONSIBIL|"
    r"RESPONSIBILITIES|REPORT\b|UPDATE|TECHNOLOGY|RICO|FLOODPLAIN|FLOOD CONTROL|"
    r"ZONING|ORDINANCE|REGULATION|RATES?|FEES?|PROGRAM|GRANT|STUDY\b|"
    r"STRATEGY|PLAN\b|PLANNING|HEALTH\b|AIR QUALITY|PM 2\.5|PM10|WATER",
    re.IGNORECASE,
)

def classify(title: str, text: str) -> dict:
    blob = f"{title} {text}"
    return {
        "litigation": bool(LITIGATION_PAT.search(blob)),
        "personnel": bool(PERSONNEL_PAT.search(blob)),
        "land": bool(LAND_PAT.search(blob)),
        "procurement": bool(PROCUREMENT_PAT.search(blob)),
        "policy_admin": bool(POLICY_PAT.search(blob)),
    }

def norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", t.upper()).strip()

def main() -> None:
    if not CSV_PATH.exists():
        sys.exit(f"Missing {CSV_PATH} — re-run the psql \\copy first")
    rows = list(csv.DictReader(CSV_PATH.open()))
    print(f"Loaded {len(rows)} rows")

    # Dedupe by (meeting_date, normalized title) — same item can appear via
    # multiple source documents (BOS + Special Districts executive agendas).
    seen: set[tuple[str, str]] = set()
    items = []
    for r in rows:
        key = (r["meeting_date"], norm_title(r["agenda_item_title"]))
        if key in seen:
            continue
        seen.add(key)
        items.append(r)
    print(f"Unique items after dedupe: {len(items)}")

    # Parse citations + classify
    for r in items:
        text = r["agenda_item_text"] or ""
        r["cites"] = parse_cites(text)
        r["cls"] = classify(r["agenda_item_title"], text)

    # ── Summaries ──
    by_year = Counter(r["meeting_date"][:4] for r in items)
    cite_counter = Counter(c for r in items for c in r["cites"])
    meetings = len({(r["meeting_date"], r["meeting_id"]) for r in items})

    # A(3)-only items that look like policy/admin/procurement (not litigation
    # and not personnel/land which have their own valid exceptions)
    flagged = []
    for r in items:
        cls = r["cls"]
        if "3" in r["cites"] and "1" not in r["cites"] and "4" not in r["cites"] and "5" not in r["cites"]:
            if cls["litigation"]:
                continue  # genuine legal-advice look
            if cls["policy_admin"] or cls["procurement"]:
                flagged.append(r)

    # ── Theme clusters for the flagged set ──
    THEMES = [
        ("property_tax_levy", re.compile(r"PROPERTY TAX|TAX LEVY|TAX RATES|CASH DEFICIT", re.I)),
        ("budget_finance", re.compile(r"BUDGET|EXPENDITURE LIMIT|OFFICE OF BUDGET|FINANCE", re.I)),
        ("rico", re.compile(r"RICO", re.I)),
        ("constable_sheriff_equipment", re.compile(r"CONSTABLE|SHERIFF|VEHICLE|EQUIPMENT|UPGRADE", re.I)),
        ("technology_it", re.compile(r"TECHNOLOGY|IT UPDATE|CYBER|SOFTWARE|SYSTEMS? UPDATE", re.I)),
        ("procurement_contracts", re.compile(r"PROCUREMENT|COMPETITION IMPRACTICABLE|CONTRACT|SOLE SOURCE|RFP", re.I)),
        ("board_authority_general", re.compile(r"BOARD AUTHORITY|RESPONSIBILIT", re.I)),
        ("public_health_air", re.compile(r"PUBLIC HEALTH|AIR QUALITY|PM 2\.5|PM10|EMISSION", re.I)),
        ("flood_zoning_landuse", re.compile(r"FLOOD|ZONING|FLOODPLAIN|LAND USE|CODE ENFORCEMENT", re.I)),
        ("compensation_benefits", re.compile(r"COMPENSATION|DEFERRED COMP|PENSION|RETIREMENT|BENEFIT", re.I)),
        ("debt_securities", re.compile(r"BOND|DEBT|SECURITIES|UNDERWRIT", re.I)),
        ("misc", re.compile(r"", re.I)),
    ]
    theme_groups = defaultdict(list)
    for r in flagged:
        blob = f"{r['agenda_item_title']} {r['agenda_item_text'] or ''}"
        for name, pat in THEMES:
            if pat.search(blob):
                theme_groups[name].append(r)
                break

    # ── Output ──
    out = {
        "rows_loaded": len(rows),
        "unique_items": len(items),
        "distinct_meetings_with_exec": meetings,
        "by_year": dict(sorted(by_year.items())),
        "citation_counts": dict(sorted(cite_counter.items())),
        "flagged_count": len(flagged),
        "flagged": [
            {
                "meeting_date": r["meeting_date"],
                "meeting_id": r["meeting_id"],
                "item": r["agenda_item_number"],
                "title": r["agenda_item_title"],
                "cites": r["cites"],
                "text_excerpt": (r["agenda_item_text"] or "")[:500],
                "source_url": r["source_url"],
            }
            for r in sorted(flagged, key=lambda r: r["meeting_date"])
        ],
    }
    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"Wrote {OUT_PATH}")
    print(f"Distinct meetings with exec sessions: {meetings}")
    print(f"By year: {dict(sorted(by_year.items()))}")
    print(f"Citation counts: {dict(sorted(cite_counter.items()))}")
    print(f"Flagged (A3-only, non-litigation, policy/procurement-looking): {len(flagged)}")
    print("\nTheme clusters (flagged items):")
    for name, members in sorted(theme_groups.items(), key=lambda kv: -len(kv[1])):
        print(f"  {name}: {len(members)}")

if __name__ == "__main__":
    main()
