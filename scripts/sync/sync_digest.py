#!/usr/bin/env python3
"""
sync_digest.py — Daily data-quality digest email (Brief 007).

Consolidates scrape/sync/data-quality issues for the administrator into one
plain-text email. Subscriber newsletters do NOT carry this content; this is
the ops-facing counterpart.

Sections (only non-empty sections render):
  1. Scrape summary            (data/sync/YYYY-MM-DD-summary.txt)
  2. Sync failures (24h)       (data/sync/YYYY-MM-DD-monitor.txt + errors.txt)
  3. Stale-complete meetings   (DB: complete + 0 items + upcoming/recent date)
  4. Prod sync staleness       (DB: _sync_meta per-table last_sync_at age)
  5. Entity pipeline health    (DB: _detect_entities_watermark)
  6. Newsletter/workflow health (data/runs/<topic>/ newest run state)
  7. Manual review queue       (monitor.txt)
  8. Orphans / no-sync backlog (monitor.txt)
  9. Pending / no-agenda       (monitor.txt)
 10. Source-change warnings    (errors.txt 404/410/timeout patterns)

Usage:
    .venv/bin/python scripts/sync/sync_digest.py                # dry-run (prints)
    .venv/bin/python scripts/sync/sync_digest.py --send          # email it
    .venv/bin/python scripts/sync/sync_digest.py --date 2026-08-25
"""

import argparse
import datetime as _dt
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_SYNC = PROJECT_ROOT / "data" / "sync"
RUNS_DIR = PROJECT_ROOT / "data" / "runs"
ENV_PATH = PROJECT_ROOT / ".env"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "sync"))


# ── .env / SMTP ────────────────────────────────────────────────────────────

def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def send_email(subject: str, body: str, env: dict) -> None:
    pw = env.get("EMAIL_APP_PASSWORD", "")
    if not pw:
        raise RuntimeError("EMAIL_APP_PASSWORD not in .env")
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = "Poliscopic <contact@poliscopic.com>"
    recipient = env.get("POLISCOPIC_DIGEST_RECIPIENT", "").strip()
    if not recipient:
        raise RuntimeError("POLISCOPIC_DIGEST_RECIPIENT not in .env")
    msg["To"] = recipient
    with smtplib.SMTP("mail.privateemail.com", 587, timeout=30) as server:
        server.starttls()
        server.login("contact@poliscopic.com", pw)
        server.send_message(msg)


# ── Artifact readers ───────────────────────────────────────────────────────

def read_file(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def parse_summary(text: str) -> dict:
    d = {}
    for line in text.splitlines():
        m = re.match(r"^([a-z_]+):\s*(.+)$", line.strip())
        if m:
            d[m.group(1)] = m.group(2)
    return d


def section_counts(monitor_text: str) -> dict:
    counts = {}
    for key in [
        "Total meetings", "Complete", "Pending", "Failed (all time)",
        "Failed (last 24h)", "No agenda", "Manual review",
        "In progress", "Stuck in progress (>2h)",
        "Orphans (no sync, >7 days old)",
    ]:
        m = re.search(rf"^\s*{re.escape(key)}:\s*([\d,]+)", monitor_text, re.M)
        if m:
            counts[key] = int(m.group(1).replace(",", ""))
    return counts


def failed_24h_lines(monitor_text: str) -> list[str]:
    # Capture the table after "Failed in Last 24h:" until next blank/section
    m = re.search(r"Failed in Last 24h:\s*\n(.*?)(?:\n\s*\n|\Z)", monitor_text, re.S)
    if not m:
        return []
    lines = [l for l in m.group(1).splitlines() if l.strip() and "----" not in l]
    return lines[1:] if lines and lines[0].startswith("body") else lines


def manual_review_lines(monitor_text: str) -> list[str]:
    m = re.search(r"Remaining Issues \(require human attention\):\s*\n(.*?)(?:\n\s*\n|\Z)", monitor_text, re.S)
    if not m:
        return []
    return [l.strip(" ••") for l in m.group(1).splitlines() if l.strip()]


def error_details(errors_text: str) -> list[str]:
    m = re.search(r"Error details \(first 50\):\s*\n(.*)", errors_text, re.S)
    if not m:
        return []
    return [l for l in m.group(1).splitlines() if l.strip()][:10]


# ── DB queries ─────────────────────────────────────────────────────────────

def db_queries() -> dict:
    from sqlalchemy import text
    from db import get_engine
    eng = get_engine()
    out = {}

    # Stale-complete: complete + 0 items + upcoming (or yesterday) meeting
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT body, meeting_id, meeting_date, meeting_type, last_synced_at "
            "FROM meetings "
            "WHERE sync_status='complete' AND item_count_actual=0 "
            "AND meeting_date >= to_char(CURRENT_DATE - 1, 'YYYY-MM-DD') "
            "ORDER BY meeting_date LIMIT 25"
        )).fetchall()
        out["stale_complete"] = [dict(r._mapping) for r in rows]

        rows = conn.execute(text(
            "SELECT phase, last_run_at, duration_s, entities_created, edges_created "
            "FROM _detect_entities_watermark ORDER BY phase"
        )).fetchall()
        out["entity_watermarks"] = [dict(r._mapping) for r in rows]
    return out


def prod_sync_meta() -> list[dict]:
    """Read _sync_meta checkpoints from the PROD database.

    sync_prod.py stores its checkpoints in a _sync_meta table ON PROD — the
    dev _sync_meta table is a stale leftover and must NOT be used for prod
    staleness (Brief 009). Read-only SELECT via PROD_DATABASE_URL.
    """
    from sqlalchemy import create_engine, text
    env = load_env()
    url = env.get("PROD_DATABASE_URL", "")
    if not url:
        return [{"error": "PROD_DATABASE_URL not in .env"}]
    try:
        eng = create_engine(url, connect_args={"connect_timeout": 15})
        with eng.connect() as conn:
            rows = conn.execute(text(
                "SELECT table_name, last_sync_at FROM _sync_meta "
                "ORDER BY last_sync_at LIMIT 20"
            )).fetchall()
        # Filter to tables that still exist in dev — prod's _sync_meta keeps
        # leftover checkpoint rows for renamed/dropped tables (e.g.
        # meeting_supervisors → meeting_members), which are not real staleness.
        from db import get_engine
        with get_engine().connect() as dconn:
            dev_tables = {
                r[0] for r in dconn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public'"
                )).fetchall()
            }
        return [dict(r._mapping) for r in rows if r.table_name in dev_tables]
    except Exception as e:
        return [{"error": f"prod unreachable: {e}"}]


# ── Workflow health ────────────────────────────────────────────────────────

TOPICS = ["housing", "public-safety", "boards-commissions", "transportation", "water-environment"]


def entity_run_health() -> list[str]:
    """Surface failed entity-pipeline phases / failed verification gate.

    Reads the newest data/sync/entity-run-*.json written by
    detect_entities.py (Brief 016 Step 2 per-phase state). Returns a list of
    warning lines; empty when the latest run is clean.
    """
    if not DATA_SYNC.is_dir():
        return []
    runs = sorted(DATA_SYNC.glob("entity-run-*.json"), reverse=True)
    if not runs:
        return []
    latest = None
    state = None
    unreadable = None
    for candidate in runs:
        try:
            candidate_state = json_loads(candidate)
        except Exception:
            unreadable = candidate
            continue
        if candidate_state.get("dry_run"):
            continue
        latest = candidate
        state = candidate_state
        break
    if state is None:
        if unreadable is not None:
            return [f"entity pipeline: unreadable run state {unreadable.name}"]
        return []
    out = []
    failed_phases = [p for p in state.get("phases", []) if p.get("status") == "failed"]
    for p in failed_phases:
        out.append(f"  phase {p['name']} FAILED"
                   + (f" — {str(p.get('error'))[:120]}" if p.get("error") else "")
                   + (f" (attempts {p.get('attempts')})" if p.get("attempts") else ""))
    gate = state.get("gate") or {}
    if gate.get("failed"):
        for ch in gate.get("checks", []):
            if not ch.get("ok"):
                out.append(f"  gate ✗ {ch.get('check')}: {ch.get('detail')}")
    return out



def workflow_health() -> list[str]:
    lines = []
    for topic in TOPICS:
        tdir = RUNS_DIR / topic
        if not tdir.is_dir():
            continue
        # Newest run dir by name (YYYY-MM-DD--HHMMSS), skip archive-*
        runs = sorted(
            [d for d in tdir.iterdir() if d.is_dir() and not d.name.startswith(("archive", "archived"))],
            key=lambda d: d.name, reverse=True,
        )
        if not runs:
            continue
        latest = runs[0]
        wf = latest / "workflow.state"
        steps = latest / "steps"
        if wf.exists():
            try:
                st = json_loads(wf)
                status = st.get("status", "?")
                err = st.get("error", "")
                lines.append(f"{topic}: {latest.name} — {status}" + (f" ({err[:80]})" if err else ""))
            except Exception:
                lines.append(f"{topic}: {latest.name} — unreadable workflow.state")
        elif steps.is_dir():
            failed = [f.stem for f in steps.glob("*.state") if "failed" in f.read_text()[:200]]
            if failed:
                lines.append(f"{topic}: {latest.name} — failed steps: {', '.join(failed)}")
            else:
                lines.append(f"{topic}: {latest.name} — no workflow.state, steps: "
                             f"{', '.join(sorted(f.stem for f in steps.glob('*.state')))}")
        else:
            lines.append(f"{topic}: {latest.name} — no state files")
    return lines


def json_loads(p: Path):
    import json
    return json.loads(p.read_text())


# ── Compose ────────────────────────────────────────────────────────────────

def compose(date_str: str) -> tuple[str, str]:
    summary_text = read_file(DATA_SYNC / f"{date_str}-summary.txt")
    monitor_text = read_file(DATA_SYNC / f"{date_str}-monitor.txt")
    errors_text = read_file(DATA_SYNC / f"{date_str}-errors.txt")

    summary = parse_summary(summary_text)
    counts = section_counts(monitor_text)
    failed = failed_24h_lines(monitor_text)
    review = manual_review_lines(monitor_text)
    errs = error_details(errors_text)
    db = db_queries()
    db["prod_sync"] = prod_sync_meta()  # read checkpoints from PROD (Brief 009)
    wf_lines = workflow_health()

    body_parts = [f"=== Poliscopic Data Quality Digest — {date_str} ==="]

    # Delta vs yesterday's monitor
    y_date = (_dt.date.fromisoformat(date_str) - _dt.timedelta(days=1)).isoformat()
    y_counts = section_counts(read_file(DATA_SYNC / f"{y_date}-monitor.txt"))
    deltas = []
    for k in ["Failed (last 24h)", "Manual review", "Orphans (no sync, >7 days old)"]:
        if k in counts and k in y_counts and counts[k] != y_counts[k]:
            deltas.append(f"{k}: {y_counts[k]} → {counts[k]}")
    if deltas:
        body_parts.append("Changed since yesterday: " + "; ".join(deltas))

    # 1. Scrape summary
    if summary:
        body_parts.append("\n[1] Scrape summary")
        for k in ["completion_status", "exit_code", "error_count", "duration_seconds",
                  "meetings_synced_this_run", "new_meetings_discovered", "new_agenda_items"]:
            if k in summary:
                body_parts.append(f"  {k}: {summary[k]}")

    # 2. Sync failures (24h)
    if failed:
        body_parts.append("\n[2] Sync failures (last 24h)")
        body_parts.extend(f"  {l}" for l in failed[:10])

    # 3. Stale-complete meetings
    if db["stale_complete"]:
        body_parts.append("\n[3] Stale-complete meetings (complete + 0 items)")
        for r in db["stale_complete"][:15]:
            body_parts.append(f"  {r['body']} {r['meeting_date']} {r['meeting_type']} "
                              f"(meeting_id={r['meeting_id']}, synced {str(r['last_synced_at'])[:16]})")

    # 4. Prod sync staleness (flag > 48h)
    stale_tables = []
    now = _dt.datetime.now(_dt.timezone.utc)
    for r in db["prod_sync"]:
        if r["last_sync_at"]:
            age = now - r["last_sync_at"]
            if age > _dt.timedelta(hours=48):
                days = round(age.total_seconds() / 86400, 1)
                stale_tables.append(f"  {r['table_name']}: {days}d stale (last {str(r['last_sync_at'])[:16]})")
    if stale_tables:
        body_parts.append("\n[4] ⚠ PROD SYNC STALENESS (>48h)")
        body_parts.extend(stale_tables[:15])

    # 5. Entity pipeline health
    stale_phases = []
    for r in db["entity_watermarks"]:
        if r["last_run_at"]:
            age = now - r["last_run_at"]
            if age > _dt.timedelta(hours=48):
                stale_phases.append(f"  {r['phase']}: {round(age.total_seconds()/3600,1)}h ago "
                                    f"(created {r['entities_created']}, edges {r['edges_created']})")
    if stale_phases:
        body_parts.append("\n[5] ⚠ ENTITY PIPELINE STALE (>48h)")
        body_parts.extend(stale_phases)
    elif db["entity_watermarks"]:
        body_parts.append("\n[5] Entity pipeline")
        for r in db["entity_watermarks"]:
            body_parts.append(f"  {r['phase']}: {str(r['last_run_at'])[:16]} "
                              f"(created {r['entities_created']}, edges {r['edges_created']})")

    # 5b. Entity run state — failed phases / failed verification gate (Brief 016)
    entity_lines = entity_run_health()
    if entity_lines:
        body_parts.append("\n[5b] ⚠ ENTITY PIPELINE RUN FAILURES")
        body_parts.extend(entity_lines)

    # 6. Newsletter/workflow health
    if wf_lines:
        body_parts.append("\n[6] Newsletter workflow health")
        body_parts.extend(f"  {l}" for l in wf_lines)

    # 7. Manual review queue
    if review:
        body_parts.append(f"\n[7] Manual review queue ({counts.get('Manual review', len(review))})")
        body_parts.extend(f"  • {l}" for l in review[:10])

    # 8. Orphans
    orphans = counts.get("Orphans (no sync, >7 days old)")
    if orphans is not None and orphans > 0:
        body_parts.append(f"\n[8] Orphans (no sync, >7 days old): {orphans}")

    # 9. Pending / no-agenda
    pend = counts.get("Pending")
    noag = counts.get("No agenda")
    if pend is not None or noag is not None:
        body_parts.append("\n[9] Pipeline state")
        if pend is not None:
            body_parts.append(f"  pending: {pend}")
        if noag is not None:
            body_parts.append(f"  no agenda: {noag}")

    # 10. Source-change warnings
    src_warnings = [l for l in errs if re.search(r"404|410|Timeout|timed out|connection", l, re.I)]
    if src_warnings:
        body_parts.append("\n[10] ⚠ Source-change warnings (404/410/timeout)")
        body_parts.extend(f"  {l[:150]}" for l in src_warnings[:8])

    # Errors tail if nothing else caught
    if errs and not (failed or src_warnings):
        body_parts.append("\n[2b] Error lines (from scrape error report)")
        body_parts.extend(f"  {l[:150]}" for l in errs[:8])

    if len(body_parts) == 1:
        body_parts.append("\nAll quiet — no issues detected.")

    body = "\n".join(body_parts)
    subject = f"Poliscopic Data Digest — {date_str}"
    return subject, body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=_dt.date.today().isoformat())
    parser.add_argument("--send", action="store_true", help="email the digest (default: dry-run print)")
    args = parser.parse_args()

    subject, body = compose(args.date)
    print(body)
    if args.send:
        env = load_env()
        send_email(subject, body, env)
        print(f"\n[digest] EMAILED to configured recipient — {subject}")
    else:
        print(f"\n[digest] DRY-RUN — subject: {subject} (pass --send to email)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
