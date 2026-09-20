#!/usr/bin/env python3
"""
sample_unknown_lifecycle.py — Show a random sample of "unknown" lifecycle items
for pattern discovery.  Reads the text and metadata so you can find recurring
signal language that the classifier doesn't yet catch.

Usage:
    python3 scripts/analytics/sample_unknown_lifecycle.py [--body bos] [--n 50] [--full]
"""

import sys, os, random
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from db import get_engine
from sqlalchemy import text

BODIES = ["bos", "phoenix-cc", "phoenix-ti", "phoenix-ps", "chandler-cc", "tempe-cc",
          "mesa-city-council", "glendale-cc", "scottsdale-cc", "el-mirage-cc"]

def main():
    body_arg = sys.argv[sys.argv.index("--body") + 1] if "--body" in sys.argv else None
    n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 50
    full_text = "--full" in sys.argv

    engine = get_engine()
    with engine.connect() as conn:
        if body_arg:
            bodies = [body_arg]
        else:
            bodies = BODIES

        for body in bodies:
            # Pull random unknown items for this body
            rows = conn.execute(text("""
                SELECT id, meeting_id, agenda_item_number, agenda_item_title,
                       agenda_item_text, agenda_category, item_type
                FROM agenda_items
                WHERE body = :body
                  AND (lifecycle_status IS NULL OR lifecycle_status = ''
                       OR lifecycle_status = 'unknown')
                  AND LENGTH(agenda_item_text) > 50
                ORDER BY RANDOM()
                LIMIT :n
            """), {"body": body, "n": n}).fetchall()

            if not rows:
                print(f"\n=== {body}: no unknown items found ===")
                continue

            print(f"\n=== {body}: {len(rows)} unknown items sampled ===")
            for r in rows:
                item_id = r[0]
                title = (r[3] or "")[:80]
                text_val = r[4] or ""
                category = r[5] or ""
                item_type = r[6] or ""

                print(f"\n--- ID={item_id} | {category:20s} | {item_type:15s} | {title}")
                if full_text:
                    # Show up to 500 chars
                    snippet = text_val[:500]
                    if len(text_val) > 500:
                        snippet += "..."
                    print(f"  TEXT: {snippet}")
                else:
                    # Show first 150 chars
                    print(f"  TEXT: {text_val[:150].strip()}")


if __name__ == "__main__":
    main()
