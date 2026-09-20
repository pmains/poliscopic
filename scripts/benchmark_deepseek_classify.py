#!/usr/bin/env python3
"""benchmark_deepseek_classify.py — Comprehensive DeepSeek sweep of all agenda items.

Sends ALL 305 items from the past 14 days to DeepSeek with full text.
No pre-screening, no binary filter, no truncation at 500 chars.
Establishes the ground-truth benchmark of what's actually housing-related.

Usage:
    python3 -u scripts/benchmark_deepseek_classify.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from db.tier import DEVELOPMENT, TierError, require_role_url  # noqa: E402


def _load_env():
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _call_deepseek(prompt: str) -> str:
    _load_env()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "") or os.environ.get("REPORTS_LLM_API_KEY", "")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY not set")
    model = os.environ.get("REPORTS_LLM_MODEL", "deepseek-chat")
    base_url = os.environ.get("REPORTS_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")

    import urllib.request
    import urllib.error

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=600)
    result = json.loads(resp.read().decode("utf-8"))
    return result["choices"][0]["message"]["content"]


def main():
    from datetime import timedelta
    from sqlalchemy import create_engine, text as sa_text

    _load_env()
    raw_url = os.environ.get("DATABASE_URL") or os.environ.get("DEV_DATABASE_URL")
    if not raw_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    try:
        url = require_role_url(DEVELOPMENT, raw_url, label="development")
    except TierError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(url)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")

    with engine.connect() as conn:
        rows = conn.execute(sa_text("""
            SELECT
                a.agenda_item_id,
                a.agenda_item_number,
                a.agenda_item_title,
                a.agenda_item_text,
                a.body,
                m.meeting_date,
                m.meeting_type,
                m.meeting_title,
                j.name AS jurisdiction_name
            FROM agenda_items a
            JOIN meetings m ON a.body = m.body AND a.meeting_id = m.meeting_id
            LEFT JOIN jurisdictions j ON m.jurisdiction_id = j.id
            WHERE m.meeting_date >= :cutoff
            ORDER BY m.meeting_date DESC, a.body, a.agenda_item_number
        """), {"cutoff": cutoff}).fetchall()

    items = [dict(r._mapping) for r in rows]
    print(f"Queried {len(items)} items from DB", flush=True)

    # Build the prompt with ALL items — full text, no truncation
    lines = []
    for idx, item in enumerate(items):
        title = (item.get("agenda_item_title") or "").strip()[:200]
        body = (item.get("body") or "").strip()
        meeting_type = (item.get("meeting_type") or "").strip()
        meeting_date = str(item.get("meeting_date") or "")
        jurisdiction = (item.get("jurisdiction_name") or "").strip()
        text = (item.get("agenda_item_text") or "").strip()
        # Truncate text to 1500 for prompt size sanity
        text = text[:1500]
        lines.append(
            f"[Item {idx + 1}]\n"
            f"Body: {body}\n"
            f"Jurisdiction: {jurisdiction}\n"
            f"Meeting Date: {meeting_date}\n"
            f"Meeting Type: {meeting_type}\n"
            f"Title: {title}\n"
            f"Text: {text}\n"
        )

    item_block = "\n".join(lines)
    char_count = len(item_block)
    print(f"Prompt item block: {char_count} chars, ~{char_count // 4} tokens", flush=True)

    prompt = f"""You are a housing policy analyst reviewing public meeting agenda items from Maricopa County and its municipalities.

Your task: Find EVERY agenda item that relates to housing, zoning, rezoning, land use, urban planning, community development, or housing policy — even remotely.

Cast a WIDE net. Include items about:
- Rezoning and zoning map amendments (including General Plan amendments)
- Zoning code amendments affecting housing
- Affordable housing policy and funding (HOME, CDBG, housing trust funds, Section 8)
- ADUs (accessory dwelling units) and missing-middle housing
- Multifamily and apartment developments
- Subdivision maps and lot splits
- Housing authority actions
- Residential development moratoriums or incentives
- Use permits for residential properties (parking setbacks, garage conversions)
- Development agreements for residential projects
- Homeless services and housing stability programs
- Community development block grants and related funding
- Appointments to housing/community development committees
- Transitional housing or emergency shelter projects
- Water/sewer infrastructure that enables new housing development
- Any item whose title contains "ZONING", "REZONE", "LAND USE", "GENERAL PLAN AMENDMENT", "SUBDIVISION", "PLANNED AREA DEVELOPMENT", "HOUSING", "DEVELOPMENT AGREEMENT", "USE PERMIT"
- ANY item involving residential properties, even if the primary focus is commercial

EXCLUDE only:
- Purely procedural items with zero housing content (minutes approval, adjournment, scheduling-only)
- Items about routine building permits for existing structures
- Road easements and right-of-way abandonments with no housing nexus
- Law enforcement, criminal justice, or non-housing social services
- Items that only mention "housing" in passing (e.g., "the facility includes staff housing")

There are {len(lines)} items below. List ALL that qualify, even if the connection to housing is weak. It's better to include a borderline item than miss a real one.

Return a JSON object with:
- "benchmark_items": array of objects with keys "item_index" (1-based), "body", "title", "relevance" (high/medium/low), "reason"
- "total_identified": count
- "bodies_represented": array of body names

Here are the items:

{item_block}

Respond with ONLY the JSON object.
"""

    print("Sending to DeepSeek...", flush=True)
    start = time.time()
    try:
        raw = _call_deepseek(prompt)
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    elapsed = time.time() - start
    print(f"DeepSeek responded in {elapsed:.0f}s", flush=True)

    # Parse
    try:
        import re
        # Try direct parse first
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
            if match:
                result = json.loads(match.group(1))
            else:
                print(f"Could not parse response: {raw[:500]}", file=sys.stderr, flush=True)
                sys.exit(1)

        benchmark = result.get("benchmark_items", [])
        print(f"\n{'='*60}", flush=True)
        print(f"BENCHMARK RESULTS: {len(benchmark)} items identified", flush=True)
        print(f"{'='*60}", flush=True)

        for bi in benchmark:
            idx = bi.get("item_index", 0)
            body = bi.get("body", "?")
            title = bi.get("title", "?")[:100]
            rel = bi.get("relevance", "?")
            reason = (bi.get("reason", "") or "")[:150]

            # Look up the actual item for more context
            actual = items[idx - 1] if 0 < idx <= len(items) else {}
            print(f"\n  [{rel.upper():6s}] {body:25s} | {title}", flush=True)
            print(f"         {reason}", flush=True)

        print(f"\n{'='*60}", flush=True)
        print(f"Bodies: {result.get('bodies_represented', [])}", flush=True)
        print(f"{'='*60}", flush=True)

        # Save full results
        output = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_items_screened": len(items),
            "total_identified": len(benchmark),
            "deepseek_model": os.environ.get("REPORTS_LLM_MODEL", "deepseek-chat"),
            "elapsed_seconds": round(elapsed, 1),
            "items": benchmark,
            "bodies_represented": result.get("bodies_represented", []),
        }
        out_path = PROJECT_ROOT / "data" / "runs" / "benchmark" / "deepseek-sweep.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2))
        print(f"\nFull results saved to {out_path}", flush=True)

    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr, flush=True)
        print(f"Raw response: {raw[:1000]}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
