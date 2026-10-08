#!/usr/bin/env python3
"""
Pipeline runner — called by cron to scrape all jurisdictions.

Usage:
  DATABASE_URL=postgresql://... python scripts/run_pipeline.py

Flags:
  --days-back N      Tier 1 & 2 search window (default: 3)
  --days-forward N   Future search window (default: 14)
  --weekly-days-back N  Tier 3 (Sunday) search window (default: 30)

Design:
  - Tier 1 & 2 (daily): N-day rolling window back, N-day rolling window
    forward — quick check for anything newly posted in the last few days,
    plus upcoming meetings (e.g. Maricopa County BOS on DataBank).
  - Tier 3 (weekly/Sunday): 30-day rolling window back, 14 days forward
    — safety net for meetings posted further out than the daily window.
  - After both tiers, a minutes check pass re-visits completed meetings
    without minutes_url to see if minutes have been published since.
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

from scraper.source_registry import source_by_command

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_pipeline")

# ── Default windows (overridable via CLI) ────────────────────────────────

DEFAULT_DAYS_BACK = 3
DEFAULT_WEEKLY_DAYS_BACK = 30
DEFAULT_DAYS_FORWARD = 14


def _window_args(days_back: int, days_forward: int) -> list[str]:
    """Return ['--start-date=YYYY-MM-DD', '--end-date=YYYY-MM-DD']."""
    today = date.today()
    start = today - timedelta(days=days_back)
    end = today + timedelta(days=days_forward)
    return [f"--start-date={start}", f"--end-date={end}"]


def _build_tiers(days_back: int, days_forward: int, weekly_days_back: int
) -> tuple[list[list[str]], list[list[str]], list[list[str]]]:
    """Build Tier 1/2/3 lists with the given window sizes."""

    def daily_args(label: str, *extra: str) -> list[str]:
        return [label] + _window_args(days_back, days_forward) + list(extra)

    def weekly_args(label: str, *extra: str) -> list[str]:
        return [label] + _window_args(weekly_days_back, days_forward) + list(extra)

    tier_1: list[list[str]] = [
        daily_args("chandler", "--sync"),
        daily_args("tempe", "--sync"),
        ["tempe-subcommittees", "--sync"],
    ]

    tier_2: list[list[str]] = [
        daily_args("bos", "--sync"),
        daily_args("pz", "--sync"),
        daily_args("adj", "--sync"),
        daily_args("health", "--sync"),
        daily_args("drain", "--sync"),
        daily_args("tab", "--sync"),
        daily_args("ida", "--sync"),
        daily_args("mesa", "--sync"),
        daily_args("phoenix-rss", "--sync"),
        daily_args("phoenix-aem", "--sync"),
        daily_args("scottsdale", "--sync"),
        daily_args("scottsdale-boards", "--sync"),
        daily_args("glendale", "--sync"),
        daily_args("glendale-new", "--sync",
                   "--bodies=glendale-city-council,glendale-planning-commission"),
        daily_args("peoria", "--sync"),
        daily_args("surprise", "--sync"),
        daily_args("surprise-civicclerk", "--sync",
                   "--bodies=surprise-pz,surprise-arts,surprise-veterans,surprise-library,surprise-parks,surprise-psprs-fire,surprise-psprs-police,surprise-health-benefits,surprise-nominations,surprise-audit,surprise-tourism,surprise-judicial-selection"),
        daily_args("gilbert", "--sync"),
        daily_args("gilbert-planning", "--sync"),
        daily_args("tucson", "--sync"),
        daily_args("tucson-pc", "--sync"),
        daily_args("avondale", "--sync"),
        daily_args("goodyear", "--sync"),
        daily_args("el-mirage", "--sync"),
        daily_args("paradise-valley", "--sync"),
        daily_args("fountain-hills", "--sync"),
        daily_args("litchfield-park", "--sync"),
        daily_args("youngtown", "--sync"),
        daily_args("flagstaff", "--sync"),
        daily_args("yuma", "--sync"),
        daily_args("queen-creek", "--sync"),
        daily_args("apache-junction", "--sync"),
        daily_args("tolleson", "--sync"),
        daily_args("buckeye-granicus", "--sync"),
        daily_args("valley-metro", "--sync"),
    ]

    tier_3: list[list[str]] = [
        weekly_args("chandler", "--sync"),
        weekly_args("tempe", "--sync"),
        ["tempe-subcommittees", "--sync"],
        weekly_args("bos", "--sync"),
        weekly_args("pz", "--sync"),
        weekly_args("adj", "--sync"),
        weekly_args("health", "--sync"),
        weekly_args("drain", "--sync"),
        weekly_args("tab", "--sync"),
        weekly_args("ida", "--sync"),
        weekly_args("mesa", "--sync"),
        weekly_args("phoenix-aem", "--sync"),
        weekly_args("scottsdale", "--sync"),
        weekly_args("scottsdale-boards", "--sync"),
        weekly_args("glendale", "--sync"),
        weekly_args("glendale-new", "--sync",
                    "--bodies=glendale-city-council,glendale-planning-commission"),
        weekly_args("peoria", "--sync"),
        weekly_args("surprise", "--sync"),
        weekly_args("surprise-civicclerk", "--sync",
                    "--bodies=surprise-pz,surprise-arts,surprise-veterans,surprise-library,surprise-parks,surprise-psprs-fire,surprise-psprs-police,surprise-health-benefits,surprise-nominations,surprise-audit,surprise-tourism,surprise-judicial-selection"),
        weekly_args("gilbert", "--sync"),
        weekly_args("gilbert-planning", "--sync"),
        weekly_args("tucson", "--sync"),
        weekly_args("tucson-pc", "--sync"),
        weekly_args("avondale", "--sync"),
        weekly_args("goodyear", "--sync",
                    "--bodies=goodyear-city-council,goodyear-planning-zoning-commission,goodyear-arts-culture-commission,goodyear-youth-commission,goodyear-water-advisory,goodyear-fire-psprs,goodyear-police-psprs,goodyear-joint-psprs,goodyear-psprs,goodyear-audit-committee,goodyear-notice-of-quorum,goodyear-ida,goodyear-parks,goodyear-boa,goodyear-cfd,goodyear-healthcare-trust,goodyear-firefighter-retirement,goodyear-public-art"),
        weekly_args("el-mirage", "--sync"),
        weekly_args("paradise-valley", "--sync"),
        weekly_args("fountain-hills", "--sync"),
        weekly_args("litchfield-park", "--sync"),
        weekly_args("youngtown", "--sync"),
        weekly_args("flagstaff", "--sync"),
        weekly_args("yuma", "--sync"),
        weekly_args("queen-creek", "--sync"),
        weekly_args("apache-junction", "--sync"),
        weekly_args("buckeye-granicus", "--sync"),
        weekly_args("valley-metro", "--sync"),
    ]

    return tier_1, tier_2, tier_3


def run_sync(args: list[str], label: str) -> tuple[int, str]:
    """Run a single agenda_scraper sync and return (exit_code, summary_line).
    Runs inside a worker process when called from ProcessPoolExecutor.
    """
    # The scheduler label is not always the CLI command.  In particular,
    # ``buckeye-granicus`` is the scheduler/source identity while the existing
    # scraper handler is named ``buckeye``.  Passing the label through made the
    # generic AgendaOnline fallback scrape Maricopa County and persist it under
    # the bogus body code ``buckeye-granicus``.  Resolve aliases at the process
    # boundary, just as the newer sync runner does.
    source = source_by_command(args[0])
    invocation_args = [source.invocation_command, *args[1:]]
    cmd = [sys.executable, "scripts/scrape_agendas.py"] + invocation_args
    start = time.time()
    log.info("  [Worker %s] Starting %s", os.getpid(), label)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
        )
        elapsed = time.time() - start
        lines = result.stdout.strip().split("\n")
        summary = (lines[-1] if lines else "").strip()
        if not summary or "Synced" not in summary:
            summary = (lines[-2] if len(lines) > 1 else "").strip()
        return result.returncode, f"{label}: {summary} ({elapsed:.0f}s)"
    except subprocess.TimeoutExpired:
        return -1, f"{label}: TIMEOUT (>600s)"
    except Exception as e:
        return -1, f"{label}: ERROR {e}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the daily sync across all jurisdictions.",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=DEFAULT_DAYS_BACK,
        help=f"Tier 1 & 2 backward window in days (default: {DEFAULT_DAYS_BACK})",
    )
    parser.add_argument(
        "--days-forward",
        type=int,
        default=DEFAULT_DAYS_FORWARD,
        help=f"Forward window in days for upcoming meetings (default: {DEFAULT_DAYS_FORWARD})",
    )
    parser.add_argument(
        "--weekly-days-back",
        type=int,
        default=DEFAULT_WEEKLY_DAYS_BACK,
        help=f"Tier 3 (Sunday) backward window in days (default: {DEFAULT_WEEKLY_DAYS_BACK})",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Run scrapers concurrently using a process pool",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help=f"Number of parallel workers (default: 5, used with --parallel)",
    )
    return parser.parse_args(argv)


def main():
    args = parse_args()

    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon
    hour = now.hour
    is_sunday_morning = weekday == 6 and hour < 6

    log.info("=== Daily sync start ===")
    log.info("  Window: %d days back, %d days forward (weekly: %d)",
             args.days_back, args.days_forward, args.weekly_days_back)

    TIER_1, TIER_2, TIER_3 = _build_tiers(
        args.days_back, args.days_forward, args.weekly_days_back,
    )

    def _submit_to_pool(
        pool: ProcessPoolExecutor,
        tier_list: list[list[str]],
        all_futures: dict,
    ) -> None:
        """Submit a tier's jobs to a shared pool."""
        for arglist in tier_list:
            label_j = arglist[0]
            fut = pool.submit(run_sync, arglist, label_j)
            all_futures[fut] = label_j
            log.info("  \u2192 %s submitted to worker pool", label_j)

    def _run_tier_serial(tier_list: list[list[str]], label: str) -> list[str]:
        """Run a tier serially."""
        tier_results: list[str] = []
        log.info("--- %s (serial) ---", label)
        for arglist in tier_list:
            code, summary = run_sync(arglist, arglist[0])
            tier_results.append(summary)
            log.info(summary)
        return tier_results

    results: list[str] = []

    if args.parallel:
        # Single pool across all tiers — workers stay busy
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            log.info("--- Submitting tiers with %d workers ---", args.workers)

            all_futures: dict = {}
            _submit_to_pool(pool, TIER_1, all_futures)
            _submit_to_pool(pool, TIER_2, all_futures)

            if is_sunday_morning:
                log.info("  Weekly window: %d days back, %d days forward",
                         args.weekly_days_back, args.days_forward)
                _submit_to_pool(pool, TIER_3, all_futures)
            else:
                log.info("--- No Tier 3 (next Sunday <%d>6 UTC) ---", hour)

            for future in as_completed(all_futures):
                label_j = all_futures[future]
                try:
                    code, summary = future.result()
                except Exception as e:
                    summary = f"{label_j}: EXECUTOR ERROR {e}"
                results.append(summary)
                log.info("  \u2713 %s", summary)
    else:
        results += _run_tier_serial(TIER_1, "Tier 1 (active jurisdictions)")
        results += _run_tier_serial(TIER_2, "Tier 2 (all jurisdictions)")
        if is_sunday_morning:
            log.info("  Weekly window: %d days back, %d days forward",
                     args.weekly_days_back, args.days_forward)
            results += _run_tier_serial(TIER_3, "Tier 3 (weekly safety net)")
        else:
            log.info("--- No Tier 3 (next Sunday <%d>6 UTC) ---", hour)
    # ── Doc checks moved to scripts/check_docs.py ──
    # Run independently:  python scripts/check_docs.py [--minutes] [--docs]


    # ── Backfill votes from minutes PDFs ──
    log.info("--- Backfill votes (Destiny jurisdictions) ---")
    for jname in ["el-mirage", "glendale"]:
        try:
            from scraper.backfill_votes import backfill_jurisdiction
            cnt = backfill_jurisdiction(jname, limit=5)
            if cnt:
                log.info("  ✅ %s: votes backfilled for %d meeting(s)", jname, cnt)
            else:
                log.info("  %s: no new votes found", jname)
        except Exception as e:
            log.warning("  %s backfill skipped: %s", jname, e)

    # ── Rebuild FTS search index ──
    log.info("--- Rebuilding FTS search index ---")
    try:
        from db.newsroom import rebuild_fts
        rebuild_fts()
        log.info("  ✅ FTS search index rebuilt")
    except Exception as e:
        log.warning("  FTS index rebuild skipped: %s", e, exc_info=True)

    # ── Summary ──
    log.info("=== Daily sync complete ===")
    n_tiers = len(TIER_1) + len(TIER_2)
    n_tier3 = len(TIER_3) if is_sunday_morning else 0
    log.info("  %d jurisdictions synced today, %d on Sunday", n_tiers, n_tier3)
    for r in results:
        log.info("  %s", r)


if __name__ == "__main__":
    main()
