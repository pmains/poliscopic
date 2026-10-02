#!/usr/bin/env python3
"""
Parallel sync runner — replaces run_pipeline.py.

Called by pipeline.sh (via nohup).  Handles resource-based batching,
DB health check, per-jurisdiction subprocess sync, post-sync tasks,
and state tracking.

Usage (called by pipeline.sh, not directly):
  PYTHONPATH=scripts python3 scripts/sync/runner.py --tier daily   # 3-day window
  PYTHONPATH=scripts python3 scripts/sync/runner.py --tier weekly  # 30-day window
  PYTHONPATH=scripts python3 scripts/sync/runner.py --dry-run      # Plan only
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from scraper.source_registry import (
    no_date_commands,
    schedule_group,
    scheduled_commands,
    source_by_command,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sync_runner")

# ── Project paths ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATE_DIR = PROJECT_ROOT / "data" / "sync"
STATE_FILE = STATE_DIR / "state.json"

# ── Date windows ──
DAILY_WINDOW_BACK = 3
WEEKLY_WINDOW_BACK = 30
FUTURE_WINDOW_FORWARD = 14

# ── Per-jurisdiction timeout (seconds) ──
JURISDICTION_TIMEOUT = 120

# Compatibility views for runner callers; the registry is authoritative.
GROUP_A = schedule_group("A", "daily")
GROUP_B = schedule_group("B", "daily")
GROUP_C = schedule_group("C", "daily")
GROUP_D = schedule_group("D", "daily")
ALL_TIER_JURISDICTIONS = scheduled_commands()
NO_DATE_ARGS = no_date_commands()


# ── Helpers ──

def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_args(tier: str) -> tuple[str, str]:
    """Return (start_date, end_date) for the given tier."""
    today = dt.date.today()
    if tier == "weekly":
        back = WEEKLY_WINDOW_BACK
    else:
        back = DAILY_WINDOW_BACK
    start = today - dt.timedelta(days=back)
    end = today + dt.timedelta(days=FUTURE_WINDOW_FORWARD)
    return (start.isoformat(), end.isoformat())


def _load_state() -> dict[str, Any]:
    """Load state.json or return default structure."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "tier": None,
        "started_at": None,
        "finished_at": None,
        "total_duration": None,
        "success_count": 0,
        "failure_count": 0,
        "skipped_count": 0,
        "jurisdictions": {},
        "reconciliation": {
            "last_checked_at": None,
            "escalated": False,
            "escalated_at": None,
            "error": None,
        },
        "post_sync": {
            "minutes_check": None,
            "votes_backfill": None,
            "fts_rebuild": None,
        },
        "updated_at": _timestamp(),
    }


def _save_state(state: dict[str, Any]) -> None:
    """Write state.json atomically."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _timestamp()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_FILE)


def _update_juris_state(
    state: dict[str, Any],
    name: str,
    **fields: Any,
) -> None:
    """Update fields for one jurisdiction in state."""
    if name not in state["jurisdictions"]:
        state["jurisdictions"][name] = {
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "items_synced": None,
            "meetings_found": None,
            "duration_s": None,
            "error_message": None,
            "retry_count": 0,
            "escalated": False,
        }
    for k, v in fields.items():
        state["jurisdictions"][name][k] = v


def _check_db_health() -> bool:
    """Verify DB connectivity with a 5-second timeout."""
    try:
        from db import get_engine
        from sqlalchemy import text
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        log.info("DB health check: OK")
        return True
    except Exception as e:
        log.error("DB health check FAILED: %s", e)
        return False


# ── Core sync function ──

def _build_cmd(
    juris: str,
    extra_args: list[str],
    start_date: str,
    end_date: str,
    tier: str,
) -> list[str]:
    """Build the subprocess command for a jurisdiction sync."""
    source = source_by_command(juris)
    cmd = [
        sys.executable,
        "scripts/scraper/main.py",
        source.invocation_command,
        *source.action_args,
    ]
    if source.accepts_date_range:
        cmd.append(f"--start-date={start_date}")
        cmd.append(f"--end-date={end_date}")
    cmd.extend(extra_args)
    return cmd


def run_jurisdiction(
    juris: str,
    extra_args: list[str],
    start_date: str,
    end_date: str,
    tier: str,
) -> dict[str, Any]:
    """Run a single jurisdiction sync as a subprocess and return results."""
    cmd = _build_cmd(juris, extra_args, start_date, end_date, tier)
    cmd_str = " ".join(str(c) for c in cmd)
    log.info("  Starting %s: %s", juris, cmd_str)

    env = {**os.environ, "PYTHONPATH": "scripts"}
    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=JURISDICTION_TIMEOUT,
            env=env,
            cwd=str(PROJECT_ROOT),
        )
        elapsed = time.time() - start
        exit_code = result.returncode

        # Parse summary from last line of stdout
        stdout_lines = result.stdout.strip().split("\n")
        summary = (stdout_lines[-1] if stdout_lines else "").strip()

        # Try to extract items_synced and meetings_found
        items_synced = None
        meetings_found = None
        import re
        items_m = re.search(r"(\d+)\s+agenda\s+items?\b", summary, re.I)
        if items_m:
            items_synced = int(items_m.group(1))
        meetings_m = re.search(r"(\d+)\s+meeting", summary, re.I)
        if meetings_m:
            meetings_found = int(meetings_m.group(1))

        log.info("  %s: exit=%d (%.0fs) %s", juris, exit_code, elapsed, summary[:120])

        return {
            "juris": juris,
            "exit_code": exit_code,
            "duration_s": elapsed,
            "items_synced": items_synced,
            "meetings_found": meetings_found,
            "error_message": result.stderr.strip()[:500] if result.stderr else None,
            "summary": summary,
            "cmd": cmd_str,
        }

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        log.warning("  %s: TIMEOUT (%ds)", juris, JURISDICTION_TIMEOUT)
        return {
            "juris": juris,
            "exit_code": -1,
            "duration_s": elapsed,
            "items_synced": None,
            "meetings_found": None,
            "error_message": f"TIMEOUT after {JURISDICTION_TIMEOUT}s",
            "summary": "TIMEOUT",
            "cmd": cmd_str,
        }
    except Exception as e:
        elapsed = time.time() - start
        log.error("  %s: ERROR %s", juris, e)
        return {
            "juris": juris,
            "exit_code": -2,
            "duration_s": elapsed,
            "items_synced": None,
            "meetings_found": None,
            "error_message": str(e)[:500],
            "summary": f"EXCEPTION: {e}",
            "cmd": cmd_str,
        }


def _print_plan(tier: str) -> None:
    """Print the execution plan without running anything."""
    start_date, end_date = _window_args(tier)
    print(f"=== Sync Plan (--tier={tier}) ===")
    print(f"  Window: {start_date} to {end_date}")
    print()

    print("--- Group A (Playwright serial) ---")
    for juris, extra in GROUP_A:
        cmd = _build_cmd(juris, extra, start_date, end_date, tier)
        print(f"  {' '.join(cmd)}")

    print()
    print("--- Group B (HTTP batch, up to 8 workers) ---")
    for juris, extra in schedule_group("B", tier):
        cmd = _build_cmd(juris, extra, start_date, end_date, tier)
        print(f"  {' '.join(cmd)}")

    print()
    print("--- Group C (HTTP batch, up to 8 workers) ---")
    for juris, extra in GROUP_C:
        cmd = _build_cmd(juris, extra, start_date, end_date, tier)
        print(f"  {' '.join(cmd)}")

    print()
    print("--- Group D (HTTP batch, up to 8 workers) ---")
    for juris, extra in GROUP_D:
        cmd = _build_cmd(juris, extra, start_date, end_date, tier)
        print(f"  {' '.join(cmd)}")

    # Coverage check
    _verify_coverage()


def _verify_coverage() -> None:
    """Verify all jurisdictions from run_pipeline.py are covered."""
    configured = set()
    for group in [GROUP_A, GROUP_B, GROUP_C, GROUP_D]:
        for juris, _ in group:
            configured.add(juris)

    missing = ALL_TIER_JURISDICTIONS - configured
    extra = configured - ALL_TIER_JURISDICTIONS

    if missing:
        print()
        log.warning(
            "Jurisdictions from run_pipeline.py NOT in any group: %s",
            ", ".join(sorted(missing)),
        )
    if extra:
        print()
        log.info(
            "Additional jurisdictions in groups (not in run_pipeline.py tiers): %s",
            ", ".join(sorted(extra)),
        )
    if not missing:
        log.info("Jurisdiction coverage: ALL %d jurisdictions covered ✓",
                 len(configured))


def run_group_serial(
    group: list[tuple[str, list[str]]],
    start_date: str,
    end_date: str,
    tier: str,
    state: dict[str, Any],
) -> None:
    """Run a group serially (one at a time)."""
    for juris, extra in group:
        result = run_jurisdiction(juris, extra, start_date, end_date, tier)
        _update_juris_state(
            state, juris,
            started_at=result["duration_s"] and _timestamp(),
            finished_at=_timestamp(),
            exit_code=result["exit_code"],
            duration_s=result["duration_s"],
            items_synced=result["items_synced"],
            meetings_found=result["meetings_found"],
            error_message=result["error_message"],
        )
        if result["exit_code"] == 0:
            state["success_count"] += 1
        else:
            state["failure_count"] += 1
        _save_state(state)


def run_group_parallel(
    group: list[tuple[str, list[str]]],
    start_date: str,
    end_date: str,
    tier: str,
    state: dict[str, Any],
    max_workers: int = 8,
) -> None:
    """Run a group in parallel via ProcessPoolExecutor."""
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for juris, extra in group:
            future = pool.submit(
                run_jurisdiction, juris, extra, start_date, end_date, tier,
            )
            futures[future] = juris

        for future in as_completed(futures):
            juris = futures[future]
            try:
                result = future.result()
            except Exception as e:
                log.error("  %s: pool exception %s", juris, e)
                result = {
                    "juris": juris, "exit_code": -3, "duration_s": 0,
                    "items_synced": None, "meetings_found": None,
                    "error_message": str(e)[:500], "summary": "POOL_EXCEPTION",
                    "cmd": "",
                }

            _update_juris_state(
                state, juris,
                started_at=result["duration_s"] and _timestamp(),
                finished_at=_timestamp(),
                exit_code=result["exit_code"],
                duration_s=result["duration_s"],
                items_synced=result["items_synced"],
                meetings_found=result["meetings_found"],
                error_message=result["error_message"],
            )
            if result["exit_code"] == 0:
                state["success_count"] += 1
            else:
                state["failure_count"] += 1
            _save_state(state)


# ── Post-sync tasks ──

def run_minutes_check(state: dict[str, Any]) -> None:
    """Check completed meetings without minutes_url for newly published minutes."""
    log.info("--- Minutes check pass ---")
    try:
        from sqlalchemy import text
        from db import get_engine
        from db.minutes_check import check_all as check_minutes

        engine = get_engine()
        with engine.connect() as conn:
            total_pending = conn.execute(
                text(
                    "SELECT COUNT(*) FROM meetings "
                    "WHERE minutes_url IS NULL AND sync_status = 'complete'"
                )
            ).scalar()
        log.info("  %d meetings without minutes_url", total_pending)
        updated = check_minutes(engine)
        if updated:
            log.info("  ✅ %d meetings now have minutes_url", updated)
            state["post_sync"]["minutes_check"] = f"{updated} meetings updated"
        else:
            log.info("  No new minutes found")
            state["post_sync"]["minutes_check"] = "no updates"
    except Exception as e:
        log.warning("  Minutes check pass skipped: %s", e)
        state["post_sync"]["minutes_check"] = f"skipped: {e}"


def run_votes_backfill(state: dict[str, Any]) -> None:
    """Backfill votes from minutes PDFs for Destiny jurisdictions."""
    log.info("--- Backfill votes (Destiny jurisdictions) ---")
    results = []
    for jname in ["el-mirage", "glendale"]:
        try:
            from scraper.backfill_votes import backfill_jurisdiction
            cnt = backfill_jurisdiction(jname, limit=5)
            if cnt:
                log.info("  ✅ %s: votes backfilled for %d meeting(s)", jname, cnt)
                results.append(f"{jname}: {cnt}")
            else:
                log.info("  %s: no new votes found", jname)
                results.append(f"{jname}: none")
        except Exception as e:
            log.warning("  %s backfill skipped: %s", jname, e)
            results.append(f"{jname}: skipped ({e})")

    state["post_sync"]["votes_backfill"] = "; ".join(results) if results else "none"


def run_fts_rebuild(state: dict[str, Any]) -> None:
    """Rebuild the FTS search index."""
    log.info("--- Rebuilding FTS search index ---")
    try:
        from db.newsroom import rebuild_fts
        rebuild_fts()
        log.info("  ✅ FTS search index rebuilt")
        state["post_sync"]["fts_rebuild"] = "completed"
    except Exception as e:
        log.warning("  FTS index rebuild skipped: %s", e)
        state["post_sync"]["fts_rebuild"] = f"skipped: {e}"


# ── Main entry ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Parallel sync runner")
    parser.add_argument(
        "--tier", choices=["daily", "weekly"], default="daily",
        help="Sync tier: daily (3d window) or weekly (30d window)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print execution plan and exit without syncing",
    )
    args = parser.parse_args()

    if args.dry_run:
        _print_plan(args.tier)
        return 0

    # ── Step 1: DB Health Check ──
    log.info("=== Sync pipeline start (tier=%s) ===", args.tier)
    if not _check_db_health():
        state = _load_state()
        state["tier"] = args.tier
        state["started_at"] = _timestamp()
        state["finished_at"] = _timestamp()
        state["reconciliation"]["escalated"] = True
        state["reconciliation"]["error"] = "DB health check failed — aborting"
        state["reconciliation"]["escalated_at"] = _timestamp()
        _save_state(state)
        log.error("DB unreachable — aborting. No jurisdictions synced.")
        return 1

    # ── Initialize state ──
    state = _load_state()
    state["tier"] = args.tier
    state["started_at"] = _timestamp()
    state["finished_at"] = None
    state["total_duration"] = None
    state["success_count"] = 0
    state["failure_count"] = 0
    state["skipped_count"] = 0
    _save_state(state)

    start_date, end_date = _window_args(args.tier)
    log.info("  Window: %s to %s", start_date, end_date)

    overall_start = time.time()

    # ── Step 2: Group A (Playwright serial) ──
    log.info("--- Group A (Playwright serial, %d jurisdiction(s)) ---",
             len(GROUP_A))
    run_group_serial(GROUP_A, start_date, end_date, args.tier, state)

    # ── Step 3: Group B (HTTP batch) ──
    log.info("--- Group B (HTTP batch, %d jurisdiction(s)) ---",
             len(GROUP_B))
    group_b = schedule_group("B", args.tier)
    run_group_parallel(group_b, start_date, end_date, args.tier, state)

    # ── Step 4: Group C (HTTP batch) ──
    log.info("--- Group C (HTTP batch, %d jurisdiction(s)) ---",
             len(GROUP_C))
    run_group_parallel(GROUP_C, start_date, end_date, args.tier, state)

    # ── Step 5: Group D (HTTP batch) ──
    log.info("--- Group D (HTTP batch, %d jurisdiction(s)) ---",
             len(GROUP_D))
    run_group_parallel(GROUP_D, start_date, end_date, args.tier, state)

    overall_elapsed = time.time() - overall_start
    log.info("All sync groups completed in %.0fs", overall_elapsed)

    # ── Step 6: Post-sync tasks ──
    # Minutes check (runs even if some syncs failed)
    run_minutes_check(state)

    # Votes backfill
    run_votes_backfill(state)

    # FTS rebuild
    run_fts_rebuild(state)

    # ── Finalize state ──
    state["finished_at"] = _timestamp()
    state["total_duration"] = overall_elapsed
    state["reconciliation"]["last_checked_at"] = _timestamp()
    _save_state(state)

    # ── Summary ──
    log.info("=== Sync pipeline complete ===")
    log.info("  Duration: %.0fs", overall_elapsed)
    log.info("  Success: %d | Failure: %d", state["success_count"], state["failure_count"])
    for juris, jstate in sorted(state["jurisdictions"].items()):
        ec = jstate.get("exit_code")
        dur = jstate.get("duration_s", 0)
        items = jstate.get("items_synced") or "?"
        err = jstate.get("error_message") or ""
        if ec == 0 and not err:
            log.info("  ✅ %s: %s items (%.0fs)", juris, items, dur)
        else:
            log.info("  ❌ %s: exit=%s (%.0fs) %s", juris, ec, dur, err[:100])

    return 0 if state["failure_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
