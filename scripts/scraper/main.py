from __future__ import annotations

import asyncio
import datetime as dt
import re
import sys
import time
from pathlib import Path

from scraper.common.utils import (
    log, setup_logger, SOURCE_PAGE, SEARCH_BASE, REQUIRED_BODY, REQUIRED_TYPES,
    ROOT, AGENDAS_ROOT, SUPPORT_ROOT, AGENDA_ITEMS_ROOT, AGENDA_ITEMS_CSV,
    RAW_AGENDA_ITEMS_CSV, REJECTED_RAW_BLOCKS_CSV, DISCOVERY_CSV, LOGS_ROOT,
    get_async_playwright, retry_with_backoff, CASE_PATTERN, C_NUMBER_PATTERN,
    _extract_c_number, parse_c_number_parts, parse_metadata_from_page_data,
    extract_meeting_metadata_from_page, is_image_based_agenda,
    get_page_state_summary,
)
from scraper.common.models import Meeting
from scraper.cli import parse_args, parse_date
from scraper.source_registry import adapter_for_command
from scraper.common.search import parse_search_results_html, build_search_url, extract_meetings
from scraper.common.io_utils import (
    slugify, normalize_meeting_date, _normalize_text_date,
    month_dir_for_date, month_metadata_path, ensure_dir, csv_row,
    read_existing_rows, write_download_row, write_discovery_row,
    write_agenda_item_row, write_structured_agenda_item_row,
    write_raw_agenda_item_row, write_rejected_raw_block_row,
    debug_agenda_html_path,
    url_ext, infer_extension, download_url,
    existing_paths_present, row_paths_present, read_existing_agenda_urls,
    read_existing_discovery_keys, read_agenda_metadata_rows,
    filter_agenda_metadata_rows, read_existing_agenda_item_keys,
    read_existing_raw_block_keys, read_existing_rejected_block_keys,
    read_existing_structured_item_keys, write_discovery_rows,
    iter_discovery_documents,
)
from scraper.common.agenda_items import (
    parse_agenda_items_from_html, parse_raw_agenda_blocks_html,
    split_bilingual_title,
    _raw_block_boilerplate_reason, validate_raw_block, split_raw_block_into_items,
    splitter_self_test, split_raw_agenda_blocks_to_structured,
    _clean_line, _looks_like_boilerplate, _looks_like_item_heading,
    _looks_like_section_heading, _detect_vote_or_action, _build_item_url,
    _clean_lnk_title, _find_item_tables, _extract_lnk_from_table,
    extract_agenda_item_titles,
)
from scraper.common.supporting_docs import (
    _extract_supporting_docs_from_table, extract_supporting_documents_from_items,
    extract_supporting_documents_dynamic, extract_supporting_documents_dynamic_concurrent,
    _click_and_extract_item,
)
from scraper.county.pz import (
    _format_mm_dd_yyyy,
    _normalize_pz_meeting_title,
    build_pz_search_url,
    extract_pz_meetings,
    extract_pz_agenda_items,
)
from scraper.common.votes import extract_votes_from_summary

async def write_agenda_debug_files(page, meeting: dict[str, str]) -> None:
    ensure_dir(LOGS_ROOT)
    meeting_id = (meeting.get("record_id") or meeting.get("meeting_id") or "meeting").strip() or "meeting"
    html_path = debug_agenda_html_path(meeting_id, ".html")
    txt_path = debug_agenda_html_path(meeting_id, ".txt")
    selectors_path = debug_agenda_html_path(meeting_id, "_selectors.txt")

    html_path.write_text(await page.content(), encoding="utf-8")
    body_text = await page.locator("body").inner_text(timeout=60000)
    txt_path.write_text(body_text, encoding="utf-8")

    selector_report = await page.evaluate(
        r"""
        () => {
          const clean = s => (s || '').replace(/\s+/g, ' ').trim();
          const describe = el => {
            if (!el) return 'unknown';
            const tag = el.tagName ? el.tagName.toLowerCase() : 'element';
            const id = el.id ? `#${el.id}` : '';
            const cls = el.className && typeof el.className === 'string'
              ? '.' + el.className.trim().split(/\s+/).filter(Boolean).slice(0, 4).join('.')
              : '';
            return `${tag}${id}${cls}`;
          };
          const text = el => clean(el?.innerText || el?.textContent || '');
          const candidates = Array.from(document.querySelectorAll('main, section, article, table, tbody, thead, tr, div, ul, ol, body'))
            .filter(el => text(el).length > 0)
            .slice(0, 50);
          return candidates.map((el, idx) => {
            const t = text(el);
            const rows = el.querySelectorAll('tr, li, p').length;
            const links = el.querySelectorAll('a[href]').length;
            const numbered = t.includes('1.') || t.includes('2.') || t.includes('3.');
            return {
              index: idx + 1,
              selector: describe(el),
              rows,
              links,
              numbered,
              text: t.slice(0, 500),
            };
          });
        }
        """
    )

    with selectors_path.open("w", encoding="utf-8") as f:
        for row in selector_report:
            f.write(
                f"Selector: {row['selector']}\n"
                f"Child rows/items: {row['rows']}\n"
                f"Link count: {row['links']}\n"
                f"Contains numbered items: {row['numbered']}\n"
                f"Text (first 500 chars): {row['text']}\n"
                f"---\n"
            )



async def extract_agenda_items_for_meeting(page, meeting: dict[str, str]) -> list[dict[str, str]]:
    source_url = (meeting.get("document_url") or meeting.get("agenda_url") or "").strip()
    if not source_url:
        return []
    # Wait for all network activity to settle (including the AJAX call that
    # populates the agenda).  Using networkidle ensures the page is fully
    # rendered, not a stale JS scaffold with unloaded data.
    await page.goto(source_url, wait_until="networkidle", timeout=60000)
    html = await page.content()
    normalized_meeting = {
        "meeting_id": (meeting.get("record_id") or meeting.get("meeting_id") or "meeting").strip() or "meeting",
        "meeting_date": (meeting.get("record_date") or meeting.get("meeting_date") or "").strip(),
        "meeting_type": (meeting.get("meeting_type") or "").strip(),
    }
    return parse_agenda_items_from_html(html, source_url, normalized_meeting)



async def extract_raw_agenda_blocks_for_meeting(page, meeting: dict[str, str]) -> list[dict[str, str]]:
    source_url = (meeting.get("document_url") or meeting.get("agenda_url") or "").strip()
    if not source_url:
        return []
    await page.goto(source_url, wait_until="load")
    return parse_raw_agenda_blocks_html(await page.content(), meeting)



async def extract_raw_agenda_blocks_from_metadata(page, meeting_rows: Optional[list[dict[str, str]]] = None) -> int:
    if meeting_rows is None:
        meeting_rows = read_agenda_metadata_rows()
    if not meeting_rows:
        print("No agenda metadata rows found for raw block extraction.")
        return 0

    existing_keys = read_existing_raw_block_keys(RAW_AGENDA_ITEMS_CSV)
    ensure_dir(RAW_AGENDA_ITEMS_CSV.parent)
    wrote = 0

    for meeting in meeting_rows:
        blocks = await extract_raw_agenda_blocks_for_meeting(page, meeting)
        for block in blocks:
            key = (block["meeting_id"], block["raw_block_index"])
            if key in existing_keys:
                continue
            write_raw_agenda_item_row(block)
            existing_keys.add(key)
            wrote += 1
    return wrote



async def extract_agenda_items_from_metadata(
    page,
    start_date: Optional[dt.date] = None,
    end_date: Optional[dt.date] = None,
    limit: Optional[int] = None,
) -> int:
    meeting_rows = filter_agenda_metadata_rows(
        read_agenda_metadata_rows(), start_date, end_date, limit
    )
    if not meeting_rows:
        print("No agenda metadata rows matched the selected date range/limit.")
        return 0

    existing_keys = read_existing_agenda_item_keys(AGENDA_ITEMS_CSV)
    ensure_dir(AGENDA_ITEMS_CSV.parent)
    wrote = 0

    for meeting in meeting_rows:
        meeting_id = (meeting.get("record_id") or meeting.get("meeting_id") or "").strip() or "meeting"
        try:
            items = await extract_agenda_items_for_meeting(page, meeting)
        except Exception as e:
            log.error("extract_agenda_items_for_meeting failed meeting_id=%s error=%s", meeting_id, str(e)[:300])
            continue
        for item in items:
            key = (item["meeting_id"], item["agenda_item_id"])
            if key in existing_keys:
                continue
            write_agenda_item_row(item)
            existing_keys.add(key)
            wrote += 1
    return wrote



async def count_agenda_items_for_meeting(page, meeting_url: str) -> int:
    """Visit an agenda HTML page and count the number of numbered agenda items."""
    items = await extract_agenda_item_titles(page, meeting_url)
    return len(items)




def _extract_chandler_minutes(session, body_code, meeting_id, meeting_date):
    """Extract votes from Chandler meeting minutes PDF for a single meeting.

    Runs independently of the agenda sync so minutes can be backfilled
    for already-synced meetings without re-syncing agenda items.
    """
    try:
        from scraper.jurisdictions.chandler import (
            build_attachments_url, fetch_attachments_page,
            parse_attachments_for_minutes,
            fetch_minutes_pdf_bytes, extract_pdf_text,
            parse_minutes_votes,
        )
        from db.persist import persist_votes

        att_url = build_attachments_url(meeting_id, meeting_date)
        att_html = fetch_attachments_page(att_url)
        if att_html:
            minutes_pdfs = parse_attachments_for_minutes(att_html, meeting_id)
            for pdf_url in minutes_pdfs:
                pdf_bytes = fetch_minutes_pdf_bytes(pdf_url)
                if pdf_bytes:
                    text = extract_pdf_text(pdf_bytes)
                    if text:
                        vote_data = parse_minutes_votes(text)
                        if vote_data.get("votes"):
                            count = persist_votes(
                                session, body_code, meeting_id,
                                vote_data["supervisors"],
                                vote_data["votes"],
                            )
                            session.flush()
                            print("        minutes votes: %d recorded (%d persisted)" % (len(vote_data["votes"]), count))
                            break
    except Exception as ve:
        log.debug("Chandler minutes vote extraction failed for %s: %s", meeting_id, ve)



async def main() -> int:
    global log
    setup_logger()
    args = parse_args()

    # Backward compatibility: --sync-pz with legacy --pz-* flags
    if getattr(args, 'sync_pz', False):
        print("WARNING: --sync-pz is deprecated. Use: pz --sync", file=sys.stderr)
        args.source = "pz"
        # Map legacy PZ flags to the new unified args
        if getattr(args, 'pz_start_date', None):
            args.start_date = args.pz_start_date
        if getattr(args, 'pz_end_date', None):
            args.end_date = args.pz_end_date
        if getattr(args, 'pz_limit', None) is not None:
            args.limit = args.pz_limit
        args.sync = True

    if getattr(args, 'self_test_splitter', False):
        return 0 if splitter_self_test(verbose=True) else 1

    if args.source == "hearings":
        from scraper.housing_hearings import HearingFinder
        import sys as _sys
        import argparse as _argparse
        
        _p = _argparse.ArgumentParser(prog="hearings", add_help=False)
        _p.add_argument("--days", type=int, default=30)
        _p.add_argument("--jurisdiction", default=None)
        _p.add_argument("--body", default=None)
        _p.add_argument("--json", action="store_true")
        _hargs, _ = _p.parse_known_args(_sys.argv[_sys.argv.index('hearings')+1:])
        
        finder = HearingFinder()
        items, hearing_meetings = finder.find_housing_hearings(
            days=_hargs.days, jurisdiction=_hargs.jurisdiction, body_filter=_hargs.body
        )
        return finder.print_report(items, hearing_meetings, _hargs.json, _hargs.jurisdiction)

    if args.source == "tempe-subcommittees" and args.sync:
        from scraper.jurisdictions.tempe.subcommittees import main as tempe_sub_main
        import sys as _sys
        # Extract remaining args after 'tempe-subcommittees' for the module parser
        remaining = _sys.argv[_sys.argv.index('tempe-subcommittees') + 1:]
        _sys.argv = ['tempe-subcommittees'] + remaining
        return tempe_sub_main()

    if args.source == "phoenix-aem" and args.sync:
        from scraper.jurisdictions.phoenix_aem import fetch_all_notice_bodies, search_and_convert
        from db import get_session, init_db, replace_meeting_data_safe, Meeting as MeetingModel
        from sqlalchemy import select
        import datetime as _dt

        init_db()
        session = get_session()

        body_filter_str = getattr(args, "bodies", None)
        body_filter = [b.strip() for b in body_filter_str.split(",") if b.strip()] if body_filter_str else None

        results = search_and_convert("notices", max_results=200, body_filter=body_filter)
        if not results:
            print("No Phoenix AEM meetings found.")
            return 0

        # Enrich with PDF-extracted agenda items
        if getattr(args, "extract_pdf", True):
            try:
                from scraper.jurisdictions.phoenix_planning import enrich_notice_meetings_with_pdf_items
                print(f"Extracting PDF content for {len(results)} AEM meetings...")
                enrich_notice_meetings_with_pdf_items(results, force=args.force)
            except Exception as e:
                print(f"PDF extraction failed (continuing without): {e}")

        total_items = 0
        meeting_count = len(results)
        for idx, m in enumerate(results, 1):
            meeting_id = m["meeting_id"]
            body_code = m.get("body", "phoenix-aem")
            meeting_date = m.get("meeting_date", "")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("meeting_title", "")
            source_url = m.get("source_url", "")

            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
                "meeting_title": meeting_title,
                "source_url": source_url,
            }

            existing = session.execute(
                select(Meeting).where(Meeting.body == body_code, Meeting.meeting_id == meeting_id)
            ).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                if args.limit and idx > args.limit:
                    break
                continue

            items_raw = m.get("agenda_items", [])
            if not items_raw:
                replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                ts = _dt.datetime.now().strftime("%H:%M:%S")
                print(f"{ts} [{idx}/{meeting_count}] {meeting_id}: no items")
                continue

            agenda_dicts = []
            for i, item in enumerate(items_raw):
                an = item.get("agenda_item_number", str(i + 1))
                agenda_dicts.append({
                    "agenda_item_id": f"{body_code}-{meeting_id}_{an}",
                    "meeting_id": meeting_id,
                    "agenda_item_number": an,
                    "agenda_item_title": item.get("title", ""),
                    "agenda_item_text": item.get("description", ""),
                    "source_body": body_code,
                    "source_url": source_url,
                    "c_number": "", "c_number_base": "", "case_number": "",
                    "agenda_item_url": item.get("url", ""),
                    "vote_or_action": item.get("action", ""),
                    "item_type": item.get("item_type", ""),
                })

            replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, agenda_dicts)
            total_items += len(agenda_dicts)
            ts = _dt.datetime.now().strftime("%H:%M:%S")
            print(f"{ts} [{idx}/{meeting_count}] {meeting_date} {meeting_id}: "
                  f"{len(agenda_dicts)} item(s)")

            if args.limit and idx >= args.limit:
                break

        # Cross-reference staff reports to meetings by case number
        try:
            from scraper.jurisdictions.phoenix_planning import link_staff_reports_to_meetings
            links = link_staff_reports_to_meetings(session)
            if links:
                print(f"Linked {links} staff reports to meeting agenda items")
        except Exception as e:
            log.warning("Staff report cross-referencing failed: %s", e)

        session.close()
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        print(f"{ts} Synced {total_items} Phoenix AEM agenda items across {meeting_count} meeting(s)")
        return 0

    if args.source == "phoenix-aem" and args.sync_results:
        # Incremental pagination — fetch one page at a time, process immediately
        try:
            from scraper.jurisdictions.phoenix_aem import RESULTS_BASE, _build_url, fetch_json, convert_to_meeting_dict, resolve_body
        except ImportError:
            from scraper.jurisdictions.phoenix_aem import RESULTS_BASE, _build_url, fetch_json, convert_to_meeting_dict, resolve_body
        from db import get_session, init_db, replace_meeting_data_safe, Meeting as MeetingModel
        from sqlalchemy import select
        from scraper.jurisdictions.phoenix_planning import (
            link_staff_reports_to_meetings,
        )
        import datetime as _pdt
        init_db()
        session = get_session()

        log.info("Fetching Phoenix AEM meeting results (incremental)...")

        # Shared sentinel/non-meeting guard: the ordinary search path uses the
        # same helper, so the two ingestion routes cannot drift apart.
        from scraper.jurisdictions.phoenix_aem import resolve_persistable_body

        PAGE_SIZE = 10
        offset = 0
        total_fetched = 0
        total_new = 0
        skipped_sentinels = 0
        start_ts = time.time()

        while total_fetched < 5000:
            url = _build_url(RESULTS_BASE, "", offset)
            try:
                data = fetch_json(url)
            except Exception as e:
                log.warning("Failed at offset %d: %s", offset, e)
                break

            results = data.get("results", [])
            if not results:
                break

            for raw in results:
                total_fetched += 1
                title = raw.get("title", "") or ""
                resolved = resolve_persistable_body(title)
                if resolved is None:
                    # A sentinel body or non-meeting title must never be converted
                    # or persisted: such a row is unparentable and would block
                    # normalization downstream.
                    skipped_sentinels += 1
                    continue
                slug, code = resolved
                meeting_dict = convert_to_meeting_dict(raw, slug, code)
                meeting_dict["meeting_type"] = "Result"
                meeting_dict["sync_status"] = "complete"

                meeting_id = meeting_dict["meeting_id"]
                body_code = meeting_dict["body_code"]

                existing = session.execute(
                    select(MeetingModel).where(
                        MeetingModel.body == body_code,
                        MeetingModel.meeting_id == meeting_id,
                    )
                ).scalar_one_or_none()

                if existing:
                    continue

                replace_meeting_data_safe(
                    session, body_code, meeting_id, meeting_dict, []
                )
                total_new += 1

            elapsed = time.time() - start_ts
            log.info(
                "  offset=%d  fetched=%d  new=%d  (%.0fs)",
                offset, total_fetched, total_new, elapsed,
            )
            session.commit()

            if len(results) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            time.sleep(0.5)

        # Cross-reference staff reports to results meetings
        try:
            links = link_staff_reports_to_meetings(session)
            if links:
                log.info("Linked %d staff reports to result meetings", links)
        except Exception as e:
            log.warning("Staff report cross-referencing failed: %s", e)

        session.close()
        elapsed = time.time() - start_ts
        print(f"{_pdt.datetime.now().strftime('%H:%M:%S')} Done. {total_fetched} results, {total_new} new, {skipped_sentinels} sentinel/non-meeting skipped in {elapsed:.0f}s")
        return 0

    if args.source == "phoenix-planning" and args.sync:
        from scraper.jurisdictions.phoenix_planning import sync_all
        from db import get_session, init_db
        import datetime as _pdt

        init_db()
        session = get_session()
        force = getattr(args, "force", False)

        results = sync_all(session, force=force)

        session.close()
        ts = _pdt.datetime.now().strftime("%H:%M:%S")
        events = results.get("events", {})
        staff = results.get("staff_reports", {})
        pud = results.get("pud_cases", {})
        print(f"{ts} Phoenix planning sync complete: "
              f"{events.get('synced', 0)}/{events.get('fetched', 0)} events, "
              f"{staff.get('docs_synced', 0)}/{staff.get('fetched', 0)} staff docs, "
              f"{pud.get('docs_synced', 0)}/{pud.get('fetched', 0)} PUD docs")
        return 0

    if args.init_db:
        from scraper.housing_hearings import HearingFinder
        import sys as _sys
        
        # Parse remaining args for hearings
        import argparse as _argparse
        _p = _argparse.ArgumentParser(prog="hearings", add_help=False)
        _p.add_argument("--days", type=int, default=30)
        _p.add_argument("--body", default=None)
        _p.add_argument("--json", action="store_true")
        _hargs, _ = _p.parse_known_args(_sys.argv[_sys.argv.index('hearings')+1:])
        
        finder = HearingFinder()
        items, hearing_meetings = finder.find_housing_hearings(
            days=_hargs.days, body_filter=_hargs.body
        )
        return finder.print_report(items, hearing_meetings, _hargs.json)

    if args.source == "tempe" and args.sync:
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe, persist_votes
        from db import Supervisor, PublicBody
        from scraper.jurisdictions.tempe.council_summary import fetch_and_parse_summary
        from scraper.jurisdictions.tempe import (
            search_tempe_meetings,
            PUBLIC_BODY_CODE,
        )
        from scraper.platforms.onbase import TEMPE_CONFIG, fetch_item_details_batch
        import datetime as _dt

        from db import Meeting as MeetingModel
        from db import PublicBody as PublicBodyModel
        from sqlalchemy import select

        init_db()

        # If --meeting-id is provided, skip the date-range search entirely
        if args.meeting_id:
            _sid = get_session()
            existing = _sid.execute(
                select(MeetingModel).where(
                    MeetingModel.body == PUBLIC_BODY_CODE,
                    MeetingModel.meeting_id == args.meeting_id,
                )
            ).scalar_one_or_none()
            _sid.close()
            if existing:
                meetings = [{
                    "meeting_id": args.meeting_id,
                    "meeting_date": existing.meeting_date,
                    "meeting_title": existing.meeting_title,
                    "meeting_type": existing.meeting_type,
                    "body": existing.body,
                    "canceled": False,
                }]
            else:
                meetings = [{
                    "meeting_id": args.meeting_id,
                    "meeting_date": "",
                    "meeting_title": f"Meeting {args.meeting_id}",
                    "meeting_type": "Regular Meeting",
                    "body": PUBLIC_BODY_CODE,
                    "canceled": False,
                }]
            print(f"Syncing single Tempe meeting: {args.meeting_id}")
        else:
            # Format dates for OnBase (MM/DD/YYYY)
            now = _dt.date.today()
            if args.start_date:
                d = _dt.date.fromisoformat(args.start_date)
                pz_start = f"{d.month:02d}/{d.day:02d}/{d.year}"
            else:
                three_months_ago = now - _dt.timedelta(days=90)
                pz_start = f"{three_months_ago.month:02d}/01/{three_months_ago.year}"

            if args.end_date:
                d = _dt.date.fromisoformat(args.end_date)
                pz_end = f"{d.month:02d}/{d.day:02d}/{d.year}"
            else:
                pz_end = f"{now.month:02d}/{min(28, now.day):02d}/{now.year}"

            print(f"Tempe search: {pz_start} to {pz_end}")
            body_group = getattr(args, "bodies", None) or "all"
            meetings = await search_tempe_meetings(None, pz_start, pz_end, body_group=body_group)
            if not meetings:
                print("No Tempe meetings found.")
                return 0

            if args.limit:
                meetings = meetings[:args.limit]

            print(f"Found {len(meetings)} Tempe meeting(s)")
        if len(meetings) >= 100:
            print(f"  ⚠  OnBase returned the maximum number of results. Meetings at the")
            print(f"     start of the date range may have been silently truncated.")
            print(f"     Consider syncing one year at a time (e.g. --year=2024 --year=2025).")

        session = get_session()
        total_items = 0
        meeting_count = len(meetings)

        # Pre-resolve public_body_id to avoid per-meeting DB lookups
        pb_map: dict[str, PublicBodyModel] = {}
        for pb in session.execute(select(PublicBodyModel)).scalars().all():
            pb_map[pb.body_code] = pb

        def _resolve_pb(body_code: str) -> PublicBodyModel | None:
            """Look up a PublicBody by code, auto-registering if missing."""
            pb = pb_map.get(body_code)
            if pb is not None:
                return pb
            from db import ensure_public_body
            pb_id = ensure_public_body(session, body_code)
            if pb_id:
                # Reload fresh row
                pb = session.get(PublicBodyModel, pb_id)
                pb_map[body_code] = pb
                return pb
            return None

        # Pre-resolve jurisdiction_id from DB — no hard-coded fallback
        from db import Jurisdiction as JurisdictionModel
        jur = session.execute(
            select(JurisdictionModel).where(JurisdictionModel.slug == "tempe")
        ).scalar_one_or_none()
        jur_id = jur.id if jur else None
        if not jur_id:
            log.warning("Tempe jurisdiction not found in DB")
            jur_id = 2  # hard-coded last resort for bootstrap safety

        def _ensure_tempe_members(session, sup_list):
            """Ensure Tempe council members have BodyMembership rows.
            persist_votes creates the Supervisor rows; this ensures
            BodyMembership records exist with correct roles."""
            pb = session.execute(
                select(PublicBody).where(PublicBody.slug == "tempe-city-council")
            ).scalar_one_or_none()
            if not pb:
                return
            # Tempe's Legal Action Summary PDF only contains last names
            # (e.g. "Adams" not "Jennifer Adams"). Map last names to full names
            # so Persons are created correctly.
            _TEMPE_NAME_MAP = {
                "adams": "Jennifer Adams", "amberg": "Nikki Amberg",
                "chin": "Arlene Chin", "garlid": "Doreen Garlid",
                "hodge": "Berdetta Hodge", "keating": "Randy Keating",
                "navarro": "Joel Navarro", "woods": "Corey D Woods",
            }
            titler_map = {"woods": "Mayor", "garlid": "Vice Mayor"}
            from db import BodyMembership, _ensure_membership, _find_or_create_person
            for sup in sup_list:
                norm = sup.get("normalized_name", "").strip().lower()
                if not norm:
                    continue
                role = titler_map.get(norm, "Councilmember")
                name = _TEMPE_NAME_MAP.get(norm) or sup.get("name", norm.capitalize())
                person_id = None
                person, _ = _find_or_create_person(
                    session, name, norm,
                    log_prefix="_ensure_tempe_members[",
                )
                person_id = person.id

                if person_id:
                    membership = _ensure_membership(session, person_id, "tempe-cc")
                    if membership and role:
                        membership.role = role
            session.flush()

        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m.get("meeting_date", "")
            meeting_title = m.get("meeting_title", "")
            meeting_type = m.get("meeting_type", "")
            body_code = m.get("body", PUBLIC_BODY_CODE)

            # Check if already synced (skip complete/no_agenda unless --force)
            db_m = session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == body_code,
                    MeetingModel.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()

            if db_m and (db_m.sync_status == "no_agenda" or (db_m.sync_status == "complete" and (db_m.item_count_actual or 0) > 0)) and not args.force:
                print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: {db_m.sync_status} (skip)")
                if db_m.sync_status == "complete":
                    total_items += db_m.item_count_actual or 0
                continue

            # Canceled meetings — skip immediately
            if m.get("canceled"):
                if db_m:
                    db_m.sync_status = "no_agenda"
                    db_m.last_error = "Meeting was canceled"
                    db_m.last_attempted_at = None
                    db_m.updated_at = dt.datetime.now(dt.timezone.utc)
                else:
                    pb = _resolve_pb(body_code)
                    meeting_row = MeetingModel(
                        body=body_code,
                        meeting_id=meeting_id,
                        meeting_date=meeting_date,
                        meeting_type=meeting_type,
                        meeting_title=meeting_title,
                        source_url=TEMPE_CONFIG.build_meeting_view_url(int(meeting_id)),
                        sync_status="no_agenda",
                        last_error="Meeting was canceled",
                        jurisdiction_id=pb.jurisdiction_id if pb else None,
                        public_body_id=pb.id if pb else None,
                    )
                    session.add(meeting_row)
                session.commit()
                print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: canceled (no_agenda)")
                continue

            # Ensure meeting row exists
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
                "meeting_title": meeting_title,
                "source_url": TEMPE_CONFIG.build_meeting_view_url(int(meeting_id)),
            }

            meeting_row = None
            if db_m:
                for key, val in meeting_dict.items():
                    setattr(db_m, key, val)
                db_m.sync_status = "pending"
                db_m.last_attempted_at = None
                db_m.last_error = None
                db_m.retry_count = 0
                session.flush()
                meeting_row = db_m
            else:
                pb = _resolve_pb(body_code)
                meeting_row = MeetingModel(
                    body=body_code,
                    meeting_id=meeting_id,
                    meeting_date=meeting_date,
                    meeting_type=meeting_type,
                    meeting_title=meeting_title,
                    source_url=meeting_dict["source_url"],
                    sync_status="pending",
                    jurisdiction_id=pb.jurisdiction_id if pb else None,
                    public_body_id=pb.id if pb else None,
                )
                session.add(meeting_row)
            session.commit()

            # Fetch agenda items via direct HTTP GET (no Playwright)
            update_sync_status(session, body_code, meeting_id, "in_progress")
            session.commit()

            try:
                from scraper.platforms.onbase import fetch_agenda_sync, parse_agenda_html
                html = fetch_agenda_sync(TEMPE_CONFIG, int(meeting_id))

                # Tempe OnBase returns a 200 with "Document unavailable" when the
                # agenda hasn't been published yet (future or recently posted meetings).
                # Mark as pending so future syncs retry, unless the meeting is old
                # and has been retried multiple times — then it's likely canceled.
                if "Document unavailable" in html:
                    import datetime as _dt
                    # Re-fetch the meeting row to get current retry_count
                    current_meeting = session.execute(
                        select(MeetingModel).where(
                            MeetingModel.body == body_code,
                            MeetingModel.meeting_id == meeting_id,
                        )
                    ).scalar_one_or_none()
                    retry_count = current_meeting.retry_count if current_meeting else 0
                    try:
                        meeting_age_days = (_dt.date.today() - _dt.date.fromisoformat(meeting_date)).days
                    except (ValueError, TypeError, AttributeError):
                        meeting_age_days = 0

                    if meeting_age_days > 60 and retry_count >= 1:
                        update_sync_status(session, body_code, meeting_id, "no_agenda",
                                           error="Agenda never published (likely canceled)")
                        session.commit()
                        print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: agenda never published (no_agenda)")
                    else:
                        update_sync_status(session, body_code, meeting_id, "pending",
                                           error="Agenda not yet published")
                        session.commit()
                        print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: agenda not yet published (pending)")
                    continue

                items = parse_agenda_html(html, meeting_id, body_code)
                for item in items:
                    item["source_url"] = meeting_dict["source_url"]
                    item["body"] = body_code

                # Propagate consent/non-consent category labels
                from scraper.jurisdictions.tempe import _assign_tempe_categories
                _assign_tempe_categories(items)
            except Exception as e:
                update_sync_status(session, body_code, meeting_id, "failed", error=str(e)[:500])
                session.commit()
                print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: FAILED - {e}")
                continue

            if not items:
                # Check if the page has a meeting header but no sections —
                # indicates a procedural meeting (joint meetings, special
                # sessions) with no formal agenda items.
                import re
                has_header = bool(re.search(r"<h1[^>]*>[^<]+</h1>", html))
                has_sections = bool(re.search(r"accessible-section", html))

                if has_header and not has_sections:
                    update_sync_status(session, body_code, meeting_id, "no_agenda",
                                       error="Meeting had no published agenda items")
                    session.commit()
                    print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: no agenda items (no_agenda)")
                else:
                    update_sync_status(session, body_code, meeting_id, "pending",
                                       error="Agenda format not recognized")
                    session.commit()
                    print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: 0 items (pending)")
                continue

            # Build supporting documents (packet PDF, summary PDF)
            supp_docs = []
            packet_url = m.get("agenda_packet_url", "")
            summary_url = m.get("summary_url", "")
            if packet_url:
                supp_docs.append({
                    "agenda_item_id": "0",
                    "agenda_item_number": "0",
                    "document_title": "Agenda Packet",
                    "document_url": packet_url,
                    "document_type": "Packet",
                    "file_name": f"{meeting_id}_packet.pdf",
                    "file_extension": ".pdf",
                })
            if summary_url:
                supp_docs.append({
                    "agenda_item_id": "0",
                    "agenda_item_number": "0",
                    "document_title": "Legal Action Summary",
                    "document_url": summary_url,
                    "document_type": "Summary",
                    "file_name": f"{meeting_id}_summary.pdf",
                    "file_extension": ".pdf",
                })

            # Fetch item-level supporting documents (per-item attachments)
            # by calling the OnBase ViewMeetingAgendaItem API via concurrent
            # batch fetch.  ThreadPoolExecutor parallelizes the HTTP requests
            # so 74 items finish in ~10s instead of ~75s.
            try:
                item_docs = fetch_item_details_batch(
                    TEMPE_CONFIG, int(meeting_id), items,
                    max_workers=3,
                )
                supp_docs.extend(item_docs)
            except Exception as de:
                log.debug("Item-level doc batch fetch failed for %s: %s",
                           meeting_id, de)

            # Persist
            replace_meeting_data_safe(
                session, body_code, meeting_id, meeting_dict, items,
                supporting_doc_dicts=supp_docs,
            )
            total_items += len(items)

            detail_items = [i for i in items if i["item_type"] == "item"]

            # Download documents if --download flag is set
            doc_summary = ""
            if getattr(args, "download", False):
                try:
                    from scraper.jurisdictions.tempe import download_tempe_documents
                    doc_results = download_tempe_documents(
                        meeting_id, meeting_date,
                        doc_dir=str(ROOT / "data"),
                    )
                    parts = []
                    if doc_results.get("agenda_pdf_path"):
                        parts.append("agenda")
                    if doc_results.get("packet_pdf_path"):
                        parts.append("packet")
                    if parts:
                        doc_summary = " (" + " + ".join(parts) + " downloaded)"
                except Exception as de:
                    log.warning("Document download failed for %s: %s", meeting_id, de)

            # Fetch and persist vote data from Legal Action Summary PDF
            # Only Regular City Council meetings have published summary PDFs
            vote_str = ""
            if body_code == "tempe-cc" and meeting_type == "Regular City Council Meeting":
                try:
                    vote_data = fetch_and_parse_summary(
                        int(meeting_id), meeting_date, meeting_type,
                    )
                    if vote_data["votes"]:
                        persist_votes(
                            session, body_code, meeting_id,
                            vote_data["supervisors"],
                            vote_data["votes"],
                        )
                        # Ensure vote supervisors are registered as public body members
                        _ensure_tempe_members(session, vote_data["supervisors"])
                        vote_str = f" ({len(vote_data['votes'])} votes)"
                except Exception as ve:
                    log.warning("Vote extraction failed for %s: %s", meeting_id, ve)

            ts = time.strftime("%H:%M:%S")
            ts_items_detail = len(items)
            print(f"{ts} [{idx}/{meeting_count}] {meeting_id} {meeting_date}: {ts_items_detail} item(s) ({len(detail_items)} actionable){doc_summary}{vote_str}")

        session.close()
        ts = time.strftime("%H:%M:%S")
        print(f"{ts} Synced {total_items} Tempe agenda items across {meeting_count} meeting(s)")
        return 0



    # ── Chandler sync (via AgendaQuick) ──
    if args.source == "chandler" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from db import Meeting as MeetingModel
        from sqlalchemy import select

        from scraper.jurisdictions.chandler import (
            search_chandler_meetings, parse_agenda_items,
            fetch_page, build_month_url,
            PUBLIC_BODY_CODE,
        )
        from scraper.platforms.destiny_common import fetch_agenda_memo_docs

        init_db()

        body_slugs_str = getattr(args, "bodies", None) or "all"
        if body_slugs_str == "all":
            body_slugs = None  # include all bodies from the AgendaQuick results
        else:
            body_slugs = [s.strip() for s in body_slugs_str.split(",") if s.strip()]

        # ── Determine search scope: date range → month range, or full year ──
        have_date_range = bool(getattr(args, "start_date", None) and getattr(args, "end_date", None))

        if have_date_range:
            sd = _dt.date.fromisoformat(args.start_date)
            ed = _dt.date.fromisoformat(args.end_date)
            year = sd.year
            start_month = sd.month
            end_month = ed.month
            meetings = search_chandler_meetings(
                year, body_slugs=body_slugs,
                start_month=start_month, end_month=end_month,
            )
            print(f"Chandler search: {args.start_date} to {args.end_date} (year={year}, months={start_month}–{end_month})")
        else:
            year_val = getattr(args, "year", None)
            year = int(year_val) if year_val else _dt.date.today().year
            meetings = search_chandler_meetings(year, body_slugs=body_slugs)

        if not meetings:
            if have_date_range:
                print("No Chandler meetings found in date range %s – %s." % (args.start_date, args.end_date))
            else:
                print("No Chandler meetings found for %d." % year)
            return 0
        if args.limit:
            meetings = meetings[:args.limit]

        # Post-filter by exact date within the month(s) to match the window precisely
        if have_date_range:
            sd_str = args.start_date.replace("-", "")
            ed_str = args.end_date.replace("-", "")
            meetings = [
                m for m in meetings
                if sd_str <= m.get("meeting_date", "").replace("-", "") <= ed_str
            ]
            print(f"Chandler: {len(meetings)} meeting(s) after date filter")

        print("Found %d Chandler meeting(s)" % len(meetings))

        session = get_session()
        total_items = 0
        meeting_count = len(meetings)

        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m["meeting_date"]
            body_name = m.get("body_name", "")
            body_code = m.get("body_code", "chandler-cc")
            agenda_url = m.get("agenda_url", "")
            meeting_type = m.get("meeting_type", "")
            meeting_title = body_name

            meeting_dict = {
                "meeting_id": meeting_id, "meeting_date": meeting_date,
                "meeting_type": meeting_type, "meeting_title": meeting_title,
                "source_url": agenda_url,
            }

            existing = session.execute(
                select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)
            ).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                # ── Try minutes vote extraction even for already-synced meetings ──
                _extract_chandler_minutes(session, body_code, meeting_id, meeting_date)
                session.commit()
                print("  [%d/%d] %s %s: already synced, skipping" % (idx, meeting_count, meeting_id, meeting_date))
                continue

            try:
                html = fetch_page(agenda_url, timeout=20)
                items = parse_agenda_items(html, meeting_id)

                if not items:
                    print("  [%d/%d] %s %s: no items found" % (idx, meeting_count, meeting_id, meeting_date))
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                    continue

                # ── Extract supporting docs from item memo pages ──
                supp_docs = []
                seen_memo_urls: set[str] = set()
                for it in items:
                    memo_url = it.get("agenda_item_url", "") or it.get("source_url", "")
                    if memo_url and memo_url not in seen_memo_urls:
                        seen_memo_urls.add(memo_url)
                        try:
                            docs = fetch_agenda_memo_docs(memo_url, timeout=15)
                            for doc in docs:
                                an = it.get("agenda_item_number", "")
                                doc["agenda_item_id"] = "0"
                                doc["agenda_item_number"] = an
                                supp_docs.append(doc)
                        except Exception as de:
                            log.debug("Memo docs failed for %s item %s: %s",
                                      meeting_id, it.get("agenda_item_number", ""), de)

                agenda_item_dicts = []
                for it in items:
                    an = it.get("agenda_item_number", "")
                    item_url = it.get("agenda_item_url", "") or it.get("source_url", "")
                    agenda_item_dicts.append({
                        "agenda_item_id": body_code + "-" + meeting_id + "_" + an,
                        "meeting_id": meeting_id, "agenda_item_number": an,
                        "agenda_item_title": it.get("agenda_item_title", ""),
                        "agenda_item_text": it.get("agenda_item_text", ""),
                        "agenda_item_url": item_url, "vote_or_action": "",
                        "item_type": it.get("item_type", ""),
                        "agenda_category": it.get("agenda_category", ""),
                        "source_body": body_code, "source_url": agenda_url,
                        "c_number": "", "c_number_base": "", "case_number": "",
                        "sort_order": it.get("sort_order", 0),
                    })

                replace_meeting_data_safe(
                    session, body_code, meeting_id, meeting_dict,
                    agenda_item_dicts, supporting_doc_dicts=supp_docs,
                )
                total_items += len(items)

                ts = _dt.datetime.now().strftime("%H:%M:%S")
                doc_summary = f" ({len(supp_docs)} doc(s))" if supp_docs else ""
                print("%s [%d/%d] %s %s: %d item(s)%s" % (ts, idx, meeting_count, meeting_id, meeting_date, len(items), doc_summary))

                # -- Scottsdale minutes vote extraction --
                minutes_url = m.get("minutes_url", "")
                if minutes_url:
                    try:
                        from scraper.jurisdictions.scottsdale import download_pdf, parse_minutes_votes
                        from db import persist_votes
                        pdf = download_pdf(minutes_url)
                        if pdf:
                            vote_data = parse_minutes_votes(pdf, meeting_id)
                            if vote_data.get("votes"):
                                persist_votes(
                                    session, body_code, meeting_id,
                                    vote_data["supervisors"],
                                    vote_data["votes"],
                                )
                                print("        votes: %d recorded" % len(vote_data["votes"]))
                    except Exception as ve:
                        log.debug("Scottsdale minutes parse failed: %s", ve)

                # ── Chandler Results PDF vote extraction ──
                results_url = m.get("results_url", "")
                if results_url:
                    try:
                        from scraper.jurisdictions.chandler import fetch_results_pdf_bytes, extract_pdf_text, parse_results_votes
                        from db import persist_votes
                        pdf_bytes = fetch_results_pdf_bytes(results_url)
                        if pdf_bytes:
                            text = extract_pdf_text(pdf_bytes)
                            if text:
                                vote_data = parse_results_votes(text)
                                if vote_data.get("votes"):
                                    persist_votes(
                                        session, body_code, meeting_id,
                                        vote_data["supervisors"],
                                        vote_data["votes"],
                                    )
                                    print("        votes: %d recorded" % len(vote_data["votes"]))
                    except Exception as ve:
                        log.debug("Chandler vote extraction failed: %s", ve)

                # ── Chandler meeting minutes PDF vote extraction ──
                _extract_chandler_minutes(session, body_code, meeting_id, meeting_date)

            except Exception as e:
                log.error("Failed to sync Chandler meeting %s: %s", meeting_id, e)
                try:
                    update_sync_status(session, body_code, meeting_id, "failed", error=str(e))
                except Exception:
                    pass

        session.close()
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        print("%s Synced %d Chandler agenda items across %d meeting(s)" % (ts, total_items, meeting_count))
        return 0


    # ── Goodyear sync (via AgendaQuick) ──
    if args.source == "goodyear" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, replace_meeting_data_safe
        from scraper.jurisdictions.goodyear import search_goodyear_meetings, fetch_page, parse_agenda_items, BODY_MAP, DEFAULT_BODY_SLUGS, GOODYEAR_ID, BASE_URL
        from scraper.platforms.destiny_common import fetch_agenda_memo_docs, BASE_URL as DESTINY_BASE_URL
        import urllib.parse as _gy_url
        init_db()
        body_slugs_str = getattr(args, "bodies", None) or ",".join(DEFAULT_BODY_SLUGS)
        body_slugs = None if body_slugs_str == "all" else [s.strip() for s in body_slugs_str.split(",") if s.strip()]
        _month_val = getattr(args, "month", None)
        _year_val = getattr(args, "year", None)
        if _month_val:
            year = int(_month_val.split("-")[0])
        elif _year_val:
            year = int(_year_val)
        else:
            year = _dt.date.today().year
        print("Searching Goodyear meetings for %d..." % year)
        meetings = search_goodyear_meetings(year, body_slugs=body_slugs)
        if not meetings:
            print("No Goodyear meetings found for %d." % year)
            return 0
        if args.limit:
            meetings = meetings[:args.limit]
        # Post-filter by month if --month was specified
        if _month_val:
            _before = len(meetings)
            meetings = [m for m in meetings if m.get("meeting_date", "").startswith(_month_val)]
            print("Filtered to %d meeting(s) in %s" % (len(meetings), _month_val))
            if not meetings:
                return 0
        print("Found %d Goodyear meeting(s)" % len(meetings))
        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m["meeting_date"]
            body_code = m.get("body_code", "goodyear-cc")
            agenda_url = m.get("agenda_url", "")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("meeting_title", m.get("body_name", ""))
            minutes_url = m.get("minutes_url", "")
            meeting_dict = {"meeting_id": meeting_id, "meeting_date": meeting_date, "meeting_type": meeting_type, "meeting_title": meeting_title, "source_url": agenda_url, "minutes_url": minutes_url}
            from db import Meeting as MeetingModel
            from sqlalchemy import select
            existing = session.execute(select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced" % (idx, meeting_count, meeting_id, meeting_date))
                continue
            try:
                html = fetch_page(agenda_url)
                items = parse_agenda_items(html, meeting_id)
                if not items:
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                    print("  [%d/%d] %s %s: no items" % (idx, meeting_count, meeting_id, meeting_date))
                    continue

                # ── Extract supporting docs and item text from Destiny memo pages ──
                # For each agenda item, find its View Agenda Memo link in the HTML
                # and fetch docs + text from that memo page, assigning the correct
                # agenda_item_number to each document and item text to the item.
                supp_docs = []
                if minutes_url:
                    supp_docs.append({
                        "agenda_item_id": "0",
                        "agenda_item_number": "",
                        "document_title": f"{meeting_title} Minutes",
                        "document_url": minutes_url,
                        "document_type": "Minutes",
                        "body": body_code,
                        "meeting_id": meeting_id,
                    })
                seen_memo_urls: set[str] = set()
                import re as _gy_re
                # Build item_number -> memo_url map by walking item anchor blocks.
                # Each item has an <a class="ai_link" id="ReturnToNNNN"> anchor.
                # The memo link is within the block from this anchor to the next.
                item_memo_map: dict[str, str] = {}
                anchors = list(_gy_re.finditer(
                    r'<a\s+class="ai_link"[^>]*id="ReturnTo(\d+)"',
                    html
                ))
                for i, anchor_m in enumerate(anchors):
                    block_start = anchor_m.start()
                    if i + 1 < len(anchors):
                        block_end = anchors[i + 1].start()
                    else:
                        block_end = len(html)
                    block = html[block_start:block_end]
                    item_num_m = _gy_re.search(r'<td[^>]*>(\d+)\.</td>', block)
                    if not item_num_m:
                        continue
                    item_num = item_num_m.group(1)
                    memo_m = _gy_re.search(
                        r'<a[^>]*title=[\"\']View Agenda Memo[\"\'][^>]*href=[\"\']([^\"\']+)[\"\']',
                        block, _gy_re.I
                    )
                    if memo_m:
                        memo_url = memo_m.group(1)
                        memo_url = _gy_re.sub(r'&amp;', '&', memo_url)
                        memo_url = _gy_url.urljoin(DESTINY_BASE_URL, memo_url)
                        item_memo_map[item_num] = memo_url

                # ── Helper: extract clean text from a memo page ──
                def _extract_memo_text(memo_html: str) -> str:
                    """Strip HTML from a memo page and return meaningful content."""
                    import html as _html_mod
                    t = memo_html
                    # Decode HTML entities first (before stripping tags)
                    t = _html_mod.unescape(t)
                    # Remove scripts and styles
                    t = _gy_re.sub(r'<script[^>]*>.*?</script>', '', t, flags=_gy_re.DOTALL | _gy_re.I)
                    t = _gy_re.sub(r'<style[^>]*>.*?</style>', '', t, flags=_gy_re.DOTALL | _gy_re.I)
                    # Replace breaks with newlines, then strip remaining tags
                    t = _gy_re.sub(r'<br\s*/?>', '\n', t, flags=_gy_re.I)
                    t = _gy_re.sub(r'<[^>]+>', '\n', t)
                    t = _gy_re.sub(r'\n{3,}', '\n\n', t)
                    # Clean up whitespace per line
                    lines = [l.strip() for l in t.split('\n') if l.strip()]
                    skip_prefixes = (
                        'Agenda - View Meetings', 'Print', 'Reading Mode',
                        'Back to Calendar', 'Return', 'GO TO', 'AgendaQuick',
                        'All Rights Reserved',
                    )
                    meaningful = []
                    hit_agenda_item = False
                    for line in lines:
                        if line.startswith('AGENDA ITEM #'):
                            hit_agenda_item = True
                            continue
                        if not hit_agenda_item:
                            continue
                        if line.startswith(skip_prefixes):
                            continue
                        # Strip leading colons/whitespace artifacts
                        line = line.lstrip(':').strip()
                        meaningful.append(line)
                    return '\n\n'.join(meaningful) if meaningful else ''

                # Fetch per-item memo pages for docs and item text
                for it in items:
                    an = it.get("agenda_item_number", "")
                    memo_url = item_memo_map.get(an) or it.get("agenda_item_url", "") or it.get("source_url", "")
                    if not memo_url or memo_url in seen_memo_urls:
                        continue
                    seen_memo_urls.add(memo_url)
                    try:
                        # Fetch the memo page HTML once
                        memo_raw = fetch_page(memo_url, timeout=15)
                        # Extract supporting documents from this memo
                        docs = fetch_agenda_memo_docs(memo_url, timeout=15)
                        for doc in docs:
                            doc["agenda_item_id"] = "0"
                            doc["agenda_item_number"] = an
                            supp_docs.append(doc)
                        # Extract item text from memo page
                        memo_text = _extract_memo_text(memo_raw)
                        if memo_text:
                            it["agenda_item_text"] = memo_text
                    except Exception as de:
                        log.debug("Memo fetch failed for %s item %s: %s",
                                  meeting_id, an, de)

                agenda_item_dicts = []
                for it in items:
                    an = it.get("agenda_item_number", "")
                    agenda_item_dicts.append({"agenda_item_id": body_code + "-" + meeting_id + "_" + an, "meeting_id": meeting_id, "agenda_item_number": an, "agenda_item_title": it.get("agenda_item_title", ""), "agenda_item_text": it.get("agenda_item_text", ""), "source_body": body_code, "source_url": agenda_url, "sort_order": it.get("sort_order", 0)})
                replace_meeting_data_safe(
                    session, body_code, meeting_id, meeting_dict,
                    agenda_item_dicts, supporting_doc_dicts=supp_docs,
                )
                total_items += len(items)
                doc_summary = f" ({len(supp_docs)} doc(s))" if supp_docs else ""
                print("  [%d/%d] %s %s: %d item(s)%s" % (idx, meeting_count, meeting_id, meeting_date, len(items), doc_summary))
            except Exception as e:
                log.error("Failed Goodyear meeting %s: %s", meeting_id, e)
        session.close()
        print("Synced %d Goodyear items across %d meeting(s)" % (total_items, meeting_count))
        return 0


    # ── Valley Metro sync (via browser) ──
    if args.source == "valley-metro" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status
        from db import Meeting as MeetingModel
        from sqlalchemy import select

        from scraper.jurisdictions.valley_metro import (
            search_valley_metro_meetings,
            fetch_event_detail_via_browser,
            download_document,
            extract_agenda_items_from_packet,
            CATEGORIES,
            DEFAULT_BODY_SLUGS,
        )

        init_db()

        categories_str = getattr(args, "categories", "board-meetings")
        categories = [c.strip() for c in categories_str.split(",") if c.strip()]

        # Default to 90-day window
        now = _dt.date.today()
        if args.start_date:
            sd = args.start_date
        else:
            sd = (now - _dt.timedelta(days=90)).isoformat()
        if args.end_date:
            ed = args.end_date
        else:
            ed = now.isoformat()

        headed = getattr(args, "headed", False)

        print(f"Valley Metro search: {sd} to {ed}, categories={categories}")
        meetings = await search_valley_metro_meetings(
            sd, ed, categories=categories, headed=headed,
        )
        if not meetings:
            print("No Valley Metro meetings found.")
            return 0

        if args.limit:
            meetings = meetings[:args.limit]

        print(f"Found {len(meetings)} Valley Metro meeting(s)")

        session = get_session()
        total_items = 0
        meeting_count = len(meetings)

        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m["meeting_date"]
            body_code = m.get("body_code", "valley-metro-bod")
            source_url = m.get("source_url", "")
            meeting_type = m.get("meeting_type", "Regular Meeting")
            meeting_title = m.get("meeting_title", m.get("body_name", ""))

            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
                "meeting_title": meeting_title,
                "source_url": source_url,
            }

            existing = session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == body_code,
                    MeetingModel.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()

            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: already synced")
                if existing.item_count_actual:
                    total_items += existing.item_count_actual
                continue

            if not source_url:
                print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: no URL (skip)")
                continue

            # Fetch detail page for documents
            try:
                detail = await fetch_event_detail_via_browser(source_url, headed=headed)
            except Exception as e:
                log.warning(f"Failed to fetch detail for {meeting_id}: {e}")
                detail = {
                    "description": "", "meeting_packet_url": "",
                    "agenda_url": "", "minutes_url": "",
                    "supporting_docs": [], "video_url": "",
                }

            # Build supporting documents from Info & Resources
            supp_docs = []
            seen_doc_urls: set[str] = set()
            for doc in detail.get("supporting_docs", []):
                doc_url = doc.get("url", "")
                doc_title = doc.get("title", "")
                if not doc_url or doc_url in seen_doc_urls:
                    continue
                seen_doc_urls.add(doc_url)
                if doc_url.endswith(".pdf"):
                    supp_docs.append({
                        "agenda_item_id": "0",
                        "agenda_item_number": "0",
                        "document_title": doc_title or "Meeting Document",
                        "document_url": doc_url,
                        "document_type": "Packet" if "packet" in doc_title.lower() else "Attachment",
                        "file_name": doc_url.rstrip("/").split("/")[-1],
                        "file_extension": ".pdf",
                    })

            # Also add resolved URLs if not in supporting_docs
            resolved_urls = {
                "agenda_url": detail.get("agenda_url", ""),
                "minutes_url": detail.get("minutes_url", ""),
                "meeting_packet_url": detail.get("meeting_packet_url", ""),
            }
            for label, doc_url in resolved_urls.items():
                if doc_url and doc_url not in seen_doc_urls:
                    seen_doc_urls.add(doc_url)
                    supp_docs.append({
                        "agenda_item_id": "0",
                        "agenda_item_number": "0",
                        "document_title": label.replace("_", " ").title(),
                        "document_url": doc_url,
                        "document_type": "Packet" if "packet" in label else "Agenda" if "agenda" in label else "Minutes",
                        "file_name": doc_url.rstrip("/").split("/")[-1],
                        "file_extension": ".pdf",
                    })

            # Extract agenda items from the meeting packet PDF
            packet_url = detail.get("meeting_packet_url", "")
            items = []
            if packet_url:
                try:
                    items = extract_agenda_items_from_packet(packet_url, meeting_id=meeting_id)
                except Exception as e:
                    log.warning(f"Failed to extract items from packet {packet_url[:60]}: {e}")
                if items:
                    log.info(f"  Extracted {len(items)} agenda items from packet")

            # Persist
            from db import replace_meeting_data_safe
            replace_meeting_data_safe(
                session, body_code, meeting_id, meeting_dict,
                items,
                supporting_doc_dicts=supp_docs,
            )

            ts = _dt.datetime.now().strftime("%H:%M:%S")
            item_summary = f" {len(items)} item(s)" if items else ""
            doc_summary = f" ({len(supp_docs)} doc(s))" if supp_docs else ""
            print(f"{ts} [{idx}/{meeting_count}] {meeting_id} {meeting_date}:{item_summary}{doc_summary}")

        session.close()
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        print(f"{ts} Synced Valley Metro documents across {meeting_count} meeting(s)")
        return 0


    # ── MCACC sync (Maricopa County AgendaCenter) ──
    if args.source == "mcacc" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from db import Meeting as MeetingModel
        from sqlalchemy import select

        from scraper.platforms.agendacenter import (
            MCACC_BODY_MAP, MCACC_BODY_CODES,
            body_code_to_cid, body_code_to_name,
            build_ac_search_url,
            extract_ac_meetings,
            extract_ac_agenda_items,
            _format_mm_dd_yyyy as fmt_date_fn,
        )

        init_db()

        body_codes_str = getattr(args, "bodies", None) or ",".join(MCACC_BODY_CODES)
        body_codes = [s.strip() for s in body_codes_str.split(",") if s.strip()]

        # Validate body codes
        valid_codes = MCACC_BODY_CODES
        unknown = [b for b in body_codes if b not in valid_codes]
        if unknown:
            print(f"Unknown body codes: {unknown}. Valid: {', '.join(valid_codes)}")
            return 1

        now = _dt.date.today()
        if args.start_date:
            sd = _dt.date.fromisoformat(args.start_date)
            pz_start = fmt_date_fn(args.start_date) or f"{sd.month:02d}/{sd.day:02d}/{sd.year}"
        else:
            three_months_ago = now - _dt.timedelta(days=90)
            pz_start = f"{three_months_ago.month:02d}/01/{three_months_ago.year}"

        if args.end_date:
            ed = _dt.date.fromisoformat(args.end_date)
            pz_end = fmt_date_fn(args.end_date) or f"{ed.month:02d}/{ed.day:02d}/{ed.year}"
        else:
            pz_end = f"{now.month:02d}/{min(28, now.day):02d}/{now.year}"

        # Resolve public_body_id mapping and register MCACC bodies
        from db import PublicBody as PublicBodyModel
        from scraper.platforms.agendacenter import ensure_agendacenter_public_bodies
        session = get_session()
        pb_map = {}
        try:
            # Register MCACC bodies in public_bodies if needed
            mcacc_pb_ids = ensure_agendacenter_public_bodies(session)
            session.commit()
            for pb in session.execute(select(PublicBodyModel)).scalars().all():
                pb_map[pb.body_code] = pb
        except Exception:
            pass
        session.close()

        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                grand_total_items = 0
                def _resolve_pb(body_code: str) -> PublicBodyModel | None:
                    """Look up a PublicBody by code, auto-registering if missing."""
                    pb = pb_map.get(body_code)
                    if pb is not None:
                        return pb
                    from db import ensure_public_body
                    from db import PublicBody as _PBM
                    pb_id = ensure_public_body(session, body_code)
                    if pb_id:
                        pb = session.get(_PBM, pb_id)
                        pb_map[body_code] = pb
                        return pb
                    return None

                grand_total_meetings = 0

                for body_code in body_codes:
                    cid = body_code_to_cid(body_code)
                    display_name = body_code_to_name(body_code)
                    if not cid:
                        print(f"  {body_code}: unknown CID, skipping")
                        continue

                    search_url = build_ac_search_url(cid, pz_start, pz_end)
                    print(f"{display_name} ({body_code}): searching {pz_start} to {pz_end}")

                    try:
                        meetings = await extract_ac_meetings(page, search_url, body_code)
                    except Exception as e:
                        print(f"  {body_code}: search failed: {e}")
                        continue

                    if not meetings:
                        print(f"  {body_code}: no meetings found")
                        continue

                    if args.limit:
                        meetings = meetings[:args.limit]

                    print(f"  {body_code}: found {len(meetings)} meeting(s)")

                    session = get_session()
                    total_items = 0

                    pb = _resolve_pb(body_code)

                    for idx, meeting in enumerate(meetings, 1):
                        meeting_dict = {
                            "meeting_id": meeting.meeting_id,
                            "meeting_date": meeting.meeting_date,
                            "meeting_type": meeting.meeting_type,
                            "meeting_title": meeting.meeting_title,
                            "source_url": meeting.agenda_url,
                        }

                        # Check if already synced
                        db_m = session.execute(
                            select(MeetingModel).where(
                                MeetingModel.body == body_code,
                                MeetingModel.meeting_id == meeting.meeting_id,
                            )
                        ).scalar_one_or_none()

                        if db_m and (db_m.sync_status == "no_agenda" or (db_m.sync_status == "complete" and (db_m.item_count_actual or 0) > 0)) and not args.force:
                            print(f"    [{idx}/{len(meetings)}] {meeting.meeting_id} {meeting.meeting_date}: {db_m.sync_status} (skip)")
                            if db_m.sync_status == "complete":
                                total_items += db_m.item_count_actual or 0
                            continue

                        # Build the HTML agenda URL (add ?html=true if not present)
                        agenda_html_url = meeting.agenda_url
                        if "?html=true" not in agenda_html_url:
                            if "?" in agenda_html_url:
                                agenda_html_url += "&html=true"
                            else:
                                agenda_html_url += "?html=true"

                        # Fetch agenda items
                        try:
                            items = await extract_ac_agenda_items(page, agenda_html_url, body_code)
                        except Exception as e:
                            print(f"    [{idx}/{len(meetings)}] {meeting.meeting_id} {meeting.meeting_date}: FAILED items - {e}")
                            try:
                                update_sync_status(session, body_code, meeting.meeting_id, "failed", error=str(e)[:500])
                                session.commit()
                            except Exception:
                                session.rollback()
                            continue

                        if not items:
                            # Tag as no_agenda if we got the page but no parseable items
                            print(f"    [{idx}/{len(meetings)}] {meeting.meeting_id} {meeting.meeting_date}: no items (no_agenda)")
                            replace_meeting_data_safe(session, body_code, meeting.meeting_id, meeting_dict, [])
                            continue

                        # Normalize items
                        for it in items:
                            it["meeting_id"] = meeting.meeting_id
                            it["agenda_item_id"] = f"{meeting.meeting_id}-{it.get('agenda_item_number', '0')}-item"
                            it["source_body"] = body_code
                            it["meeting_type"] = meeting.meeting_type
                            it["meeting_date"] = meeting.meeting_date

                        replace_meeting_data_safe(session, body_code, meeting.meeting_id, meeting_dict, items)
                        total_items += len(items)
                        print(f"    [{idx}/{len(meetings)}] {meeting.meeting_id} {meeting.meeting_date}: {len(items)} item(s)")

                        # ── Minutes-based member extraction ──
                        minutes_url = meeting.minutes_url
                        if minutes_url:
                            try:
                                from scraper.platforms.agendacenter import extract_members_from_minutes_pdf
                                from db import _find_or_create_person, _ensure_membership, Person, BodyMembership
                                member_data = extract_members_from_minutes_pdf(minutes_url)
                                if member_data:
                                    pb = session.execute(
                                        select(PublicBodyModel).where(
                                            PublicBodyModel.body_code == body_code
                                        )
                                    ).scalar_one_or_none()
                                    if pb:
                                        member_count = 0
                                        for md in member_data:
                                            person, _ = _find_or_create_person(
                                                session, md["name"], md["normalized_name"],
                                                log_prefix=f"mcacc[{body_code}]",
                                            )

                                            # Guard: don't create a membership for someone
                                            # who has never attended a meeting of this body.
                                            # Minutes PDFs can list non-members who
                                            # appeared (presenters, staff, public comment)
                                            # in the same "Voting Members Present" section.
                                            existing_membership = session.execute(
                                                select(BodyMembership)
                                                .where(BodyMembership.person_id == person.id)
                                                .where(BodyMembership.public_body_id == pb.id)
                                            ).scalar_one_or_none()
                                            if existing_membership:
                                                # Already a member — update role if needed
                                                if md.get("role"):
                                                    existing_membership.role = md["role"]
                                                member_count += 1
                                                continue

                                            # No existing membership; check for meeting attendance
                                            attendance_count = session.execute(
                                                text("""
                                                    SELECT COUNT(*) FROM meeting_members
                                                    WHERE member_id = :person_id
                                                      AND body = :body_code
                                                """),
                                                {"person_id": person.id, "body_code": body_code},
                                            ).scalar()

                                            if attendance_count == 0:
                                                # No meeting evidence — skip membership
                                                continue

                                            membership = _ensure_membership(
                                                session, person.id, body_code,
                                                meeting_date=_dt.date.today(),
                                            )
                                            if membership and md.get("role"):
                                                membership.role = md["role"]
                                            member_count += 1
                                        session.commit()
                                        if member_count:
                                            print(f"          members: {member_count} extracted from minutes")
                            except Exception as me:
                                log.debug("MCACC minutes member extraction failed: %s", me)

                        # ── Minutes document + outcome extraction ──
                        minutes_url = getattr(meeting, "minutes_url", "")
                        if minutes_url and not getattr(args, "dry_run", False):
                            try:
                                import tempfile, os, subprocess, io, zipfile, xml.etree.ElementTree as ET
                                from db import SupportingDocument as SdModel
                                from sqlalchemy import delete as _sql_del

                                # Add minutes as a meeting-level supporting document
                                existing_min = session.execute(
                                    select(SdModel).where(
                                        SdModel.body == body_code,
                                        SdModel.meeting_id == meeting.meeting_id,
                                        SdModel.document_url == minutes_url,
                                    )
                                ).scalar_one_or_none()
                                if not existing_min:
                                    doc = SdModel(
                                        body=body_code,
                                        meeting_id=meeting.meeting_id,
                                        agenda_item_number="",
                                        document_url=minutes_url,
                                        document_title=f"Meeting Minutes — {meeting.meeting_date}",
                                        file_type="DOCX",
                                    )
                                    session.add(doc)

                                # Try to parse minutes for outcomes
                                # (DOCX format — zip containing word/document.xml)
                                import urllib.request
                                req = urllib.request.Request(minutes_url, headers={
                                    "User-Agent": "Mozilla/5.0"
                                })
                                try:
                                    resp = urllib.request.urlopen(req, timeout=15)
                                    docx_bytes = resp.read()
                                    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
                                        if "word/document.xml" in zf.namelist():
                                            doc_xml = zf.read("word/document.xml")
                                            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                                            root = ET.fromstring(doc_xml)
                                            texts = root.findall(".//w:t", ns)
                                            full_text = " ".join(t.text or "" for t in texts)

                                            from scraper.platforms.agendacenter import extract_minutes_outcomes
                                            outcomes = extract_minutes_outcomes(full_text)
                                            if outcomes:
                                                print(f"          minutes outcomes: {len(outcomes)} found")
                                except Exception as mex:
                                    log.debug("MCACC minutes parse failed: %s", mex)

                                session.commit()
                            except Exception as mex:
                                log.debug("MCACC minutes doc processing failed: %s", mex)
                                try:
                                    session.rollback()
                                except Exception:
                                    pass

                    session.close()
                    grand_total_items += total_items
                    grand_total_meetings += len(meetings)
                    print(f"  {body_code}: synced {total_items} items across {len(meetings)} meeting(s)")

                ts = _dt.datetime.now().strftime("%H:%M:%S")
                print(f"{ts} MCACC synced {grand_total_items} items across {grand_total_meetings} meeting(s) across {len(body_codes)} body/bodies")
                return 0
            finally:
                await browser.close()
        return 0

    # ── MAG sync (via browser calendar + direct PDFs) ──
    if args.source == "mag" and args.sync:
        import datetime as _dt
        from scraper.common.mag import (
            COMMITTEES, MAG_BODY_CODES,
            ensure_mag_public_bodies,
            sync_mag_committee,
        )

        now = _dt.datetime.now()

        # Parse date range
        if args.start_date and args.end_date:
            start_date = args.start_date
            end_date = args.end_date
        elif args.year:
            start_date = f"{args.year}-01-01"
            end_date = f"{args.year}-12-31"
        elif args.month:
            y, m = args.month.split("-")
            import calendar
            last_day = calendar.monthrange(int(y), int(m))[1]
            start_date = f"{y}-{m}-01"
            end_date = f"{y}-{m}-{last_day}"
        elif args.date:
            start_date = args.date
            end_date = args.date
        else:
            start_date = now.strftime("%Y-%m-%d")
            end_date = (now + _dt.timedelta(days=365)).strftime("%Y-%m-%d")

        # Parse CIDs
        cids = [int(c.strip()) for c in args.cids.split(",") if c.strip()]
        valid_cids = [c for c in cids if c in COMMITTEES]

        if not valid_cids:
            print("No valid committee CIDs specified. Use --list-committees to see available.")
            return 0

        # Register bodies in DB
        from db import get_session, init_db
        session = get_session()
        db_map = ensure_mag_public_bodies(session)
        session.commit()
        session.close()

        api_start = _dt.datetime.strptime(start_date, "%Y-%m-%d").strftime("%m/%d/%Y")
        api_end = _dt.datetime.strptime(end_date, "%Y-%m-%d").strftime("%m/%d/%Y")

        grand_total_meetings = 0
        grand_total_items = 0

        for cid in valid_cids:
            print(f"Syncing {COMMITTEES[cid][2]} (cid={cid})...")
            synced, items = sync_mag_committee(
                cid, api_start, api_end, db_map,
                force=args.force,
            )
            grand_total_meetings += synced
            grand_total_items += items
            print(f"  {COMMITTEES[cid][2]}: {synced} meetings, {items} items")

        print(f"MAG sync complete: {grand_total_meetings} meetings, {grand_total_items} items across {len(valid_cids)} committee(s)")
        return 0

    # ── Gilbert sync (via OnBase JSON) ──
    if args.source == "gilbert" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from db import Meeting as MeetingModel
        from sqlalchemy import select

        init_db()
        from scraper.jurisdictions.gilbert import (
            search_gilbert_meetings, fetch_agenda_html,
            parse_gilbert_agenda, PUBLIC_BODY_CODE,
        )

        _month_val = getattr(args, "month", None)
        _year_val = getattr(args, "year", None)
        if _month_val:
            yr = int(_month_val.split("-")[0])
        elif _year_val:
            yr = int(_year_val)
        else:
            yr = _dt.date.today().year
        pz_start = "01/01/%d" % yr
        pz_end = _dt.date.today().strftime("%m/%d/%Y")

        meetings = search_gilbert_meetings(pz_start, pz_end)
        if not meetings:
            print("No Gilbert meetings found for year %d." % yr)
            return 0
        if args.limit:
            meetings = meetings[:args.limit]
        print("Found %d Gilbert meeting(s)" % len(meetings))

        session = get_session()
        total_items = 0
        meeting_count = len(meetings)

        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m["meeting_date"]
            body_code = m.get("body_code", PUBLIC_BODY_CODE)
            agenda_url = m["agenda_url"]
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("meeting_title", "")

            meeting_dict = {
                "meeting_id": meeting_id, "meeting_date": meeting_date,
                "meeting_type": meeting_type, "meeting_title": meeting_title,
                "source_url": agenda_url,
            }

            existing = session.execute(
                select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)
            ).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced, skipping" % (idx, meeting_count, meeting_id, meeting_date))
                continue

            try:
                html = fetch_agenda_html(int(meeting_id)) if meeting_id.isdigit() else ""
                items = parse_gilbert_agenda(html, meeting_id) if html else []

                if not items:
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                    print("  [%d/%d] %s %s: no items" % (idx, meeting_count, meeting_id, meeting_date))
                    continue

                agenda_dicts = []
                for it in items:
                    an = it.get("agenda_item_number", "")
                    agenda_dicts.append({"agenda_item_id": body_code + "-" + meeting_id + "_" + an,
                        "meeting_id": meeting_id, "agenda_item_number": an,
                        "agenda_item_title": it.get("agenda_item_title", ""),
                        "agenda_item_text": it.get("agenda_item_text", ""),
                        "agenda_item_url": "", "vote_or_action": "",
                        "item_type": it.get("item_type", ""),
                        "agenda_category": "", "source_body": body_code,
                        "source_url": agenda_url, "c_number": "",
                        "c_number_base": "", "case_number": "",
                        "sort_order": it.get("sort_order", 0)})

                replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, agenda_dicts)
                total_items += len(items)

                ts = _dt.datetime.now().strftime("%H:%M:%S")
                print("%s [%d/%d] %s %s: %d item(s)" % (ts, idx, meeting_count, meeting_id, meeting_date, len(items)))

                # -- Scottsdale minutes vote extraction --
                minutes_url = m.get("minutes_url", "")
                if minutes_url:
                    try:
                        from scraper.jurisdictions.scottsdale import download_pdf, parse_minutes_votes
                        from db import persist_votes
                        pdf = download_pdf(minutes_url)
                        if pdf:
                            vote_data = parse_minutes_votes(pdf, meeting_id)
                            if vote_data.get("votes"):
                                persist_votes(
                                    session, body_code, meeting_id,
                                    vote_data["supervisors"],
                                    vote_data["votes"],
                                )
                                print("        votes: %d recorded" % len(vote_data["votes"]))
                    except Exception as ve:
                        log.debug("Scottsdale minutes parse failed: %s", ve)

                # ── Gilbert minutes vote extraction ──
                try:
                    minutes_html = fetch_agenda_html(int(meeting_id)) if meeting_id.isdigit() else ""
                    if minutes_html:
                        import re as _re
                        clean = _re.sub("<[^>]+>", " ", minutes_html)
                        clean = clean.replace("\\u00a0", " ").replace("&#xa0;", " ")
                        clean = _re.sub("\\s+", " ", clean)
                        motions = list(_re.finditer("(A MOTION was made by[^.]*\\.)", clean))
                        if motions:
                            print("        motions: %d found in minutes" % len(motions))
                except Exception as ve:
                    log.debug("Gilbert minutes parse failed: %s", ve)

            except Exception as e:
                log.error("Gilbert sync failed for %s: %s", meeting_id, e)
                try:
                    update_sync_status(session, body_code, meeting_id, "failed", error=str(e))
                except Exception:
                    pass

        session.close()
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        print("%s Synced %d Gilbert agenda items across %d meeting(s)" % (ts, total_items, meeting_count))
        return 0

    # ── Scottsdale sync (via PDF archive) ──
    if args.source == "scottsdale" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from db import Meeting as MeetingModel
        from sqlalchemy import select

        from scraper.jurisdictions.scottsdale import (
            search_meetings, download_pdf, parse_agenda_items,
            extract_supporting_docs,
            PUBLIC_BODY_CODE,
        )

        init_db()

        _month_val = getattr(args, "month", None)
        _year_val = getattr(args, "year", None)
        if _month_val:
            yr = int(_month_val.split("-")[0])
        elif _year_val:
            yr = int(_year_val)
        else:
            yr = _dt.date.today().year

        meetings = search_meetings(int(yr))
        if not meetings:
            print("No Scottsdale meetings found for year %d." % yr)
            return 0
        if args.limit:
            meetings = meetings[:args.limit]
        # Post-filter by month if --month was specified
        if _month_val:
            _before = len(meetings)
            meetings = [m for m in meetings if m.get("date", m.get("meeting_date", "")).startswith(_month_val)]
            print("Filtered to %d meeting(s) in %s" % (len(meetings), _month_val))
            if not meetings:
                return 0
        print("Found %d Scottsdale meeting(s)" % len(meetings))

        session = get_session()
        total_items = 0
        meeting_count = len(meetings)

        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m["meeting_date"]
            body_code = PUBLIC_BODY_CODE
            agenda_url = m.get("agenda_url", "")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("body_name", "")

            meeting_dict = {
                "meeting_id": meeting_id, "meeting_date": meeting_date,
                "meeting_type": meeting_type, "meeting_title": meeting_title,
                "source_url": agenda_url,
            }

            existing = session.execute(
                select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)
            ).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced, skipping" % (idx, meeting_count, meeting_id, meeting_date))
                continue

            try:
                items = []
                if agenda_url:
                    pdf = download_pdf(agenda_url)
                    if pdf:
                        items = parse_agenda_items(pdf, meeting_id)

                if not items:
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                    print("  [%d/%d] %s %s: no items" % (idx, meeting_count, meeting_id, meeting_date))
                    continue

                agenda_dicts = []
                for it in items:
                    an = it.get("agenda_item_number", "")
                    agenda_dicts.append({"agenda_item_id": body_code + "-" + meeting_id + "_" + an,
                        "meeting_id": meeting_id, "agenda_item_number": an,
                        "agenda_item_title": it.get("agenda_item_title", ""),
                        "agenda_item_text": it.get("agenda_item_text", ""),
                        "agenda_item_url": "", "vote_or_action": it.get("vote_or_action", ""),
                        "item_type": it.get("item_type", ""),
                        "agenda_category": "", "source_body": body_code,
                        "source_url": agenda_url, "c_number": "",
                        "c_number_base": "", "case_number": "",
                        "sort_order": it.get("sort_order", 0)})

                                # ── Extract supporting docs embedded in the PDF ──
                supp_docs = []
                if pdf:
                    try:
                        supp_docs = extract_supporting_docs(pdf, items=items)
                    except Exception as de:
                        log.debug("Scottsdale doc extraction failed: %s", de)

                replace_meeting_data_safe(
                    session, body_code, meeting_id, meeting_dict,
                    agenda_dicts, supporting_doc_dicts=supp_docs,
                )
                total_items += len(items)

                ts = _dt.datetime.now().strftime("%H:%M:%S")
                print("%s [%d/%d] %s %s: %d item(s)" % (ts, idx, meeting_count, meeting_id, meeting_date, len(items)))

                # -- Scottsdale minutes vote extraction --
                minutes_url = m.get("minutes_url", "")
                if minutes_url:
                    try:
                        from scraper.jurisdictions.scottsdale import download_pdf, parse_minutes_votes
                        from db import persist_votes
                        pdf = download_pdf(minutes_url)
                        if pdf:
                            vote_data = parse_minutes_votes(pdf, meeting_id)
                            if vote_data.get("votes"):
                                persist_votes(
                                    session, body_code, meeting_id,
                                    vote_data["supervisors"],
                                    vote_data["votes"],
                                )
                                print("        votes: %d recorded" % len(vote_data["votes"]))
                    except Exception as ve:
                        log.debug("Scottsdale minutes parse failed: %s", ve)

            except Exception as e:
                log.error("Scottsdale sync failed for %s: %s", meeting_id, e)
                try:
                    update_sync_status(session, body_code, meeting_id, "failed", error=str(e))
                except Exception:
                    pass

        session.close()
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        print("%s Synced %d Scottsdale agenda items across %d meeting(s)" % (ts, total_items, meeting_count))
        return 0

    # ── Scottsdale Boards sync ──
    if args.source == "scottsdale-boards" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from db import Meeting as MeetingModel
        from sqlalchemy import select

        from scraper.jurisdictions.scottsdale_boards import search_board_meetings, BOARDS
        from scraper.jurisdictions.scottsdale import download_pdf, parse_agenda_items

        init_db()

        for slug, cfg in sorted(BOARDS.items()):
            body_code = cfg["code"]
            sd = getattr(args, "start_date", None)
            ed = getattr(args, "end_date", None)
            yr = getattr(args, "year", None)
            if sd and ed:
                meetings = search_board_meetings(slug, start_date=sd, end_date=ed)
            elif yr:
                meetings = search_board_meetings(slug, year=int(yr))
            else:
                meetings = search_board_meetings(slug)
            if not meetings:
                print("  %s: no meetings found" % cfg["name"])
                continue
            if args.limit:
                meetings = meetings[:args.limit]

            session = get_session()
            total_items = 0
            for idx, m in enumerate(meetings, 1):
                meeting_id = m["meeting_id"]
                meeting_date = m["meeting_date"]
                meeting_type = m["meeting_type"]
                agenda_url = m["agenda_url"]
                meeting_dict = {
                    "meeting_id": meeting_id, "meeting_date": meeting_date,
                    "meeting_type": meeting_type, "meeting_title": m["body_name"],
                    "source_url": agenda_url,
                }
                existing = session.execute(
                    select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)
                ).scalar_one_or_none()
                if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                    continue
                try:
                    pdf = download_pdf(agenda_url)
                    items = parse_agenda_items(pdf, meeting_id) if pdf else []
                    if not items:
                        replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                        continue
                    agenda_dicts = []
                    for it in items:
                        an = it.get("agenda_item_number", "")
                        agenda_dicts.append({"agenda_item_id": body_code + "-" + meeting_id + "_" + an,
                            "meeting_id": meeting_id, "agenda_item_number": an,
                            "agenda_item_title": it.get("agenda_item_title", ""),
                            "agenda_item_text": it.get("agenda_item_text", ""),
                            "agenda_item_url": "", "vote_or_action": it.get("vote_or_action", ""),
                            "item_type": "", "agenda_category": "", "source_body": body_code,
                            "source_url": agenda_url, "c_number": "", "c_number_base": "", "case_number": "",
                            "sort_order": it.get("sort_order", 0)})
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, agenda_dicts)
                    total_items += len(items)
                    ts = _dt.datetime.now().strftime("%H:%M:%S")
                    print("  %s %s: %d items" % (meeting_date, meeting_id[:35], len(items)))
                except Exception as e:
                    log.debug("Failed %s: %s", meeting_id, e)
            session.close()
            print("  %s: %d items total" % (cfg["name"], total_items))

        return 0

    # ── Mesa sync (via Legistar) ──
    if args.source == "mesa" and args.sync:
        import datetime as _dt
        import logging
        from db import get_session, init_db, replace_meeting_data_safe

        log = logging.getLogger("mesa-sync")

        from scraper.jurisdictions.mesa import (
            search_mesa_meetings,
            fetch_agenda_items_async,
            fetch_page,
            parse_legislation_detail_from_html,
            BODY_CODE_MAP,
            PUBLIC_BODY_CODE,
            DEFAULT_BODY_SLUGS,
        )

        init_db()

        body_slugs_str = getattr(args, "bodies", None) or ",".join(DEFAULT_BODY_SLUGS)
        body_slugs = [s.strip() for s in body_slugs_str.split(",") if s.strip()]

        # ── Determine search scope: date range, year, or default ──
        have_date_range = bool(getattr(args, "start_date", None) and getattr(args, "end_date", None))
        year_val = getattr(args, "year", None)

        if have_date_range:
            sd = args.start_date
            ed = args.end_date
            meetings = search_mesa_meetings(
                body_slugs=body_slugs,
                start_date=sd,
                end_date=ed,
            )
            print(f"Mesa search: {sd} to {ed}")
        elif year_val:
            year = int(year_val)
            meetings = search_mesa_meetings(body_slugs=body_slugs, year=year)
        else:
            year = _dt.date.today().year
            meetings = search_mesa_meetings(body_slugs=body_slugs, year=year)

        if not meetings:
            if have_date_range:
                print("No Mesa meetings found in date range %s – %s." % (args.start_date, args.end_date))
            else:
                print("No Mesa meetings found for %d." % year)
            return 0
        if args.limit:
            meetings = meetings[:args.limit]
        print("Found %d Mesa meeting(s)" % len(meetings))

        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from db import Meeting as MeetingModel
        from sqlalchemy import select

        session = get_session()
        total_items = 0
        total_docs = 0
        meeting_count = len(meetings)
        # Track seen (agenda_item_id, document_url) pairs across ALL meetings
        # to avoid the global UNIQUE constraint on supporting_documents
        _seen_sd_keys: set[tuple] = set()

        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m["meeting_date"]
            body_slug = m.get("body_slug", "mesa-city-council")
            # Use BODY_CODE_MAP for correct mapping (slug → code)
            body_code = BODY_CODE_MAP.get(body_slug, "mesa-city-council")
            detail_url = m.get("meeting_detail_url", "")
            # Fall back to agenda_url if no detail URL
            if not detail_url:
                detail_url = m.get("agenda_url", "")
            meeting_type = m.get("meeting_type", "") or m.get("body_name", "")
            meeting_title = m.get("meeting_title", m.get("body_name", ""))

            meeting_dict = {
                "meeting_id": meeting_id, "meeting_date": meeting_date,
                "meeting_type": meeting_type, "meeting_title": meeting_title,
                "source_url": detail_url,
            }
            # Include minutes URL if available (for vote extraction)
            if m.get("minutes_url"):
                meeting_dict["minutes_url"] = m["minutes_url"]

            # ── Skip already-complete meetings (unless --force) ──
            existing = session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == body_code,
                    MeetingModel.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()
            # Brief 006: a "complete" meeting with 0 items may have had its
            # agenda published since the row was created — don't skip it.
            if existing and existing.sync_status == "complete" \
                    and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced" % (idx, meeting_count, meeting_id, meeting_date))
                continue

            try:
                items = await fetch_agenda_items_async(detail_url, meeting_id, body_code)

                if not items:
                    print("  [%d/%d] %s %s: no items found" % (idx, meeting_count, meeting_id, meeting_date))
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                    continue

                agenda_item_dicts = []
                supporting_doc_dicts = []

                for it in items:
                    an = it.get("agenda_item_number", "")
                    item_aiid = body_code + "-" + meeting_id + "_" + an

                    agenda_item_dicts.append({
                        "agenda_item_id": item_aiid,
                        "meeting_id": meeting_id, "agenda_item_number": an,
                        "agenda_item_title": it.get("agenda_item_title", ""),
                        "agenda_item_text": it.get("agenda_item_text", ""),
                        "agenda_item_url": it.get("agenda_item_url", ""),
                        "vote_or_action": it.get("vote_or_action", ""),
                        "item_type": it.get("item_type", ""),
                        "agenda_category": it.get("agenda_category", ""),
                        "source_body": body_code, "source_url": detail_url,
                        "c_number": "", "c_number_base": "", "case_number": "",
                        "sort_order": it.get("sort_order", 0),
                    })

                    # Fetch supporting documents from LegislationDetail page
                    leg_url = it.get("legislation_url", "")
                    if leg_url:
                        try:
                            leg_html = await asyncio.to_thread(fetch_page, leg_url)
                            leg_detail = parse_legislation_detail_from_html(leg_html)
                            attachments = leg_detail.get("attachments", [])
                            for att in attachments:
                                att_title = att.get("title", "").strip()
                                att_url = att.get("url", "").strip()
                                if att_title and att_url:
                                    # Derive file type from title or URL
                                    doc_type = "Attachment"
                                    tl = att_title.lower()
                                    if "minutes" in tl:
                                        doc_type = "Minutes"
                                    elif "agenda" in tl:
                                        doc_type = "Agenda"
                                    elif "staff report" in tl or "staff" in tl:
                                        doc_type = "Staff Report"
                                    elif "exhibit" in tl:
                                        doc_type = "Exhibit"
                                    elif "attachment" in tl:
                                        doc_type = "Attachment"
                                    # Guess file extension from URL
                                    ext = ""
                                    if ".pdf" in att_url.lower():
                                        ext = ".pdf"
                                    elif ".docx" in att_url.lower():
                                        ext = ".docx"
                                    elif ".doc" in att_url.lower():
                                        ext = ".doc"

                                    supporting_doc_dicts.append({
                                        "agenda_item_id": "0",
                                        "agenda_item_number": an,
                                        "document_title": att_title,
                                        "document_url": att_url,
                                        "document_type": doc_type,
                                        "file_name": f"{body_code}_{meeting_id}_{an}_{att_title[:60]}",
                                        "file_extension": ext,
                                    })
                        except Exception as leg_err:
                            log.warning(
                                "Failed to fetch legislation detail for %s: %s",
                                leg_url, leg_err,
                            )

                # Deduplicate supporting documents across ALL meetings in this
                # run — the UNIQUE constraint (agenda_item_id, document_url) is
                # global, not per-meeting, and we use agenda_item_id=0 for all
                # legislatively-attached docs pending item-level linking.
                deduped_docs = []
                for sd in supporting_doc_dicts:
                    key = (sd.get("agenda_item_id", 0), sd.get("document_url", ""))
                    if key not in _seen_sd_keys:
                        _seen_sd_keys.add(key)
                        deduped_docs.append(sd)

                replace_meeting_data_safe(
                    session, body_code, meeting_id, meeting_dict,
                    agenda_item_dicts,
                    supporting_doc_dicts=deduped_docs,
                )
                total_items += len(items)
                total_docs += len(supporting_doc_dicts)

                ts = _dt.datetime.now().strftime("%H:%M:%S")
                doc_info = f", {len(supporting_doc_dicts)} doc(s)" if supporting_doc_dicts else ""
                print("%s [%d/%d] %s %s: %d item(s)%s" % (ts, idx, meeting_count, meeting_id, meeting_date, len(items), doc_info))

            except Exception as e:
                log.error("Failed to sync Mesa meeting %s: %s", meeting_id, e)
                try:
                    update_sync_status(session, body_code, meeting_id, "failed", error=str(e))
                except Exception:
                    pass

        session.close()
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        print("%s Synced %d Mesa agenda items and %d supporting documents across %d meeting(s)" % (ts, total_items, total_docs, meeting_count))
        return 0

    # ── Glendale sync (via Legistar) ──
    if args.source == "glendale" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, replace_meeting_data_safe
        from scraper.jurisdictions.glendale import search_glendale_meetings_sync, fetch_agenda_items_async, BODY_CODE_MAP, DEFAULT_BODY_SLUGS
        init_db()
        body_slugs_str = getattr(args, "bodies", None) or ",".join(DEFAULT_BODY_SLUGS)
        body_slugs = [s.strip() for s in body_slugs_str.split(",") if s.strip()]

        have_date_range = bool(getattr(args, "start_date", None) and getattr(args, "end_date", None))
        if have_date_range:
            sd = args.start_date
            ed = args.end_date
            meetings = search_glendale_meetings_sync(
                body_slugs=body_slugs,
                start_date=sd,
                end_date=ed,
            )
            print(f"Glendale search: {sd} to {ed}")
        else:
            year_val = getattr(args, "year", None)
            year = int(year_val) if year_val else _dt.date.today().year
            print("Searching Glendale meetings for %d..." % year)
            meetings = search_glendale_meetings_sync(body_slugs=body_slugs)

        if not meetings:
            if have_date_range:
                print("No Glendale meetings found in date range %s – %s." % (args.start_date, args.end_date))
            else:
                print("No Glendale meetings found for %d." % year)
            return 0
        print("Found %d Glendale meeting(s)" % len(meetings))
        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m["meeting_date"]
            body_code = BODY_CODE_MAP.get(m.get("body_slug", ""), "glendale-cc")
            agenda_url = m.get("agenda_url", "")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("meeting_title", m.get("body_name", ""))
            minutes_url = m.get("minutes_url", "")
            meeting_dict = {"meeting_id": meeting_id, "meeting_date": meeting_date, "meeting_type": meeting_type, "meeting_title": meeting_title, "source_url": agenda_url, "minutes_url": minutes_url}
            supp_docs = []
            if minutes_url:
                supp_docs.append({
                    "agenda_item_id": "0",
                    "agenda_item_number": "",
                    "document_title": f"{meeting_title} Minutes",
                    "document_url": minutes_url,
                    "document_type": "Minutes",
                    "body": body_code,
                    "meeting_id": meeting_id,
                })
            from db import Meeting as MeetingModel
            from sqlalchemy import select
            existing = session.execute(select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)).scalar_one_or_none()
            # Brief 006: same guard fix as phoenix-rss — a "complete" meeting
            # with 0 items may have had its agenda published since the row was
            # created weeks early; don't skip it.
            if existing and existing.sync_status == "complete" \
                    and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced" % (idx, meeting_count, meeting_id, meeting_date))
                continue
            try:
                items = fetch_agenda_items_async(agenda_url, meeting_id, body_code)
                if not items:
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [], supporting_doc_dicts=supp_docs)
                    print("  [%d/%d] %s %s: no items" % (idx, meeting_count, meeting_id, meeting_date))
                    continue
                agenda_item_dicts = []
                for it in items:
                    an = it.get("agenda_item_number", "")
                    agenda_item_dicts.append({"agenda_item_id": body_code + "-" + meeting_id + "_" + an, "meeting_id": meeting_id, "agenda_item_number": an, "agenda_item_title": it.get("agenda_item_title", ""), "agenda_item_text": it.get("agenda_item_text", ""), "source_body": body_code, "source_url": agenda_url, "sort_order": it.get("sort_order", 0)})
                replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, agenda_item_dicts, supporting_doc_dicts=supp_docs)
                total_items += len(items)
                print("  [%d/%d] %s %s: %d item(s)" % (idx, meeting_count, meeting_id, meeting_date, len(items)))
            except Exception as e:
                log.error("Failed Glendale meeting %s: %s", meeting_id, e)
        session.close()
        print("Synced %d Glendale items across %d meeting(s)" % (total_items, meeting_count))
        return 0

    # ── Glendale-new sync (via AgendaQuick) ──
    if args.source == "glendale-new" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, replace_meeting_data_safe
        from scraper.jurisdictions.glendale_new import (
            search_glendale_meetings,
            parse_agenda_items,
            fetch_page,
            BASE_URL,
            GLENDALE_ID,
            DEFAULT_BODY_SLUGS,
            BODY_MAP,
        )
        init_db()
        body_slugs_str = getattr(args, "bodies", None) or ",".join(DEFAULT_BODY_SLUGS)
        body_slugs = None if body_slugs_str == "all" else [s.strip() for s in body_slugs_str.split(",") if s.strip()]
        _month_val = getattr(args, "month", None)
        _year_val = getattr(args, "year", None)
        if _month_val:
            year = int(_month_val.split("-")[0])
        elif _year_val:
            year = int(_year_val)
        else:
            year = _dt.date.today().year
        print("Searching Glendale (AgendaQuick) meetings for %d..." % year)
        meetings = search_glendale_meetings(year, body_slugs=body_slugs)
        if args.limit:
            meetings = meetings[:args.limit]
        if not meetings:
            print("No Glendale (AgendaQuick) meetings found for %d." % year)
            return 0
        # Post-filter by month if --month was specified
        if _month_val:
            _before = len(meetings)
            meetings = [m for m in meetings if m.get("meeting_date", "").startswith(_month_val)]
            print("Filtered to %d meeting(s) in %s" % (len(meetings), _month_val))
            if not meetings:
                return 0
        print("Found %d Glendale (AgendaQuick) meeting(s)" % len(meetings))
        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_seq"]
            meeting_date = m["meeting_date"]
            body_code = m.get("body_code", "glendale-cc")
            agenda_url = m.get("agenda_url", "")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("body_name", "")
            minutes_url = m.get("minutes_url", "")
            meeting_dict = {"meeting_id": meeting_id, "meeting_date": meeting_date, "meeting_type": meeting_type, "meeting_title": meeting_title, "source_url": agenda_url, "minutes_url": minutes_url}
            supp_docs = []
            if minutes_url:
                supp_docs.append({
                    "agenda_item_id": "0",
                    "agenda_item_number": "",
                    "document_title": f"{meeting_title} Minutes",
                    "document_url": minutes_url,
                    "document_type": "Minutes",
                    "body": body_code,
                    "meeting_id": meeting_id,
                })
            from db import Meeting as MeetingModel
            from sqlalchemy import select
            existing = session.execute(select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced" % (idx, meeting_count, meeting_id, meeting_date))
                continue
            try:
                html = fetch_page(agenda_url)
                items = parse_agenda_items(html, meeting_id)
                if not items:
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [], supporting_doc_dicts=supp_docs)
                    print("  [%d/%d] %s %s: no items" % (idx, meeting_count, meeting_id, meeting_date))
                    continue
                agenda_item_dicts = []
                for it in items:
                    an = it.get("agenda_item_number", "")
                    agenda_item_dicts.append({"agenda_item_id": body_code + "-" + meeting_id + "_" + an, "meeting_id": meeting_id, "agenda_item_number": an, "agenda_item_title": it.get("agenda_item_title", ""), "agenda_item_text": it.get("agenda_item_text", ""), "source_body": body_code, "source_url": agenda_url, "sort_order": it.get("sort_order", 0)})
                replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, agenda_item_dicts, supporting_doc_dicts=supp_docs)

                # ── Glendale Results PDF vote extraction ──
                try:
                    from scraper.jurisdictions.glendale_new import fetch_results_pdf_bytes, extract_pdf_text, parse_results_votes
                    from db.persist import persist_votes
                    results_url = m.get("results_url", "")
                    if results_url:
                        pdf_bytes = fetch_results_pdf_bytes(results_url)
                        if pdf_bytes:
                            text = extract_pdf_text(pdf_bytes)
                            if text:
                                vote_data = parse_results_votes(text)
                                if vote_data.get("votes"):
                                    persist_votes(session, body_code, meeting_id, vote_data["supervisors"], vote_data["votes"])
                                    print("      votes: %d" % len(vote_data["votes"]))
                except Exception as ve:
                    log.debug("Glendale vote extraction failed for %s: %s", meeting_id, ve)

                total_items += len(items)
                print("  [%d/%d] %s %s: %d item(s)" % (idx, meeting_count, meeting_id, meeting_date, len(items)))
            except Exception as e:
                log.error("Failed Glendale (AgendaQuick) meeting %s: %s", meeting_id, e)
        session.close()
        print("Synced %d Glendale (AgendaQuick) items across %d meeting(s)" % (total_items, meeting_count))
        return 0

    # ── Peoria sync (via NovusAgenda) ──
    # ── Phoenix sync (new RSS/HTML-based scraper) ──
    if args.source in ("phoenix", "phoenix-rss") and args.sync:
        import datetime as _dt
        from db import get_session, init_db, replace_meeting_data_safe
        from scraper.jurisdictions.phoenix_rss import (
            search_meetings_via_html,
            fetch_meeting_items_via_rss,
            PUBLIC_BODY_CODE,
        )
        init_db()
        year_val = getattr(args, "year", None)
        year = int(year_val) if year_val else _dt.date.today().year
        print("Searching Phoenix meetings for %d..." % year)
        meetings = search_meetings_via_html(year)
        if args.limit:
            meetings = meetings[:args.limit]
        if not meetings:
            print("No Phoenix RSS meetings found for %d." % year)
            return 0
        print("Found %d Phoenix meeting(s) via RSS" % len(meetings))
        session = get_session()
        total_items = 0
        total_docs = 0
        meeting_count = len(meetings)
        from db import Meeting as MeetingModel
        from sqlalchemy import select
        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_guid = m.get("meeting_guid", "")
            meeting_date = m["meeting_date"]
            body_code = m.get("body_code", "phoenix-cc")
            detail_url = m.get("meeting_detail_url", "")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("meeting_title", "")
            meeting_dict = {"meeting_id": meeting_id, "meeting_date": meeting_date, "meeting_type": meeting_type, "meeting_title": meeting_title, "source_url": detail_url}
            existing = session.execute(select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)).scalar_one_or_none()
            # Brief 006: don't skip "complete" meetings that have 0 items — the
            # row was created weeks early from the calendar and the agenda may
            # have dropped since. Only a complete meeting WITH items is done.
            if existing and existing.sync_status == "complete" \
                    and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced, %d items" % (idx, meeting_count, meeting_id, meeting_date, existing.item_count_actual or 0))
                total_items += existing.item_count_actual or 0
                continue
            try:
                items, supp_docs = fetch_meeting_items_via_rss(
                    meeting_id, meeting_guid, body_code,
                    leg_limit=getattr(args, "leg_limit", 0),
                )
                replace_meeting_data_safe(
                    session, body_code, meeting_id, meeting_dict,
                    items, supporting_doc_dicts=supp_docs,
                )
                total_items += len(items)
                total_docs += len(supp_docs)
                doc_summary = f" ({len(supp_docs)} doc(s))" if supp_docs else ""
                print("  [%d/%d] %s %s: %d items synced%s" % (idx, meeting_count, meeting_id, meeting_date, len(items), doc_summary))
            except Exception as e:
                log.error("Failed Phoenix RSS meeting %s: %s", meeting_id, e)
                import traceback; traceback.print_exc()
                # Roll back the session so the next meeting's query doesn't hang on broken transaction
                session.rollback()
        session.close()
        print("Synced %d Phoenix RSS items across %d meeting(s) (%d docs)" % (total_items, meeting_count, total_docs))
        return 0

    if args.source == "peoria" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from db import Meeting as MeetingModel
        from sqlalchemy import select
        from scraper.jurisdictions.peoria import (
            search_meetings, extract_agenda_items,
            extract_supporting_docs,
        )

        init_db()
        print("Searching Peoria meetings via PrimeGov...")
        meetings = search_meetings()
        if not meetings:
            print("No Peoria meetings found.")
            return 0

        # Filter by date range
        start_date_str = getattr(args, "start_date", None)
        end_date_str = getattr(args, "end_date", None)
        if start_date_str:
            meetings = [m for m in meetings if m.get("meeting_date", "") >= start_date_str]
        if end_date_str:
            meetings = [m for m in meetings if m.get("meeting_date", "") <= end_date_str]
        if not meetings:
            print("No Peoria meetings found in date range.")
            return 0
        if args.limit:
            meetings = meetings[:args.limit]
        print("Found %d Peoria meeting(s)" % len(meetings))

        session = get_session()
        meeting_count = len(meetings)
        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m.get("meeting_date", "")
            body_code = m.get("body_code", "peoria-cc")

            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": m.get("meeting_type", ""),
                "meeting_title": m.get("meeting_title", ""),
                "source_url": m.get("source_url", ""),
            }

            existing = session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == body_code,
                    MeetingModel.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced" % (idx, meeting_count, meeting_id, meeting_date))
                continue

            # Extract agenda items
            items = []
            docs = []
            if m.get("agenda_url"):
                items = extract_agenda_items(m["agenda_url"], meeting_id)
            if m.get("compiled_docs"):
                docs = extract_supporting_docs(m["compiled_docs"])

            try:
                replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, list(items), supporting_doc_dicts=list(docs))
                ts = _dt.datetime.now().strftime("%H:%M:%S")
                status = "complete" if items else "no_agenda"
                print("%s [%d/%d] %s %s: %d items, %d docs (%s)" % (ts, idx, meeting_count, meeting_id, meeting_date, len(items), len(docs), status))
                update_sync_status(session, body_code, meeting_id, status)
                session.commit()
            except Exception as e:
                log.error("Failed Peoria meeting %s: %s", meeting_id, e)
                try:
                    update_sync_status(session, body_code, meeting_id, "failed", error=str(e)[:500])
                    session.commit()
                except Exception:
                    pass

        session.close()
        print("Synced %d Peoria meeting(s)" % meeting_count)
        return 0

    # ── Wickenburg sync (via Destiny/AgendaQuick) ──
    if args.source == "wickenburg" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from scraper.jurisdictions.wickenburg import (
            search_wickenburg_meetings, parse_agenda_items,
            fetch_page, BASE_URL, ORG_ID,
            DEFAULT_BODY_SLUGS,
        )
        from scraper.platforms.destiny_common import fetch_agenda_memo_docs
        init_db()
        body_slugs_str = getattr(args, "bodies", None) or ",".join(DEFAULT_BODY_SLUGS)
        body_slugs = [s.strip() for s in body_slugs_str.split(",") if s.strip()]
        _month_val = getattr(args, "month", None)
        _year_val = getattr(args, "year", None)
        if _month_val:
            year = int(_month_val.split("-")[0])
        elif _year_val:
            year = int(_year_val)
        else:
            year = _dt.date.today().year
        print("Searching Wickenburg meetings for %d..." % year)
        meetings = search_wickenburg_meetings(year, body_slugs=body_slugs)
        if args.limit:
            meetings = meetings[:args.limit]
        if not meetings:
            print("No Wickenburg meetings found for %d." % year)
            return 0
        # Post-filter by month if --month was specified
        if _month_val:
            _before = len(meetings)
            meetings = [m for m in meetings if m.get("meeting_date", "").startswith(_month_val)]
            print("Filtered to %d meeting(s) in %s" % (len(meetings), _month_val))
            if not meetings:
                return 0
        print("Found %d Wickenburg meeting(s)" % len(meetings))
        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        from db import Meeting as MeetingModel
        from sqlalchemy import select
        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m["meeting_date"]
            body_code = m.get("body_code", "wickenburg-cc")
            agenda_url = m.get("agenda_url", "")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("body_name", "")
            meeting_dict = {"meeting_id": meeting_id, "meeting_date": meeting_date, "meeting_type": meeting_type, "meeting_title": meeting_title, "source_url": agenda_url}
            existing = session.execute(select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced" % (idx, meeting_count, meeting_id, meeting_date))
                continue
            try:
                html = fetch_page(agenda_url)
                items = parse_agenda_items(html, meeting_id)
                if not items:
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                    print("  [%d/%d] %s %s: no items" % (idx, meeting_count, meeting_id, meeting_date))
                    continue

                # ── Extract supporting docs from Destiny memo pages ──
                supp_docs = []
                seen_memo_urls: set[str] = set()
                for it in items:
                    memo_url = it.get("agenda_item_url", "") or it.get("source_url", "")
                    if memo_url and memo_url not in seen_memo_urls:
                        seen_memo_urls.add(memo_url)
                        try:
                            docs = fetch_agenda_memo_docs(memo_url, timeout=15)
                            for doc in docs:
                                an = it.get("agenda_item_number", "")
                                doc["agenda_item_id"] = "0"
                                doc["agenda_item_number"] = an
                                supp_docs.append(doc)
                        except Exception as de:
                            log.debug("Memo docs failed for %s item %s: %s",
                                      meeting_id, it.get("agenda_item_number", ""), de)

                agenda_item_dicts = []
                for it in items:
                    an = it.get("agenda_item_number", "")
                    item_url = it.get("agenda_item_url", "") or it.get("source_url", "")
                    agenda_item_dicts.append({"agenda_item_id": body_code + "-" + meeting_id + "_" + an, "meeting_id": meeting_id, "agenda_item_number": an, "agenda_item_title": it.get("agenda_item_title", ""), "agenda_item_text": it.get("agenda_item_text", ""), "agenda_item_url": item_url, "source_body": body_code, "source_url": agenda_url, "sort_order": it.get("sort_order", 0)})
                replace_meeting_data_safe(
                    session, body_code, meeting_id, meeting_dict,
                    agenda_item_dicts, supporting_doc_dicts=supp_docs,
                )
                total_items += len(items)
                doc_summary = f" ({len(supp_docs)} doc(s))" if supp_docs else ""
                print("  [%d/%d] %s %s: %d item(s)%s" % (idx, meeting_count, meeting_id, meeting_date, len(items), doc_summary))
            except Exception as e:
                log.error("Failed Wickenburg meeting %s: %s", meeting_id, e)
        session.close()
        print("Synced %d Wickenburg items across %d meeting(s)" % (total_items, meeting_count))
        return 0

    # ── Avondale sync (via CivicClerk — current) ──
    if args.source == "avondale" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, replace_meeting_data_safe
        from scraper.platforms.civicclerk import CivicClerkConfig, search_meetings, fetch_and_parse_agenda

        _avondale_t0 = time.time()
        print(f"  [dbg] Avondale sync starting...")

        avondale_config = CivicClerkConfig(
            subdomain="avondaleaz",
            body_map={
                "City Council": ("avondale-cc", "avondale-cc", "City Council"),
                "City Council Subcommittee": ("avondale-cc", "avondale-cc", "City Council Subcommittee"),
                "Planning Commission": ("avondale-pz", "avondale-pz", "Planning Commission"),
                "Board of Adjustment": ("avondale-boa", "avondale-boa", "Board of Adjustment"),
                "Judicial Advisory Board": ("avondale-judicial", "avondale-judicial", "Judicial Advisory Board"),
                "Art Committee": ("avondale-arts", "avondale-arts", "Art Committee"),
                "Sustainability Commission": ("avondale-sustainability", "avondale-sustainability", "Sustainability Commission"),
                "Public Safety Personnel Retirement System": ("avondale-psprs", "avondale-psprs", "PSPRS"),
                "Alamar (Lakin) CFD Board": ("avondale-cfd", "avondale-cfd", "CFD Board"),
                "Audit Committee": ("avondale-audit", "avondale-audit", "Audit Committee"),
                "Parks, Recreation & Libraries Advisory Board": ("avondale-parks", "avondale-parks", "Parks, Recreation & Libraries"),
                "Neighborhood & Family Services Commission": ("avondale-neighborhood", "avondale-neighborhood", "Neighborhood & Family Services"),
                "Employee Benefit Trust Board": ("avondale-benefits", "avondale-benefits", "Employee Benefit Trust"),
                "Risk Management Trust Fund Board": ("avondale-risk", "avondale-risk", "Risk Management Trust"),
                "Possible Quorum": ("avondale-cc", "avondale-cc", "Possible Quorum"),
            },
            default_body="avondale-cc",
        )

        init_db()

        body_slugs_str = getattr(args, "bodies", None) or "all"
        if body_slugs_str == "all":
            body_slugs = None
        else:
            body_slugs = [s.strip() for s in body_slugs_str.split(",") if s.strip()]

        year_val = getattr(args, "year", None)
        year = int(year_val) if year_val else _dt.date.today().year
        start_date = getattr(args, "start_date", None) or f"{year-1}-01-01"
        end_date = getattr(args, "end_date", None) or f"{year}-12-31"

        print("Searching Avondale CivicClerk meetings from %s to %s..." % (start_date, end_date))
        _t_search = time.time()
        meetings = search_meetings(avondale_config, start_date=start_date)
        print(f"  [dbg]   search_meetings took {time.time() - _t_search:.1f}s")
        if body_slugs:
            meetings = [m for m in meetings if m["body_code"] in body_slugs]
        if not meetings:
            print("No Avondale CivicClerk meetings found.")
            return 0
        print("Found %d Avondale CivicClerk meeting(s)" % len(meetings))

        # Filter by requested end_date, then unreasonable future dates
        today = _dt.date.today()
        end_filter = _dt.date.fromisoformat(end_date) if getattr(args, "end_date", None) else today + _dt.timedelta(days=60)
        meetings = [m for m in meetings if _dt.date.fromisoformat(m["meeting_date"]) <= end_filter]
        if args.limit:
            meetings = meetings[:args.limit]

        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        from db import Meeting as MeetingModel
        from sqlalchemy import select

        _t_loop_start = time.time()
        for idx, m in enumerate(meetings, 1):
            _t_meeting = time.time()
            meeting_id = str(m.get("event_id", m["meeting_id"]))
            meeting_date = m["meeting_date"]
            body_code = m.get("body_code", "avondale-cc")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("meeting_title", "")
            source_url = m.get("source_url", "")
            agenda_url = m.get("agenda_url", "")

            meeting_dict = {"meeting_id": meeting_id, "meeting_date": meeting_date, "meeting_type": meeting_type, "meeting_title": meeting_title, "source_url": source_url}

            existing = session.execute(select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced (%d items) [%.1fs]" % (idx, meeting_count, meeting_id, meeting_date, existing.item_count_actual or 0, time.time() - _t_meeting))
                total_items += existing.item_count_actual or 0
                continue

            try:
                items = []
                if agenda_url:
                    items = fetch_and_parse_agenda(agenda_url, meeting_id, body_code)
                    for item in items:
                        an = item.get("agenda_item_number", "") or ""
                        item["agenda_item_id"] = f"{body_code}-{meeting_id}_{an}"
                        item["source_body"] = body_code
                        item["source_url"] = agenda_url

                # ── Build supporting docs from CivicClerk published files ──
                supp_docs = []
                for sf in m.get("supporting_files", []):
                    supp_docs.append({
                        "agenda_item_id": "0",
                        "agenda_item_number": "",
                        "document_title": sf["file_name"],
                        "document_url": sf.get("api_url") or sf["url"],
                        "document_type": sf["type"],
                        "body": body_code,
                        "meeting_id": meeting_id,
                    })

                replace_meeting_data_safe(
                    session, body_code, meeting_id, meeting_dict,
                    items, supporting_doc_dicts=supp_docs,
                )
                total_items += len(items)
                doc_summary = f" ({len(supp_docs)} doc(s))" if supp_docs else ""
                print("  [%d/%d] %s %s: %d items synced%s [%.1fs]" % (idx, meeting_count, meeting_id, meeting_date, len(items), doc_summary, time.time() - _t_meeting))
            except Exception as e:
                log.error("Failed Avondale meeting %s: %s", meeting_id, e)

        session.close()
        _total_elapsed = time.time() - _avondale_t0
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        print("%s Synced %d Avondale CivicClerk items across %d meeting(s) [%ds total]" % (ts, total_items, meeting_count, _total_elapsed))
        print(f"  [dbg] Avondale done in {_total_elapsed:.0f}s (loop phase: {time.time() - _t_loop_start:.1f}s)")
        return 0

    # ── Avondale Granicus sync (legacy, no items) ──
    if args.source == "avondale-granicus" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from scraper.jurisdictions.avondale import search_avondale_meetings, PUBLIC_BODY_CODE
        init_db()
        _month_val = getattr(args, "month", None)
        _year_val = getattr(args, "year", None)
        if _month_val:
            year = int(_month_val.split("-")[0])
        elif _year_val:
            year = int(_year_val)
        else:
            year = _dt.date.today().year
        print("Searching Avondale (Granicus legacy) meetings for %d..." % year)
        meetings = search_avondale_meetings(year)
        if args.limit:
            meetings = meetings[:args.limit]
        if not meetings:
            print("No Avondale (Granicus legacy) meetings found for %d." % year)
            return 0
        # Post-filter by month if --month was specified
        if _month_val:
            _before = len(meetings)
            meetings = [m for m in meetings if m.get("meeting_date", "").startswith(_month_val)]
            print("Filtered to %d meeting(s) in %s" % (len(meetings), _month_val))
            if not meetings:
                return 0
        print("Found %d Avondale (Granicus legacy) meeting(s)" % len(meetings))
        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        from db import Meeting as MeetingModel
        from sqlalchemy import select
        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m["meeting_date"]
            body_code = m.get("body_code", "avondale-cc")
            source_url = m.get("source_url", "")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("meeting_title", "")
            meeting_dict = {"meeting_id": meeting_id, "meeting_date": meeting_date, "meeting_type": meeting_type, "meeting_title": meeting_title, "source_url": source_url}
            existing = session.execute(select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced" % (idx, meeting_count, meeting_id, meeting_date))
                continue
            try:
                replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                total_items += 0
                print("  [%d/%d] %s %s: meeting synced" % (idx, meeting_count, meeting_id, meeting_date))
            except Exception as e:
                log.error("Failed Avondale (Granicus legacy) meeting %s: %s", meeting_id, e)
        session.close()
        print("Synced %d Avondale (Granicus legacy) meetings (no items)" % meeting_count)
        return 0

    if args.source == "buckeye" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from scraper.jurisdictions.buckeye_granicus import (
            search_buckeye_meetings, fetch_and_parse_agenda,
            extract_supporting_docs,
            BASE_URL, SOURCE_INSTANCE_URL, SOURCE_SYSTEM,
        )
        init_db()
        # Determine search year: --year takes priority, then --month, then current year
        _month_val = getattr(args, "month", None)
        _year_val = getattr(args, "year", None)
        if _month_val:
            year = int(_month_val.split("-")[0])
        elif _year_val:
            year = int(_year_val)
        else:
            year = _dt.date.today().year
        print("Searching Buckeye (Granicus) meetings for %d..." % year)
        meetings = search_buckeye_meetings(year=year, use_html=True)
        if args.limit:
            meetings = meetings[:args.limit]
        if not meetings:
            print("No Buckeye (Granicus) meetings found for %d." % year)
            return 0
        # Further filter by month if --month was specified
        if _month_val:
            _prefix = _month_val  # already YYYY-MM format
            _before = len(meetings)
            meetings = [m for m in meetings if m.get("meeting_date", "").startswith(_prefix)]
            print("Filtered to %d meeting(s) in %s" % (len(meetings), _month_val))
            if not meetings:
                return 0
        print("Found %d Buckeye (Granicus) meeting(s)" % len(meetings))
        if args.meeting_id:
            target_id = str(args.meeting_id)
            target_body = (getattr(args, "body", None) or "").strip().lower()
            meetings = [
                m for m in meetings
                if str(m.get("event_id", m.get("meeting_id", ""))) == target_id
                and (not target_body or m.get("body_code", "").lower() == target_body)
            ]
            if not meetings:
                msg = "Meeting %s not found" % target_id
                if target_body:
                    msg += " for body '%s'" % target_body
                print(msg)
                return 0
            print("Filtered to 1 meeting: %s (body: %s)" % (target_id, meetings[0].get("body_code", "?")))
        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        from db import Meeting as MeetingModel
        from sqlalchemy import select
        for idx, m in enumerate(meetings, 1):
            meeting_id = str(m.get("event_id", m["meeting_id"]))
            meeting_date = m["meeting_date"]
            body_code = m.get("body_code", "buckeye-cc")
            source_url = m.get("agenda_url", "") or m.get("source_url", "")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("meeting_title", "")
            meeting_dict = {"meeting_id": meeting_id, "meeting_date": meeting_date, "meeting_type": meeting_type, "meeting_title": meeting_title, "source_url": source_url, "source_system": SOURCE_SYSTEM}
            existing = session.execute(select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced, %d items" % (idx, meeting_count, meeting_id, meeting_date, existing.item_count_actual or 0))
                if existing.item_count_actual:
                    total_items += existing.item_count_actual
                continue
            try:
                items = []
                supporting_docs = []
                if m.get("packet_url"):
                    try:
                        from scraper.jurisdictions.buckeye_granicus import fetch_and_parse_agenda
                        raw_items = fetch_and_parse_agenda(m)
                        item_counter = 0
                        seen_in_packet: set[str] = set()
                        items = []
                        for it in raw_items:
                            an = (it.get("agenda_item_number", "") or "").strip()
                            if not an:
                                item_counter += 1
                                an = f"auto-{item_counter}"
                            item_id = body_code + "-" + meeting_id + "_" + an
                            if item_id in seen_in_packet:
                                continue
                            seen_in_packet.add(item_id)
                            it["agenda_item_id"] = item_id
                            it["meeting_id"] = meeting_id
                            it["meeting_date"] = meeting_date
                            it["meeting_type"] = meeting_type
                            it["source_body"] = body_code
                            it["source_url"] = source_url
                            items.append(it)
                        if items:
                            print(f"  Parsed {len(items)} agenda items from packet")
                    except Exception as pe:
                        log.debug("Packet parsing failed for %s: %s", meeting_id, pe)

                # Extract supporting documents from the agenda PDF
                if source_url:
                    try:
                        supporting_docs = extract_supporting_docs(source_url)
                        if supporting_docs:
                            print(f"  Extracted {len(supporting_docs)} supporting documents from agenda")
                    except Exception as de:
                        log.debug("Supporting doc extraction failed for %s: %s", meeting_id, de)

                replace_meeting_data_safe(
                    session, body_code, meeting_id, meeting_dict, items,
                    supporting_doc_dicts=supporting_docs,
                )
                total_items += len(items)
                print("  [%d/%d] %s %s: %d items synced" % (idx, meeting_count, meeting_id, meeting_date, len(items)))
            except Exception as e:
                log.error("Failed Buckeye (Granicus) meeting %s: %s", meeting_id, e)
        session.close()
        print("Synced %d Buckeye (Granicus) meetings" % meeting_count)
        return 0

    if args.source == "buckeye-novusagenda" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, replace_meeting_data_safe
        from scraper.jurisdictions.buckeye import search_buckeye_meetings, fetch_agenda_items_async, SLUG_TO_CODE, BODY_CODE_MAP, DEFAULT_BODY_SLUGS
        init_db()
        body_slugs_str = getattr(args, "bodies", None) or ",".join(DEFAULT_BODY_SLUGS)
        body_slugs = [s.strip() for s in body_slugs_str.split(",") if s.strip()]
        year_val = getattr(args, "year", None)
        year = int(year_val) if year_val else _dt.date.today().year
        print("Searching Buckeye meetings for %d..." % year)
        meetings = search_buckeye_meetings(date_range="lyr", body_slugs=body_slugs)
        if not meetings:
            print("No Buckeye meetings found for %d." % year)
            return 0
        print("Found %d Buckeye meeting(s)" % len(meetings))
        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        for idx, m in enumerate(meetings, 1):
            meeting_id = str(m["meeting_id"])
            meeting_date = m["meeting_date"]
            body_code = SLUG_TO_CODE.get(m.get("body_slug", ""), "buckeye-cc")
            meeting_url = m.get("meeting_view_url", "") or m.get("meeting_url", "") or m.get("agenda_url", "")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("meeting_title", m.get("body_name", ""))
            meeting_dict = {"meeting_id": meeting_id, "meeting_date": meeting_date, "meeting_type": meeting_type, "meeting_title": meeting_title, "source_url": meeting_url}
            from db import Meeting as MeetingModel
            from sqlalchemy import select
            existing = session.execute(select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == meeting_id)).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced" % (idx, meeting_count, meeting_id, meeting_date))
                continue
            try:
                items = fetch_agenda_items_async(meeting_url, meeting_id)
                if not items:
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                    print("  [%d/%d] %s %s: no items" % (idx, meeting_count, meeting_id, meeting_date))
                    continue
                agenda_item_dicts = []
                for it in items:
                    an = it.get("agenda_item_number", "")
                    agenda_item_dicts.append({"agenda_item_id": body_code + "-" + meeting_id + "_" + an, "meeting_id": meeting_id, "agenda_item_number": an, "agenda_item_title": it.get("agenda_item_title", ""), "agenda_item_text": it.get("agenda_item_text", ""), "source_body": body_code, "source_url": meeting_url, "sort_order": it.get("sort_order", 0)})
                replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, agenda_item_dicts)
                total_items += len(items)
                print("  [%d/%d] %s %s: %d item(s)" % (idx, meeting_count, meeting_id, meeting_date, len(items)))
            except Exception as e:
                log.error("Failed Buckeye meeting %s: %s", meeting_id, e)
        session.close()
        print("Synced %d Buckeye items across %d meeting(s)" % (total_items, meeting_count))
        return 0


    # ── Surprise sync (via CivicClerk) ──
    if args.source == "surprise" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, replace_meeting_data_safe
        from scraper.jurisdictions.surprise import search_surprise_meetings, DEFAULT_BODY_SLUGS, SOURCE_SYSTEM, SOURCE_INSTANCE_URL
        init_db()
        body_slugs_str = getattr(args, "bodies", None) or ",".join(DEFAULT_BODY_SLUGS)
        body_slugs = [s.strip() for s in body_slugs_str.split(",") if s.strip()]
        start = getattr(args, "start_date", None)
        end = getattr(args, "end_date", None)
        if not start:
            year_val = getattr(args, "year", None)
            year = int(year_val) if year_val else _dt.date.today().year
            start = _dt.date(year, 1, 1).isoformat()
            end = _dt.date(year, 12, 31).isoformat()
        print("Searching Surprise meetings from %s to %s..." % (start, end))
        meetings = search_surprise_meetings(start, end, body_slugs=body_slugs)
        if not meetings:
            print("No Surprise meetings found.")
            return 0
        print("Found %d Surprise meeting(s)" % len(meetings))
        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m.get("meeting_date", "")
            body_code = m.get("body_code", m.get("body_slug", "surprise-cc"))
            meeting_url = m.get("agenda_url", "") or ""
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("meeting_title", m.get("body_name", ""))
            meeting_dict = {"meeting_id": str(meeting_id), "meeting_date": meeting_date, "meeting_type": meeting_type, "meeting_title": meeting_title, "source_url": meeting_url}
            from db import Meeting as MeetingModel
            from sqlalchemy import select
            existing = session.execute(select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == str(meeting_id))).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced" % (idx, meeting_count, meeting_id, meeting_date))
                continue
            try:
                items = m.get("agenda_items", [])
                if not items:
                    # Try PDF fallback — agenda items may only be in the packet PDF
                    import re as _re
                    import fitz as _fitz
                    packet_url = m.get("agenda_packet_url") or m.get("agenda_url", "")
                    file_id_m = _re.search(r'fileId=(\d+)', packet_url, _re.I)
                    if file_id_m:
                        try:
                            from scraper.jurisdictions.surprise import download_meeting_file, parse_agenda_items_from_pdf_text
                            pdf_bytes = download_meeting_file(int(file_id_m.group(1)))
                            if pdf_bytes:
                                pdf_doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
                                pdf_text = "".join(page.get_text() for page in pdf_doc)
                                pdf_doc.close()
                                if pdf_text and len(pdf_text) > 50:
                                    pdf_items = parse_agenda_items_from_pdf_text(pdf_text)
                                    if pdf_items:
                                        items = pdf_items
                                        print(f"  Parsed {len(items)} items from agenda packet")
                        except Exception as pe:
                            log.debug("PDF fallback failed for %s: %s", meeting_id, pe)
                    
                    if not items:
                        replace_meeting_data_safe(session, body_code, str(meeting_id), meeting_dict, [])
                        print("  [%d/%d] %s %s: no items" % (idx, meeting_count, meeting_id, meeting_date))
                        continue
                agenda_item_counter = 0
                agenda_item_dicts = []
                for it in items:
                    an = str(it.get("agenda_item_number", "") or "")
                    if not an:
                        agenda_item_counter += 1
                        an = f"sec-{agenda_item_counter}"
                    agenda_item_dicts.append({"agenda_item_id": body_code + "-" + str(meeting_id) + "_" + an, "meeting_id": str(meeting_id), "agenda_item_number": an, "agenda_item_title": it.get("agenda_item_title", ""), "agenda_item_text": it.get("agenda_item_text", ""), "source_body": body_code, "source_url": meeting_url, "sort_order": it.get("sort_order", 0)})
                replace_meeting_data_safe(session, body_code, str(meeting_id), meeting_dict, agenda_item_dicts)
                total_items += len(items)
                print("  [%d/%d] %s %s: %d item(s)" % (idx, meeting_count, meeting_id, meeting_date, len(items)))
            except Exception as e:
                log.error("Failed Surprise meeting %s: %s", meeting_id, e)
        session.close()
        print("Synced %d Surprise items across %d meeting(s)" % (total_items, meeting_count))
        return 0

    if args.source == "surprise-civicclerk" and args.sync:
        _surprise_t0 = time.time()  # start of this Surprise branch, for total elapsed
        import datetime as _dt
        from db import get_session, init_db, replace_meeting_data_safe
        from scraper.platforms.civicclerk import CivicClerkConfig, search_meetings, fetch_meeting_items

        surprise_config = CivicClerkConfig(
            subdomain="surpriseaz",
            body_map={
                "Planning and Zoning Commission": ("surprise-pz", "surprise-pz", "Planning & Zoning Commission"),
                "Regular City Council Meeting": ("surprise-cc", "surprise-cc", "City Council"),
                "Regular City Council Work Session": ("surprise-cc", "surprise-cc", "City Council Work Session"),
                "Special City Council Meeting": ("surprise-cc", "surprise-cc", "Special City Council Meeting"),
                "Arts and Cultural Advisory Commission": ("surprise-arts", "surprise-arts", "Arts & Cultural Advisory Commission"),
                "Arts & Cultural Advisory Commission": ("surprise-arts", "surprise-arts", "Arts & Cultural Advisory Commission"),
                "Veteran, Disability and Human Service Commission": ("surprise-veterans", "surprise-veterans", "Veterans, Disability & Human Services"),
                "Library Commission": ("surprise-library", "surprise-library", "Library Commission"),
                "Library Advisory Commission": ("surprise-library", "surprise-library", "Library Advisory Commission"),
                "Parks and Recreation Commission": ("surprise-parks", "surprise-parks", "Parks & Recreation Commission"),
                "Public Safety Personnel Retirement System Commission \u2013 Fire": ("surprise-psprs-fire", "surprise-psprs-fire", "PSPRS Fire"),
                "Public Safety Personnel Retirement System Commission \u2013 Police": ("surprise-psprs-police", "surprise-psprs-police", "PSPRS Police"),
                "Health Benefits Trust Fund Board": ("surprise-health-benefits", "surprise-health-benefits", "Health Benefits Trust Fund Board"),
                "Boards and Commissions Nominations Committee": ("surprise-nominations", "surprise-nominations", "Boards & Commissions Nominations"),
                "City Audit Committee": ("surprise-audit", "surprise-audit", "City Audit Committee"),
                "Tourism Fund Subcommittee": ("surprise-tourism", "surprise-tourism", "Tourism Fund Subcommittee"),
                "Judicial Selection Advisory Commission": ("surprise-judicial-selection", "surprise-judicial-selection", "Judicial Selection Advisory Commission"),
            },
            default_body="surprise-pz",
        )

        init_db()
        body_slugs_str = getattr(args, "bodies", None) or "surprise-pz"
        body_slugs = None if body_slugs_str == "all" else [s.strip() for s in body_slugs_str.split(",") if s.strip()]
        year_val = getattr(args, "year", None)
        year = int(year_val) if year_val else _dt.date.today().year
        start_date = getattr(args, "start_date", None) or f"{year}-01-01"
        print("Searching Surprise CivicClerk meetings from %s..." % start_date)
        _t_search = time.time()
        meetings = search_meetings(surprise_config, start_date=start_date)
        print(f"  [dbg]   search_meetings took {time.time() - _t_search:.1f}s")
        if body_slugs:
            meetings = [m for m in meetings if m["body_code"] in body_slugs]
        if not meetings:
            print("No Surprise CivicClerk meetings found.")
            return 0
        print("Found %d Surprise CivicClerk meeting(s)" % len(meetings))
        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        from db import Meeting as MeetingModel
        from sqlalchemy import select
        _t_loop_start = time.time()
        for idx, m in enumerate(meetings, 1):
            _t_mtg = time.time()
            event_id = m.get("event_id")
            if not event_id:
                event_id = int(m.get("meeting_id", 0))
            meeting_date = m["meeting_date"]
            body_code = m.get("body_code", "surprise-pz")
            meeting_type = m.get("meeting_type", "")
            meeting_title = m.get("meeting_title", "")
            source_url = m.get("source_url", "")
            meeting_dict = {"meeting_id": str(event_id), "meeting_date": meeting_date, "meeting_type": meeting_type, "meeting_title": meeting_title, "source_url": source_url}
            existing = session.execute(select(MeetingModel).where(MeetingModel.body == body_code, MeetingModel.meeting_id == str(event_id))).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print("  [%d/%d] %s %s: already synced (%d items) [%.1fs]" % (idx, meeting_count, event_id, meeting_date, existing.item_count_actual or 0, time.time() - _t_mtg))
                total_items += existing.item_count_actual or 0
                continue
            try:
                # ── Extract structured items + docs from Meetings API ──
                items, supp_docs = [], []
                if event_id:
                    import urllib.request, json
                    _t_evt = time.time()
                    evt_url = f"{surprise_config.api_base}/Events/{event_id}"
                    evt_req = urllib.request.Request(evt_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
                    try:
                        with urllib.request.urlopen(evt_req, timeout=10) as evt_resp:
                            evt_data = json.loads(evt_resp.read())
                        print(f"  [dbg]     Events/{event_id} API call: {time.time() - _t_evt:.1f}s, agendaId={evt_data.get('agendaId', 0)}")
                        agenda_id = evt_data.get("agendaId", 0)
                        if agenda_id and agenda_id > 0:
                            items, supp_docs = fetch_meeting_items(
                                surprise_config, event_id, agenda_id,
                                body_code, meeting_date,
                            )
                    except Exception as e:
                        print(f"  [dbg]     Events/{event_id} FAILED ({time.time() - _t_evt:.1f}s): {e}")

                replace_meeting_data_safe(
                    session, body_code, str(event_id), meeting_dict,
                    items, supporting_doc_dicts=supp_docs,
                )
                total_items += len(items)
                doc_summary = f" ({len(supp_docs)} doc(s))" if supp_docs else ""
                print("  [%d/%d] %s %s: %d items synced%s [%.1fs]" % (idx, meeting_count, event_id, meeting_date, len(items), doc_summary, time.time() - _t_mtg))
            except Exception as e:
                import traceback
                print("Failed Surprise CivicClerk meeting %s: %s" % (event_id, e))
        session.close()
        _total_elapsed = time.time() - _surprise_t0
        print("Synced %d Surprise CivicClerk items across %d meeting(s) [%ds total]" % (total_items, meeting_count, _total_elapsed))
        print(f"  [dbg] Surprise CivicClerk done in {_total_elapsed:.0f}s (loop phase: {time.time() - _t_loop_start:.1f}s)")
        return 0

    # ── Tucson sync (via OnBase) ──
    if args.source == "tucson" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from db import Meeting as MeetingModel
        from sqlalchemy import select

        init_db()

        year_val = getattr(args, "year", None)
        year = int(year_val) if year_val else _dt.date.today().year
        if args.start_date:
            d = _dt.date.fromisoformat(args.start_date)
            start_mmddyy = f"{d.month:02d}/{d.day:02d}/{d.year}"
        else:
            start_mmddyy = f"01/01/{year}"
        if args.end_date:
            d = _dt.date.fromisoformat(args.end_date)
            end_mmddyy = f"{d.month:02d}/{d.day:02d}/{d.year}"
        else:
            end_mmddyy = _dt.date.today().strftime("%m/%d/%Y")

        from scraper.jurisdictions.tucson import search_tucson_meetings, extract_tucson_agenda_items

        print(f"Searching Tucson meetings: {start_mmddyy} to {end_mmddyy}")
        meetings = await search_tucson_meetings(None, start_mmddyy, end_mmddyy)
        if not meetings:
            print("No Tucson meetings found.")
            return 0
        if args.limit:
            meetings = meetings[:args.limit]
        print("Found %d Tucson meeting(s)" % len(meetings))

        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m["meeting_date"]
            body_code = m.get("body", "tucson-cc")
            meeting_title = m.get("meeting_title", "")
            meeting_type = m.get("meeting_type", "")
            source_url = m.get("source_url", "")

            meeting_dict = {
                "meeting_id": meeting_id, "meeting_date": meeting_date,
                "meeting_type": meeting_type, "meeting_title": meeting_title,
                "source_url": source_url,
            }

            existing = session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == body_code,
                    MeetingModel.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: already synced")
                total_items += existing.item_count_actual or 0
                continue

            update_sync_status(session, body_code, meeting_id, "in_progress")
            session.commit()

            try:
                agenda_url = m.get("agenda_url", "")
                if agenda_url:
                    items = await extract_tucson_agenda_items(None, agenda_url)
                else:
                    items = []

                if not items:
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                    print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: no items")
                    update_sync_status(session, body_code, meeting_id, "no_agenda")
                    session.commit()
                    continue

                agenda_dicts = []
                for idx2, it in enumerate(items):
                    an = it.get("agenda_item_number", "")
                    # Unique ID: use sequential position (idx2) as the suffix.
                    # The item number alone isn't unique because items at different
                    # nesting levels (or under different parent sections) can share
                    # numbers like "1".
                    level = it.get("section_level", 0)
                    aid = f"{body_code}-{meeting_id}_l{level}_i{idx2 + 1}"
                    agenda_dicts.append({
                        "agenda_item_id": aid,
                        "meeting_id": meeting_id, "agenda_item_number": an,
                        "agenda_item_title": it.get("agenda_item_title", ""),
                        "agenda_item_text": it.get("agenda_item_text", ""),
                        "agenda_item_url": it.get("agenda_item_url", ""),
                        "vote_or_action": it.get("vote_or_action", ""),
                        "item_type": it.get("item_type", "section"),
                        "section_level": it.get("section_level", 0),
                        "source_body": body_code, "source_url": source_url,
                        "c_number": "", "c_number_base": "", "case_number": "",
                    })

                replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, agenda_dicts)
                total_items += len(items)
                ts = _dt.datetime.now().strftime("%H:%M:%S")
                print(f"{ts} [{idx}/{meeting_count}] {meeting_id} {meeting_date}: {len(items)} item(s)")
                update_sync_status(session, body_code, meeting_id, "complete")
                session.commit()
            except Exception as e:
                log.error("Failed to sync Tucson meeting %s: %s", meeting_id, e)
                try:
                    update_sync_status(session, body_code, meeting_id, "failed", error=str(e)[:500])
                    session.commit()
                except Exception:
                    pass

        session.close()
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        print(f"{ts} Synced {total_items} Tucson agenda items across {meeting_count} meeting(s)")
        return 0

    # ── Tucson PC sync (PDF-based) ──
    if args.source == "tucson-pc" and args.sync:
        import datetime as _dt
        from db import get_session, init_db, update_sync_status, replace_meeting_data_safe
        from db import Meeting as MeetingModel
        from sqlalchemy import select

        init_db()

        year_val = getattr(args, "year", None)
        year = int(year_val) if year_val else _dt.date.today().year
        start_date = getattr(args, "start_date", None) or f"{year}-01-01"
        end_date = getattr(args, "end_date", None) or _dt.date.today().isoformat()

        from scraper.jurisdictions.tucson import search_tucson_pc_meetings, extract_tucson_pc_agenda_items

        print(f"Searching Tucson PC meetings: {start_date} to {end_date}")
        meetings = search_tucson_pc_meetings(start_date, end_date)
        if not meetings:
            print("No Tucson PC meetings found.")
            return 0
        if args.limit:
            meetings = meetings[:args.limit]
        print("Found %d Tucson PC meeting(s)" % len(meetings))

        session = get_session()
        total_items = 0
        meeting_count = len(meetings)
        for idx, m in enumerate(meetings, 1):
            meeting_id = m["meeting_id"]
            meeting_date = m.get("meeting_date", "")
            body_code = "tucson-pc"
            meeting_title = m.get("meeting_title", "Tucson Planning Commission")
            meeting_type = m.get("meeting_type", "Regular Meeting")
            source_url = m.get("source_url", "")

            meeting_dict = {
                "meeting_id": meeting_id, "meeting_date": meeting_date,
                "meeting_type": meeting_type, "meeting_title": meeting_title,
                "source_url": source_url,
            }

            existing = session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == body_code,
                    MeetingModel.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()
            if existing and existing.sync_status == "complete" and (existing.item_count_actual or 0) > 0 and not args.force:
                print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: already synced")
                total_items += existing.item_count_actual or 0
                continue

            update_sync_status(session, body_code, meeting_id, "in_progress")
            session.commit()

            try:
                pdf_url = m.get("agenda_pdf_url", "")
                if pdf_url:
                    items = extract_tucson_pc_agenda_items(pdf_url)
                else:
                    items = []

                if not items:
                    replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, [])
                    print(f"  [{idx}/{meeting_count}] {meeting_id} {meeting_date}: no items")
                    update_sync_status(session, body_code, meeting_id, "no_agenda")
                    session.commit()
                    continue

                agenda_dicts = []
                for it in items:
                    an = it.get("agenda_item_number", "")
                    agenda_dicts.append({
                        "agenda_item_id": body_code + "-" + meeting_id + "_" + an,
                        "meeting_id": meeting_id, "agenda_item_number": an,
                        "agenda_item_title": it.get("agenda_item_title", ""),
                        "agenda_item_text": it.get("agenda_item_text", ""),
                        "source_body": body_code, "source_url": source_url,
                        "c_number": "", "c_number_base": "", "case_number": "",
                    })

                replace_meeting_data_safe(session, body_code, meeting_id, meeting_dict, agenda_dicts)
                total_items += len(items)
                ts = _dt.datetime.now().strftime("%H:%M:%S")
                print(f"{ts} [{idx}/{meeting_count}] {meeting_id} {meeting_date}: {len(items)} item(s)")
                update_sync_status(session, body_code, meeting_id, "complete")
                session.commit()
            except Exception as e:
                log.error("Failed to sync Tucson PC meeting %s: %s", meeting_id, e)
                try:
                    update_sync_status(session, body_code, meeting_id, "failed", error=str(e)[:500])
                    session.commit()
                except Exception:
                    pass

        session.close()
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        print(f"{ts} Synced {total_items} Tucson PC agenda items across {meeting_count} meeting(s)")
        return 0

    # Standalone adapters whose ownership has migrated to the source registry.
    if args.sync:
        registered_adapter = adapter_for_command(args.source)
        if registered_adapter is not None:
            return registered_adapter(args)

    if args.source in ("pz", "adj", "drain", "health", "tab", "ida") and args.sync:
        from db import get_session, init_db, replace_meeting_data_safe

        init_db()

        # Source-specific dispatch
        if args.source == "pz":
            CID = "9,"
            source_body = "pz"
            source_type = "Planning & Zoning"
            source_label = "P&Z"
            from scraper.county.pz import build_pz_search_url as build_search_url_fn
            from scraper.county.pz import extract_pz_meetings as extract_meetings_fn
            from scraper.county.pz import extract_pz_agenda_items as extract_items_fn
            from scraper.county.pz import _format_mm_dd_yyyy as fmt_date_fn
            from scraper.county.pz import _normalize_pz_meeting_title as normalize_title_fn
        elif args.source == "adj":
            CID = "3,"
            source_body = "adj"
            source_type = "Board of Adjustment"
            source_label = "ADJ"
            from scraper.county.adj import build_adj_search_url as build_search_url_fn
            from scraper.county.adj import extract_adj_meetings as extract_meetings_fn
            from scraper.county.adj import extract_adj_agenda_items as extract_items_fn
            from scraper.county.adj import _format_mm_dd_yyyy as fmt_date_fn
            from scraper.county.adj import _normalize_adj_meeting_title as normalize_title_fn
        elif args.source == "drain":
            CID = "19,"
            source_body = "drain"
            source_type = "Drainage Review Board"
            source_label = "DRB"
            from scraper.county.drain import build_drain_search_url as build_search_url_fn
            from scraper.county.drain import extract_drain_meetings as extract_meetings_fn
            from scraper.county.drain import extract_drain_agenda_items as extract_items_fn
            from scraper.county.drain import _format_mm_dd_yyyy as fmt_date_fn
        elif args.source == "health":
            CID = "13,"
            source_body = "health"
            source_type = "Board of Health"
            source_label = "BOH"
            from scraper.county.health import build_health_search_url as build_search_url_fn
            from scraper.county.health import extract_health_meetings as extract_meetings_fn
            from scraper.county.health import extract_health_agenda_items as extract_items_fn
            from scraper.county.health import _format_mm_dd_yyyy as fmt_date_fn
        elif args.source == "tab":
            CID = "11,"
            source_body = "tab"
            source_type = "Transportation Advisory Board"
            source_label = "TAB"
            from scraper.county.tab import build_tab_search_url as build_search_url_fn
            from scraper.county.tab import extract_tab_meetings as extract_meetings_fn
            from scraper.county.tab import extract_tab_agenda_items as extract_items_fn
            from scraper.county.tab import _format_mm_dd_yyyy as fmt_date_fn
        elif args.source == "ida":
            CID = ""
            source_body = "ida"
            source_type = "Industrial Development Authority"
            source_label = "IDA"
            from scraper.county.ida import extract_ida_meetings as extract_meetings_fn
            from scraper.county.ida import extract_ida_agenda_items as extract_items_fn

            # IDA is a static page — no search URL or date formatting needed
            def fmt_date_fn(x): return x
            def build_search_url_fn(start, end): return "https://mcida.com/about-us/public-meetings/"
        elif args.source == "tempe":
            source_body = "tempe-cc"
            source_type = "City Council"
            source_label = "Tempe"
            from scraper.jurisdictions.tempe import (
                search_tempe_meetings as extract_meetings_fn,
                extract_tempe_agenda_items as extract_items_fn,
                normalize_tempe_meeting_title as normalize_title_fn,
                extract_meeting_type_from_title,
                PUBLIC_BODY_CODE,
            )
            def fmt_date_fn(x): return x
            from scraper.platforms.onbase import TEMPE_CONFIG
            def build_search_url_fn(start, end): return TEMPE_CONFIG.build_search_url(start, end)

        # If --meeting-id is provided, bypass search and use direct meeting URL
        if args.meeting_id:
            meeting_id = args.meeting_id
            # Try to get the agenda URL from the database first
            from db import Meeting as MeetingModel
            from sqlalchemy import select
            db_session = get_session()
            existing = db_session.execute(
                select(MeetingModel).where(
                    MeetingModel.body == source_body,
                    MeetingModel.meeting_id == meeting_id,
                )
            ).scalar_one_or_none()
            agenda_url = ""
            meeting_date = ""
            meeting_title = ""
            if existing and existing.source_url:
                # Use the HTML agenda URL from previous sync
                agenda_url = existing.source_url
                meeting_date = existing.meeting_date or ""
                meeting_title = existing.meeting_title or ""
                print(f"Found existing {source_label} meeting {meeting_id} in database")
            else:
                # Construct a best-guess URL. Agenda Center HTML pages use
                # ViewFile/Agenda/<id>?html=true
                if source_body in ("drain", "health", "tab"):
                    agenda_url = f"https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/{meeting_id}?html=true"
                elif source_body == "ida":
                    agenda_url = ""  # IDA uses date-based IDs; agenda_url comes from re-fetching the page
                else:
                    agenda_url = f"https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/{meeting_id}?html=true"
            db_session.close()

            if args.source == "pz":
                clean_title, clean_type = normalize_title_fn(
                    meeting_title, source_type
                )
            elif args.source == "adj":
                clean_title = normalize_title_fn(meeting_title)
                clean_type = source_type
            else:
                clean_title = meeting_title
                clean_type = source_type

            pz_meetings = [Meeting(
                meeting_date=meeting_date,
                meeting_time="",
                meeting_title=clean_title,
                meeting_type=clean_type,
                body=source_body,
                row_text="",
                detail_url="",
                agenda_url=agenda_url,
            )]
            print(f"Syncing {source_label} meeting {meeting_id}...")
        else:
            now = dt.date.today()
            if args.start_date:
                pz_start = fmt_date_fn(args.start_date) or f"{now.month:02d}/01/{now.year}"
            else:
                three_months_ago = now - dt.timedelta(days=90)
                pz_start = f"{three_months_ago.month:02d}/01/{three_months_ago.year}"

            if args.end_date:
                pz_end = fmt_date_fn(args.end_date) or f"{now.month:02d}/{min(28, now.day):02d}/{now.year}"
            else:
                pz_end = f"{now.month:02d}/{min(28, now.day):02d}/{now.year}"

            search_url = build_search_url_fn(pz_start, pz_end)
            print(f"{source_label} search URL: {search_url}")

        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                if args.meeting_id:
                    # Skip search when --meeting-id is provided; list already built above
                    pass
                else:
                    pz_meetings = await extract_meetings_fn(page, search_url)

                    if not pz_meetings:
                        print(f"No {source_label} meetings found.")
                        return 0

                if args.limit:
                    pz_meetings = pz_meetings[:args.limit]

                print(f"Found {len(pz_meetings)} {source_label} meeting(s)")

                session = get_session()
                total = 0
                meeting_count = len(pz_meetings)

                for idx, meeting in enumerate(pz_meetings, 1):
                    meeting_dict = {
                        "meeting_id": meeting.meeting_id,
                        "meeting_date": meeting.meeting_date,
                        "meeting_type": meeting.meeting_type,
                        "meeting_title": meeting.meeting_title,
                        "source_url": meeting.agenda_url,
                    }

                    items = await extract_items_fn(page, meeting.agenda_url)

                    # extract_pz_agenda_items now returns real items from the PDF
                    # with supporting_doc_dicts already set. We just need to fix
                    # meeting_id and agenda_item_id, then flatten supporting docs.
                    docs: list[dict] = []
                    for it in items:
                        it["meeting_id"] = meeting.meeting_id
                        it["agenda_item_id"] = f"{meeting.meeting_id}-{it['agenda_item_number']}-item"
                        for sd in it.pop("supporting_doc_dicts", []):
                            sd["meeting_id"] = meeting.meeting_id
                            sd["agenda_item_number"] = int(it["agenda_item_number"])
                            sd["agenda_item_id"] = int(it["agenda_item_number"])
                            docs.append(sd)

                    if items:
                        replace_meeting_data_safe(
                            session, meeting.body, meeting.meeting_id, meeting_dict, items,
                            supporting_doc_dicts=docs,
                        )

                        # Persist item details (PZ-specific) to database
                        if args.source == "pz":
                            from db.pz_details import persist_pz_item_details
                            try:
                                persist_pz_item_details(
                                    session, meeting.body, meeting.meeting_id, items
                                )
                                session.commit()
                            except Exception as pz_err:
                                print(f"    PZ detail persist skipped: {pz_err}")
                                session.rollback()

                        total += len(items)
                        doc_summary = f", {len(docs)} doc(s)" if docs else ""

                        # ── PZ Minutes / Votes extraction (PZ-specific only) ──
                        vote_summary = ""
                        if args.source == "pz":
                            minutes_url = meeting.agenda_url.replace(
                                "/Agenda/", "/Minutes/"
                            ).replace("?html=true", "")
                            try:
                                import urllib.request
                                from pathlib import Path
                                from scraper.common.pz_minutes import parse_pz_minutes_pdf
                                from db import persist_votes

                                pdf_req = urllib.request.Request(
                                    minutes_url,
                                    headers={"User-Agent":
                                        "Mozilla/5.0 (compatible; MaricopaAgendaBot)"},
                                )
                                with urllib.request.urlopen(pdf_req, timeout=30) as pdf_resp:
                                    pdf_path = Path(f"/tmp/pz_min_{meeting.meeting_id}.pdf")
                                    pdf_path.write_bytes(pdf_resp.read())

                                minutes_data = parse_pz_minutes_pdf(str(pdf_path))
                                pdf_path.unlink(missing_ok=True)

                                if not minutes_data.get("votes"):
                                    vote_summary = ", votes=none"
                                else:
                                    supervisors: list[dict] = [
                                        {"name": name,
                                         "normalized_name": name.lower(),
                                         "present": True}
                                        for name in minutes_data["members_present"]
                                    ]
                                    supervisors.extend([
                                        {"name": name,
                                         "normalized_name": name.lower(),
                                         "present": False}
                                        for name in minutes_data["members_absent"]
                                    ])

                                    votes_list: list[dict] = []
                                    aye_names_lower = set()
                                    nay_names_lower = set()

                                    for v in minutes_data["votes"]:
                                        aye_names_lower = {
                                            n.lower() for n in v.get("ayes", []) if n
                                        }
                                        nay_names_lower = {
                                            n.lower() for n in v.get("nays", []) if n
                                        }

                                        for cn in v.get("c_numbers", []):
                                            matched_num = 0
                                            for it in items:
                                                it_case = (
                                                    it.get("case_number") or
                                                    it.get("c_number") or ""
                                                ).upper()
                                                if it_case == cn.upper():
                                                    matched_num = int(
                                                        it.get("agenda_item_number", 0)
                                                    )
                                                    break

                                            if not matched_num:
                                                continue

                                            supervisor_votes = []
                                            for sup in supervisors:
                                                name_lower = sup["normalized_name"]
                                                name_words = set(name_lower.split())
                                                voted_aye = bool(name_words & aye_names_lower)
                                                voted_nay = bool(name_words & nay_names_lower)
                                                if voted_aye and not voted_nay:
                                                    supervisor_votes.append({
                                                        "name": sup["name"],
                                                        "vote": "aye",
                                                    })
                                                elif voted_nay and not voted_aye:
                                                    supervisor_votes.append({
                                                        "name": sup["name"],
                                                        "vote": "nay",
                                                    })

                                            votes_list.append({
                                                "agenda_item_number": matched_num,
                                                "c_number": cn,
                                                "motion_result":
                                                    v.get("motion_result", "unknown"),
                                                "vote_text": v.get("vote_text", ""),
                                                "conditions": v.get("conditions"),
                                                "supervisor_votes": supervisor_votes,
                                            })

                                    if votes_list:
                                        vote_session = get_session()
                                        try:
                                            vote_count = persist_votes(
                                                vote_session, meeting.body,
                                                meeting.meeting_id,
                                                supervisors, votes_list,
                                            )
                                            vote_summary = f", votes={vote_count}"
                                        finally:
                                            vote_session.close()
                                    else:
                                        vote_summary = ", votes=0(unmatched)"
                            except urllib.error.HTTPError as e:
                                vote_summary = ", votes=na" if e.code == 404 \
                                    else f", votes=error({e.code})"
                            except Exception as ve:
                                vote_summary = f", votes=error"

                        ts = time.strftime("%H:%M:%S")
                        print(f"{ts} [{idx}/{meeting_count}] {meeting.meeting_id} {meeting.meeting_date}: {len(items)} item(s){doc_summary}{vote_summary}")

                session.close()
                ts = time.strftime("%H:%M:%S")
                print(f"{ts} Synced {total} {source_label} agenda items across {len(pz_meetings)} meeting(s)")
            finally:
                await browser.close()
        return 0

    if getattr(args, "status", False):
        from db import get_session, get_sync_status_summary
        session = get_session()
        summary = get_sync_status_summary(session)
        session.close()
        print(f"{'Status':<14}  {'Count':>6}")
        print(f"{'─' * 14}  {'─' * 6}")
        for status in ["complete", "partial", "manual_review", "failed", "pending"]:
            print(f"{status:<14}  {summary.get(status, 0):>6}")
        print(f"{'─' * 14}  {'─' * 6}")
        print(f"{'Total':<14}  {summary['total']:>6}")
        print(f"\nItems: {summary['total_items']}  Supporting docs: {summary['total_docs']}")
        return 0

    if getattr(args, "failed", False):
        from db import get_session, get_failed_meetings, get_meetings_by_status
        session = get_session()
        failed_statuses = ["failed", "partial", "manual_review"] if getattr(args, "include_manual_review", False) else ["failed", "partial"]
        body_filter = args.source if hasattr(args, 'source') else "bos"
        meetings = get_meetings_by_status(session, body_filter, failed_statuses)
        session.close()
        if not meetings:
            print("No meetings with issues.")
            return 0
        print(f"{'ID':>6}  {'Date':<12}  {'Status':<12}  {'Retries':>7}  {'Error'}")
        print(f"{'─' * 6}  {'─' * 12}  {'─' * 12}  {'─' * 7}  {'─' * 40}")
        for m in meetings:
            err = (m.last_error or "")[:60]
            print(f"{m.meeting_id:>6}  {m.meeting_date:<12}  {m.sync_status:<12}  {m.retry_count:>7}  {err}")
        return 0

    if getattr(args, "persist", False):
        from db import get_session, persist_meeting, record_ingest_failure
        import csv

        csv_path = AGENDA_ITEMS_CSV
        if not csv_path.exists():
            print(f"No agenda items CSV found at {csv_path}. Run --extract-agenda-items first.")
            return 1

        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        if not rows:
            print("CSV is empty.")
            return 1

        # Group by meeting_id + meeting_date
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            key = (row.get("meeting_id", ""), row.get("meeting_date", ""))
            groups.setdefault(key, []).append(row)

        session = get_session()
        total = 0
        errors = 0
        for (meeting_id, meeting_date), items in sorted(groups.items()):
            first = items[0]
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": first.get("meeting_type", ""),
                "meeting_title": first.get("agenda_item_section", "") or meeting_id,
                "source_url": first.get("source_url", ""),
            }
            try:
                body = first.get("body") or getattr(args, 'source', None)
                if not body:
                    raise ValueError(
                        f"Cannot determine body for meeting {meeting_id} ({meeting_date}): "
                        f"no 'body' field in CSV data and no --source flag provided"
                    )
                count = persist_meeting(session, body, meeting_id, items)
                total += count
                print(f"  {meeting_id} {meeting_date}: {count} items")
            except Exception as e:
                errors += 1
                print(f"  {meeting_id} {meeting_date}: FAILED - {e}")
                try:
                    record_ingest_failure(
                        session,
                        str(e),
                        source="csv-persist",
                        meeting_id=meeting_id,
                        meeting_date=meeting_date,
                        context=f"Attempted to persist {len(items)} agenda items",
                    )
                    session.commit()
                except Exception as record_err:
                    print(f"    (failed to record ingest error: {record_err})")

        session.close()
        print(f"Persisted {total} agenda items across {len(groups)} meeting(s)")
        if errors:
            print(f"{errors} meeting(s) had errors")
            return 1
        return 0

    # ── All jurisdictions ──
    if args.source in ("all", "all-jurisdictions") and args.sync:
        import subprocess as _sp
        import sys as _sys
        cmd = [_sys.executable, "scripts/run_pipeline.py"]
        if getattr(args, "limit", None):
            print(f"Note: daily sync ignores --limit={args.limit}; run individual jurisdictions for limits")
        print(f"\nSync will take ~4 minutes (32 jurisdictions, ~8s each). Output streams below:\n")
        proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            print(line, end="", flush=True)
        proc.wait()
        return proc.returncode

    if args.sync:
        from db import get_session, init_db, persist_meeting

        init_db()

        if not getattr(args, "from_file", False) and getattr(args, "meeting_id", None) and not getattr(args, "offline", False):
            meeting_id = args.meeting_id
            source_url = (
                "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
                f"?id={meeting_id}&doctype=1"
            )
            log.info(f"Syncing BOS meeting {meeting_id}...")
            meeting_prefix = f"BOS meeting_id={meeting_id}"

            # Use metadata from --meeting-* args if provided (parallel workers),
            # otherwise construct the source URL from the meeting ID and rely
            # on page-level metadata extraction as a fallback.
            meeting_date = getattr(args, "meeting_date", None) or ""
            meeting_type = getattr(args, "meeting_type", None) or ""
            meeting_title = getattr(args, "meeting_title", None) or ""
            meeting_url = getattr(args, "meeting_url", None) or ""

            if meeting_url:
                source_url = meeting_url
            else:
                source_url = (
                    "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
                    f"?id={meeting_id}&doctype=1"
                )

            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
                "meeting_title": meeting_title,
                "source_url": source_url,
            }
            extract_meeting = {
                "document_url": source_url,
                "agenda_url": source_url,
                "record_id": meeting_id,
                "meeting_id": meeting_id,
                "record_date": meeting_date,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
            }

            from db import create_or_get_meeting, update_sync_status, replace_meeting_data_safe

            session = get_session()
            try:
                # Ensure meeting row exists
                meeting = create_or_get_meeting(session, args.source, meeting_dict)
                session.commit()

                # Cancel detection
                from db import is_canceled_meeting, mark_meeting_canceled
                if is_canceled_meeting(meeting_dict):
                    mark_meeting_canceled(session, args.source, meeting_id)
                    log.info("%s canceled (no_agenda)", meeting_prefix)
                    session.close()
                    return 0

                # Check if we should skip complete
                if args.skip_complete and meeting.sync_status == "complete":
                    log.info("%s status=complete skipping", meeting_prefix)
                    session.close()
                    return 0

                async_playwright = get_async_playwright()
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=not args.headed)
                    page = await browser.new_page()
                    page.set_default_timeout(60000)
                    try:
                        # Mark as attempted
                        update_sync_status(session, args.source, meeting_id, meeting.sync_status)
                        session.commit()

                        # Establish OnBase server session by visiting the source
                        # page first.  The agenda page is a SPA that relies on
                        # a session cookie set by the search/home page; without
                        # it the AJAX call that populates agendaView fails.
                        try:
                            await page.goto(SOURCE_PAGE, wait_until="domcontentloaded", timeout=30000)
                            await page.goto(
                                SEARCH_BASE + "?dropid=11",
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                        except Exception:
                            log.warning("%s session_bootstrap failed, proceeding anyway", meeting_prefix)

                        retry = args.retry_count

                        # Extract agenda items with retry
                        async def do_extract_items():
                            return await extract_agenda_items_for_meeting(page, extract_meeting)

                        items = await retry_with_backoff(
                            lambda: do_extract_items(),
                            max_attempts=retry,
                            label=f"items {meeting_id}",
                        )

                        # After page loads, extract meeting metadata from the page
                        # and update the meeting_dict/extract_meeting with real values
                        page_meta = await extract_meeting_metadata_from_page(page, source_url)
                        if page_meta.get("meeting_date"):
                            meeting_dict["meeting_date"] = page_meta["meeting_date"]
                            extract_meeting["meeting_date"] = page_meta["meeting_date"]
                            extract_meeting["record_date"] = page_meta["meeting_date"]
                        if page_meta.get("meeting_type"):
                            meeting_dict["meeting_type"] = page_meta["meeting_type"]
                            extract_meeting["meeting_type"] = page_meta["meeting_type"]
                        if page_meta.get("meeting_title"):
                            meeting_dict["meeting_title"] = page_meta["meeting_title"]
                        if not items:
                            page_state = await get_page_state_summary(page)
                            if await is_image_based_agenda(page):
                                status = "manual_review"
                                error = "Unsupported agenda format: page loaded but no parseable agenda items found; possible image/scanned agenda"
                                log.warning("%s items=0 image_scanned page_state=%s", meeting_prefix, page_state)
                            else:
                                status = "failed"
                                error = "No agenda items found on page"
                                log.warning("%s items=0 not_found page_state=%s", meeting_prefix, page_state)
                            update_sync_status(
                                session, args.source, meeting_id, status,
                                error=error,
                            )
                            session.commit()
                            return 1

                        # Normalize agenda_item_id to include body prefix for global uniqueness
                        for it in items:
                            it["agenda_item_id"] = f"{args.source}-{it['agenda_item_id']}"

                        # Extract supporting documents with retry
                        async def do_extract_docs():
                            return await extract_supporting_documents_dynamic_concurrent(
                                page, items, source_url, concurrency=5
                            )

                        docs = []
                        docs_ok = True
                        try:
                            docs = await retry_with_backoff(
                                lambda: do_extract_docs(),
                                max_attempts=retry,
                                label=f"docs {meeting_id}",
                            )
                        except Exception as e:
                            docs_ok = False
                            log.warning("%s docs_extraction_failed error=%s", meeting_prefix, str(e)[:200])

                        # Normalize supporting docs' agenda_item_id to match prefixed items
                        for doc in docs:
                            doc["agenda_item_id"] = f"{args.source}-{doc['agenda_item_id']}"

                        if not docs_ok:
                            # Items succeeded but docs failed - partial
                            replace_meeting_data_safe(
                                session, args.source, meeting_id, meeting_dict, items,
                                supporting_doc_dicts=docs,
                            )
                            update_sync_status(
                                session, args.source, meeting_id, "partial",
                                item_count_expected=len(items),
                                item_count_actual=len(items),
                                supporting_doc_count=len(docs),
                                items_extracted=True,
                                supporting_docs_extracted=False,
                                error="Supporting document extraction failed",
                            )
                            session.commit()
                            log.info("%s items=%d docs=partial", meeting_prefix, len(items))
                        else:
                            replace_meeting_data_safe(
                                session, args.source, meeting_id, meeting_dict, items,
                                supporting_doc_dicts=docs,
                            )
                            session.commit()

                            # Build summary line
                            summary_parts = [f"{len(items)} items", f"{len(docs)} docs"]

                            # Extract and persist votes from the summary page
                            try:
                                summary_url = source_url.replace("doctype=1", "doctype=3")
                                from db import persist_votes
                                vote_items = [
                                    {"agenda_item_number": it.get("agenda_item_number", ""),
                                     "c_number": it.get("c_number", "")}
                                    for it in items
                                ]
                                supervisors, votes = await extract_votes_from_summary(
                                    page, summary_url, vote_items
                                )
                                if votes or supervisors:
                                    vote_count = persist_votes(session, args.source, meeting_id, supervisors, votes)
                                    session.commit()
                                    summary_parts.append(f"{vote_count} votes")
                            except Exception:
                                pass

                            log.info("%s %s", meeting_prefix, ", ".join(summary_parts))

                    except Exception as e:
                        # Items extraction failed
                        update_sync_status(
                            session, args.source, meeting_id, "failed",
                            error=str(e)[:500],
                        )
                        session.commit()
                        log.error("%s failed error=%s", meeting_prefix, str(e)[:500])
                        return 1
                    finally:
                        await browser.close()
            except Exception as e:
                session.rollback()
                raise
            finally:
                session.close()
            return 0

        if getattr(args, "offline", False) and getattr(args, "meeting_id", None):
            # Offline: auto-discover HTML file by meeting ID
            meeting_id = args.meeting_id
            search_dirs = [
                ROOT / "data" / "agenda-html",
                ROOT / "tests" / "fixtures",
                ROOT / "tests" / "fixtures" / "agendas",
                ROOT,
            ]
            found = None
            for d in search_dirs:
                if not d.exists():
                    continue
                for pattern in [f"*{meeting_id}*.html", f"*{meeting_id}*.htm"]:
                    candidates = list(d.glob(pattern))
                    if candidates:
                        found = candidates[0]
                        break
                if found:
                    break

            if not found:
                raise SystemExit(
                    f"No HTML file found for meeting {meeting_id}. "
                    "Save the agenda HTML to one of:"
                    f"\n  - data/agenda-html/{meeting_id}.html"
                    f"\n  - tests/fixtures/"
                )

            print(f"Offline sync: using {found}")
            # Delegate to the --from-file handler by rewriting args
            args.from_file = str(found)
            # Fall through to the from-file block below

        if getattr(args, "from_file", None):
            # Sync from a local HTML file — no server needed
            # Check this before --meeting-id so --from-file takes priority
            # when both flags are given
            fixture_path = Path(args.from_file)
            if not fixture_path.exists():
                raise SystemExit(f"File not found: {fixture_path}")

            # Parse meeting metadata from filename
            # Supports:
            #   {date}_{type}_{id}_agenda.html  (e.g. 2025-01-29_formal_4449_agenda.html)
            #   {id}_{type}_{date}.html         (e.g. 4667_formal_2026-04-22.html)
            fn_match = re.match(
                r"(\d{4}-\d{2}-\d{2})_(.+?)_(\d+)_agenda\.html"
                r"|(\d+)_(.+?)_(\d{4}-\d{2}-\d{2})\.html",
                fixture_path.name,
            )
            if fn_match:
                groups = fn_match.groups()
                if groups[0]:  # date_type_id_agenda pattern
                    meeting_date = groups[0]
                    meeting_type = groups[1].replace("_", " ").title()
                    meeting_id = groups[2]
                else:  # id_type_date pattern
                    meeting_id = groups[3]
                    meeting_type = groups[4].replace("_", " ").title()
                    meeting_date = groups[5]
            elif args.meeting_id:
                meeting_id = args.meeting_id
                meeting_date = ""
                meeting_type = ""
            else:
                raise SystemExit(
                    f"Could not parse meeting info from filename '{fixture_path.name}'. "
                    "Use --meeting-id to specify the meeting ID, "
                    "or rename the file to: YYYY-MM-DD_type_ID_agenda.html"
                )

            html = fixture_path.read_text(encoding="utf-8")
            source_url = (
                "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
                f"?id={meeting_id}&doctype=1"
            )

            meeting = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
            }
            meeting_dict = {
                "meeting_id": meeting_id,
                "meeting_date": meeting_date,
                "meeting_type": meeting_type,
                "meeting_title": f"Meeting {meeting_id}",
                "source_url": source_url,
            }

            items = parse_agenda_items_from_html(html, source_url, meeting)
            if not items:
                print(f"  {meeting_id}: 0 items (no agenda items found in file)")
                return 1

            # Extract supporting documents from the HTML
            docs = extract_supporting_documents_from_items(html, items, source_url)
            if docs:
                print(f"  {meeting_id}: {len(docs)} supporting document(s) found")

            session = get_session()
            count = persist_meeting(session, args.source, meeting_id, items, supporting_doc_dicts=docs)
            session.close()
            print(f"  {meeting_id} {meeting_date}: {count} items synced from '{fixture_path.name}'")
            return 0

        if not args.start_date or not args.end_date:
            if not args.retry_failed:
                raise SystemExit("--start-date and --end-date (or --date) are required for --sync, or use --meeting-id")
            # --retry-failed without dates: fetch meetings from DB by status
            log.info("[retry] mode started")
            from db import get_session, init_db, get_meetings_by_status
            init_db()
            session = get_session()
            retry_statuses = ["failed", "partial", "pending"]
            if args.include_manual_review:
                retry_statuses.append("manual_review")
            db_meetings = get_meetings_by_status(session, args.source, retry_statuses, force=False)
            session.close()
            if not db_meetings:
                log.info("[retry] no failed agendas found")
                return 0
            log.info("[retry] found %d failed agendas", len(db_meetings))
            meetings = [
                Meeting(
                    meeting_date=m.meeting_date or "",
                    meeting_time="",
                    meeting_title=m.meeting_title or "",
                    meeting_type=m.meeting_type,
                    body=m.body or args.source,
                    row_text="",
                    detail_url="",
                    agenda_url=m.source_url,
                )
                for m in db_meetings
            ]
        else:
            start_date = parse_date(args.start_date)
            end_date = parse_date(args.end_date)
            if end_date < start_date:
                raise SystemExit("--end-date must be on or after --start-date")
            search_url = build_search_url(start_date, end_date)
            print(f"Agenda Online search URL: {search_url}")

        from db import get_session, init_db, persist_meeting, get_meetings_by_date_range
        from db import create_or_get_meeting, update_sync_status, replace_meeting_data_safe

        init_db()
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                # When --retry-failed was used without dates, meetings is already
                # populated from the database — skip the search.
                session = get_session()
                if 'meetings' not in dir() or not meetings:
                    await page.goto(SOURCE_PAGE, wait_until="domcontentloaded")
                    await page.goto(search_url, wait_until="domcontentloaded")
                    search_meetings = await extract_meetings(page, search_url)

                    if args.force:
                        db_meetings = get_meetings_by_date_range(
                            session,
                            args.source,
                            start_date.isoformat(),
                            end_date.isoformat(),
                        )
                        seen_ids: set[str] = set()
                        merged: list[Meeting] = []
                        for m in search_meetings:
                            if m.meeting_id not in seen_ids:
                                seen_ids.add(m.meeting_id)
                                merged.append(m)
                        for db_m in db_meetings:
                            mid = db_m.meeting_id
                            if mid not in seen_ids:
                                seen_ids.add(mid)
                                merged.append(Meeting(
                                    meeting_date=db_m.meeting_date,
                                    meeting_time="",
                                    meeting_title=db_m.meeting_title,
                                    meeting_type=db_m.meeting_type,
                                    body=db_m.body,  # Must not be null — fails loudly if body unknown
                                    row_text="",
                                    detail_url="",
                                    agenda_url=db_m.source_url,
                                ))
                        meetings = merged
                    else:
                        meetings = search_meetings

                if not meetings:
                    print("No meetings found.")
                    return 0

                if args.limit is not None:
                    meetings = meetings[: args.limit]
                meeting_count = len(meetings)

                # Parallel mode: spawn one subprocess per meeting
                parallel = getattr(args, "parallel", 1)
                if parallel > 1 and meeting_count > 1 and not getattr(args, "meeting_id", None):
                    import subprocess, sys as _sys
                    import asyncio as _asyncio
                    p_count = min(parallel, meeting_count)
                    log.info("Parallel mode: %d meetings, %d concurrent workers", meeting_count, p_count)

                    script = _sys.argv[0]
                    base_cmd = ["python3", script, "bos", "--sync", "--force"] if args.force else ["python3", script, "bos", "--sync"]
                    if getattr(args, 'headed', False):
                        base_cmd.append("--headed")

                    # Filter queue based on sync status
                    from db import Meeting as MeetingModel
                    from sqlalchemy import select

                    retry_statuses = ["failed", "partial", "pending"]
                    if getattr(args, 'include_manual_review', False):
                        retry_statuses.append("manual_review")

                    # Pre-load sync statuses for all meetings
                    status_map = {}
                    try:
                        db_session = get_session()
                        for m in meetings:
                            row = db_session.execute(
                                select(MeetingModel.sync_status).where(
                                    MeetingModel.body == "bos",
                                    MeetingModel.meeting_id == m.meeting_id,
                                )
                            ).scalar_one_or_none()
                            if row:
                                status_map[m.meeting_id] = row
                        db_session.close()
                    except Exception:
                        pass

                    # Build queue filtered by status
                    queue = []
                    skipped_status = 0
                    for m in meetings:
                        status = status_map.get(m.meeting_id, "")
                        # Determine if we should process this meeting
                        should_process = True
                        if args.retry_failed and not args.force:
                            should_process = status in retry_statuses or status == ""
                        elif not args.force and status == "complete":
                            should_process = True  # parallel mode processes all by default
                        if not should_process:
                            skipped_status += 1
                            continue
                        queue.append({
                            "id": m.meeting_id,
                            "date": m.meeting_date or "",
                            "type": m.meeting_type or "",
                            "title": m.meeting_title or "",
                            "url": m.agenda_url or "",
                        })

                    active: list[tuple[dict, subprocess.Popen]] = []
                    ok = 0
                    errs = 0
                    idx = 0
                    total_queue = len(queue)

                    while idx < len(queue) or active:
                        while len(active) < p_count and idx < len(queue):
                            item = queue[idx]
                            cmd = list(base_cmd) + [
                                "--meeting-id", item["id"],
                                "--meeting-date", item["date"],
                                "--meeting-type", item["type"],
                                "--meeting-title", item["title"],
                            ]
                            log.info("  [%d/%d] Worker %s starting (%s %s)", idx + 1, total_queue, item["id"], item["date"], item["type"])
                            active.append((item, subprocess.Popen(cmd)))
                            idx += 1

                        await _asyncio.sleep(5)

                        still_active: list = []
                        for item, proc in active:
                            rc = proc.poll()
                            if rc is not None:
                                done_so_far = ok + errs + 1
                                log.info("  [%d/%d] Worker %s finished code=%d", done_so_far, total_queue, item["id"], rc)
                                if rc == 0:
                                    ok += 1
                                else:
                                    errs += 1
                            else:
                                still_active.append((item, proc))
                        active = still_active

                    log.info("Parallel done. %d OK, %d failed out of %d", ok, errs, total_queue)
                    if errs:
                        return 1
                    return 0

                total = 0
                errors = 0
                skipped = 0
                for idx, meeting in enumerate(meetings, 1):
                    meeting_prefix = f"[{idx}/{meeting_count}]"
                    meeting_t0 = time.monotonic()
                    meeting_type_str = meeting.meeting_type or ""
                    meeting_title_str = (meeting.meeting_title or meeting_type_str)[:40]
                    log.info(
                        "%s meeting_id=%s date=%s type=%s",
                        meeting_prefix, meeting.meeting_id, meeting.meeting_date, meeting_title_str,
                    )

                    meeting_dict = {
                        "meeting_id": meeting.meeting_id,
                        "meeting_date": meeting.meeting_date,
                        "meeting_type": meeting.meeting_type,
                        "meeting_title": meeting.meeting_title,
                        "source_url": meeting.agenda_url,
                    }

                    extract_meeting = {
                        "document_url": meeting.agenda_url,
                        "agenda_url": meeting.agenda_url,
                        "record_id": meeting.meeting_id,
                        "meeting_id": meeting.meeting_id,
                        "record_date": meeting.meeting_date,
                        "meeting_date": meeting.meeting_date,
                        "meeting_type": meeting.meeting_type,
                    }

                    # Ensure meeting row exists
                    db_meeting = create_or_get_meeting(session, args.source, meeting_dict)
                    session.commit()

                    # Canceled meeting detection — applies to meetings from
                    # search results, retry-failed, and the --meeting-id path.
                    from db import is_canceled_meeting, mark_meeting_canceled
                    if is_canceled_meeting(meeting_dict):
                        mark_meeting_canceled(session, args.source, meeting.meeting_id)
                        log.info("%s canceled (no_agenda)", meeting_prefix)
                        skipped += 1
                        continue

                    # Determine which statuses to retry
                    retry_statuses = ["failed", "partial", "pending"]
                    if args.include_manual_review:
                        retry_statuses.append("manual_review")
                    # When --retry-failed is used, only process retry_statuses
                    # When --force is used, process everything
                    # When neither, skip complete
                    if not args.force and args.retry_failed:
                        if db_meeting.sync_status not in retry_statuses:
                            log.info("%s skip reason=already_complete elapsed=%.1fs", meeting_prefix, time.monotonic() - meeting_t0)
                            skipped += 1
                            continue
                    elif not args.force and db_meeting.sync_status == "complete":
                        skipped += 1
                        continue
                    elif args.retry_failed and db_meeting.sync_status not in retry_statuses:
                        skipped += 1
                        continue

                    try:
                        log.info("%s phase=load_cached_agenda started", meeting_prefix)
                        _phase_t0 = time.monotonic()

                        # Extract agenda items with retry (server-side AJAX
                        # failures are intermittent; retry often recovers)
                        _retry_count = getattr(args, "retry_count", 3)
                        async def _do_extract():
                            return await extract_agenda_items_for_meeting(page, extract_meeting)
                        items = await retry_with_backoff(
                            _do_extract,
                            max_attempts=_retry_count,
                            label=f"items {meeting.meeting_id}",
                        )
                        log.info(
                            "%s phase=parse_agenda_items done items=%d elapsed=%.1fs",
                            meeting_prefix, len(items), time.monotonic() - _phase_t0,
                        )
                        if not items:
                            page_state = await get_page_state_summary(page)
                            if await is_image_based_agenda(page):
                                status = "manual_review"
                                error = "Unsupported agenda format: page loaded but no parseable agenda items found; possible image/scanned agenda"
                                log.warning(
                                    "%s phase=parse_agenda_items result=image_scanned status=%s page_state=%s",
                                    meeting_prefix, status, page_state,
                                )
                            else:
                                status = "failed"
                                error = "No agenda items found"
                                log.warning(
                                    "%s phase=parse_agenda_items result=no_items status=%s page_state=%s",
                                    meeting_prefix, status, page_state,
                                )
                            update_sync_status(
                                session, args.source, meeting.meeting_id, status,
                                error=error,
                            )
                            session.commit()
                            if status == "failed":
                                errors += 1
                            continue

                        log.info("%s phase=discover_documents started", meeting_prefix)
                        _doc_t0 = time.monotonic()
                        docs = await extract_supporting_documents_dynamic_concurrent(
                            page, items, meeting.agenda_url, concurrency=5
                        )
                        log.info(
                            "%s phase=discover_documents done item_docs=%d elapsed=%.1fs",
                            meeting_prefix, len(docs), time.monotonic() - _doc_t0,
                        )

                        # Normalize agenda_item_id to include body prefix for global uniqueness
                        for it in items:
                            it["agenda_item_id"] = f"{args.source}-{it['agenda_item_id']}"
                        # Also normalize supporting docs' IDs to match the prefixed format
                        for doc in docs:
                            doc["agenda_item_id"] = f"{args.source}-{doc['agenda_item_id']}"

                        try:
                            log.info("%s phase=persist started", meeting_prefix)
                            _persist_t0 = time.monotonic()
                            replace_meeting_data_safe(
                                session, args.source, meeting.meeting_id, meeting_dict, items,
                                supporting_doc_dicts=docs,
                            )
                            total += len(items)
                            log.info(
                                "%s complete items=%d docs=%d elapsed=%.1fs",
                                meeting_prefix, len(items), len(docs), time.monotonic() - meeting_t0,
                            )

                            # Extract votes from the summary page
                            try:
                                _vote_t0 = time.monotonic()
                                summary_url = meeting.agenda_url.replace("doctype=1", "doctype=3")
                                vote_items = [
                                    {"agenda_item_number": it.get("agenda_item_number", ""),
                                     "c_number": it.get("c_number", "")}
                                    for it in items
                                ]
                                from db import persist_votes
                                supervisors, votes = await extract_votes_from_summary(
                                    page, summary_url, vote_items
                                )
                                if votes:
                                    vote_count = persist_votes(session, args.source, meeting.meeting_id, supervisors, votes)
                                    log.info(
                                        "%s votes=%d elapsed=%.1fs",
                                        meeting_prefix, vote_count, time.monotonic() - _vote_t0,
                                    )
                                elif supervisors:
                                    vote_count = persist_votes(session, args.source, meeting.meeting_id, supervisors, votes)
                                    log.info(
                                        "%s no-item-votes supervisors=%d elapsed=%.1fs",
                                        meeting_prefix, len(supervisors), time.monotonic() - _vote_t0,
                                    )
                            except Exception as ve:
                                log.warning("%s vote-extraction skipped: %s", meeting_prefix, str(ve)[:200])
                                # Roll back the session so the next meeting doesn't hit PendingRollbackError
                                session.rollback()

                        except Exception as e:
                            update_sync_status(
                                session, args.source, meeting.meeting_id, "failed",
                                error=str(e)[:500],
                            )
                            session.commit()
                            log.error(
                                "%s persist failed error=%s elapsed=%.1fs",
                                meeting_prefix, str(e)[:200], time.monotonic() - meeting_t0,
                            )
                            errors += 1
                    except Exception as e:
                        # Pre-extraction failure
                        update_sync_status(
                            session, args.source, meeting.meeting_id, "failed",
                            error=str(e)[:500],
                        )
                        session.commit()
                        print(f"  {meeting.meeting_id} {meeting.meeting_date}: FAILED - {e}")
                        errors += 1

                session.close()
                print(f"Synced {total} agenda items across {len(meetings)} meeting(s)")
                if skipped:
                    print(f"{skipped} meeting(s) skipped (status=complete), use --force to re-sync")
                if errors:
                    print(f"{errors} meeting(s) had errors")
                    return 1
            finally:
                await browser.close()
        return 0

    # Standalone --sync-votes: re-run vote extraction for already-synced meetings
    if getattr(args, "sync_votes", False):
        from db import get_session, init_db, persist_votes

        init_db()

        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                if args.meeting_id:
                    meeting_id = args.meeting_id
                    summary_url = (
                        "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
                        f"?id={meeting_id}&doctype=3"
                    )
                    print(f"Extracting votes from meeting {meeting_id}...")
                    print(f"  Summary URL: {summary_url}")

                    # Load agenda items for C-number matching
                    from db import Meeting as MeetingModel, AgendaItem
                    from sqlalchemy import select
                    session = get_session()
                    body = args.source if hasattr(args, 'source') else "bos"
                    try:
                        meeting = session.execute(
                            select(MeetingModel).where(
                                MeetingModel.body == body,
                                MeetingModel.meeting_id == meeting_id,
                            )
                        ).scalar_one_or_none()
                        if not meeting:
                            print(f"  {body} meeting {meeting_id} not found in database. Run --sync first.")
                            return 1
                        db_items = session.execute(
                            select(AgendaItem)
                            .where(
                                AgendaItem.body == body,
                                AgendaItem.meeting_id == meeting_id,
                            )
                            .order_by(AgendaItem.agenda_item_number)
                        ).scalars().all()
                        agenda_items = [
                            {
                                "agenda_item_number": str(it.agenda_item_number),
                                "c_number": it.c_number or "",
                            }
                            for it in db_items
                        ]
                        print(f"  Found {len(agenda_items)} agenda items for C-number matching")
                    except Exception as e:
                        print(f"  WARNING: Could not load agenda items: {e}")
                        agenda_items = []
                    finally:
                        session.close()

                    supervisors, votes = await extract_votes_from_summary(
                        page, summary_url, agenda_items
                    )

                    if not votes:
                        print(f"  No vote results found in summary for meeting {meeting_id}")
                        if supervisors:
                            print(f"  Found {len(supervisors)} supervisors present")
                        return 0

                    print(f"  Found {len(supervisors)} supervisor(s)")
                    for sup in supervisors:
                        district_str = f", District {sup['district']}" if sup.get('district') else ""
                        role_str = f" ({sup['role']})" if sup.get('role') else ""
                        print(f"    {sup['name']}{district_str}{role_str}")

                    print(f"  Found {len(votes)} item(s) with votes")
                    for v in votes:
                        c_str = f" ({v.get('c_number', '')})" if v.get('c_number') else ""
                        sv_summary = ", ".join(
                            f"{sv['name']}: {sv['vote']}"
                            for sv in v.get("supervisor_votes", [])
                        )
                        print(f"    #{v['agenda_item_number']}{c_str}: {v.get('motion_result', 'unknown')}")
                        if sv_summary:
                            print(f"      {sv_summary}")

                    # Persist to database
                    session = get_session()
                    try:
                        vote_count = persist_votes(session, body, meeting_id, supervisors, votes)
                        print(f"  Persisted {vote_count} vote record(s)")
                    finally:
                        session.close()
                else:
                    print("--sync-votes requires --meeting-id to specify a meeting")
                    return 1
            finally:
                await browser.close()
        return 0

    if args.count_agenda_items or args.list_agenda_items:
        if not args.start_date or not args.end_date:
            raise SystemExit("--start-date and --end-date are required")
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)
        if end_date < start_date:
            raise SystemExit("--end-date must be on or after --start-date")

        search_url = build_search_url(start_date, end_date)
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                await page.goto(SOURCE_PAGE, wait_until="domcontentloaded")
                await page.goto(search_url, wait_until="domcontentloaded")
                meetings = await extract_meetings(page, search_url)

                if not meetings:
                    print(f"No meetings found for {start_date.isoformat()} through {end_date.isoformat()}")
                    return 0

                if args.limit is not None:
                    meetings = meetings[: args.limit]

                if args.count_agenda_items:
                    print(f"{'ID':>6}  {'Date':<12}  {'Count':>5}  {'Title'}")
                    print(f"{'------':>6}  {'------------':<12}  {'-----':>5}  {'-----'}")
                    total = 0
                    for meeting in meetings:
                        count = await count_agenda_items_for_meeting(page, meeting.agenda_url)
                        total += count
                        print(f"{meeting.meeting_id:>6}  {meeting.meeting_date:<12}  {count:>5}  {meeting.meeting_title}")
                    print()
                    print(f"{len(meetings)} meeting(s), {total} total items")
                else:
                    for meeting in meetings:
                        items = await extract_agenda_item_titles(page, meeting.agenda_url)
                        print()
                        print(f"{'=' * 70}")
                        print(f"{meeting.meeting_id}  {meeting.meeting_date}  {meeting.meeting_type}  {meeting.meeting_title}")
                        print(f"{len(items)} items")
                        print(f"{'=' * 70}")
                        for num, title in items:
                            print(f"  {num:>4}.  {title}")
                    print()
                    print(f"{len(meetings)} meeting(s)")
            finally:
                await browser.close()
        return 0

    if args.extract_agenda_items:
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                if args.debug_agenda_html:
                    meeting_rows = read_agenda_metadata_rows()
                    if meeting_rows:
                        await page.goto((meeting_rows[0].get("document_url") or meeting_rows[0].get("agenda_url") or "").strip(), wait_until="domcontentloaded")
                        await page.wait_for_timeout(1000)
                        await write_agenda_debug_files(page, meeting_rows[0])
                start_date = parse_date(args.start_date) if args.start_date else None
                end_date = parse_date(args.end_date) if args.end_date else None
                wrote = await extract_agenda_items_from_metadata(
                    page, start_date=start_date, end_date=end_date, limit=args.limit
                )
                print(f"Extracted {wrote} agenda item row(s)")
            finally:
                await browser.close()
        return 0

    if args.extract_raw_agenda_blocks:
        start_date = parse_date(args.start_date) if args.start_date else None
        end_date = parse_date(args.end_date) if args.end_date else None
        meeting_rows = filter_agenda_metadata_rows(read_agenda_metadata_rows(), start_date, end_date, args.limit)
        ensure_dir(AGENDA_ITEMS_ROOT)
        if not RAW_AGENDA_ITEMS_CSV.exists():
            RAW_AGENDA_ITEMS_CSV.write_text(
                "source_body,meeting_id,meeting_date,meeting_type,raw_block_index,raw_text,source_url\n",
                encoding="utf-8",
            )
        if not meeting_rows:
            print("No agenda metadata rows matched the selected date range/limit.")
            return 0
        async_playwright = get_async_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not args.headed)
            page = await browser.new_page()
            page.set_default_timeout(60000)
            try:
                wrote_raw = await extract_raw_agenda_blocks_from_metadata(page, meeting_rows)
                if wrote_raw == 0:
                    print("No raw agenda blocks were extracted for the selected meeting(s).")
                    print("Possible causes: no matching agenda rows, selector mismatch, or the agenda HTML layout changed.")
                    return 0
                wrote_structured = split_raw_agenda_blocks_to_structured()
                print(f"Extracted {wrote_raw} raw agenda block row(s)")
                print(f"Extracted {wrote_structured} structured agenda item row(s)")
                if wrote_structured == 0:
                    print("All raw blocks were rejected; see data/agenda-items/rejected_raw_blocks.csv")
            finally:
                await browser.close()
        return 0

    if args.split_raw_agenda_blocks:
        wrote = split_raw_agenda_blocks_to_structured()
        print(f"Extracted {wrote} structured agenda item row(s)")
        return 0

    if not args.start_date or not args.end_date:
        raise SystemExit("--start-date and --end-date are required unless --extract-agenda-items, --extract-raw-agenda-blocks, --split-raw-agenda-blocks, or --sync is used")

    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if end_date < start_date:
        raise SystemExit("--end-date must be on or after --start-date")

    search_url = build_search_url(start_date, end_date)
    existing = read_existing_rows()
    existing_agenda_urls = read_existing_agenda_urls([DISCOVERY_CSV, *AGENDAS_ROOT.rglob("metadata.csv")])
    existing_discovery_keys = read_existing_discovery_keys(DISCOVERY_CSV)

    ensure_dir(AGENDAS_ROOT)
    ensure_dir(SUPPORT_ROOT)
    ensure_dir(AGENDA_ITEMS_ROOT)
    ensure_dir(DISCOVERY_CSV.parent)

    print(f"Agenda Online search URL: {search_url}")

    async_playwright = get_async_playwright()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        page = await browser.new_page()
        page.set_default_timeout(60000)
        try:
            await page.goto(SOURCE_PAGE, wait_until="domcontentloaded")
            await page.goto(search_url, wait_until="domcontentloaded")
            meetings = await extract_meetings(page, search_url)
            print(f"Detected {len(meetings)} meeting row(s)")

            if not meetings:
                print(f"No meetings found for {start_date.isoformat()} through {end_date.isoformat()}")
                return 0

            processed = 0
            for meeting in meetings:
                if args.limit is not None and processed >= args.limit:
                    break
                processed += 1

                agenda_month_dir = month_dir_for_date(meeting.meeting_date, AGENDAS_ROOT)
                support_month_dir = month_dir_for_date(meeting.meeting_date, SUPPORT_ROOT)
                ensure_dir(agenda_month_dir)
                ensure_dir(support_month_dir)

                existing_row = existing.get(meeting.agenda_url)
                if args.download and existing_row and row_paths_present(existing_row):
                    continue

                time_part = f"{slugify(meeting.meeting_time)}_" if meeting.meeting_time else ""
                prefix = f"{meeting.meeting_date}_{time_part}{slugify(meeting.meeting_type)}_{meeting.meeting_id}"
                agenda_path = agenda_month_dir / f"{prefix}_agenda.pdf"
                supporting_paths: list[str] = []

                if not args.download:
                    print(f"{meeting.meeting_date} | {meeting.meeting_title} | {meeting.meeting_type}")
                    print(f"  agenda_url: {meeting.agenda_url}")
                    print(f"  summary_url: {meeting.summary_url or 'none'}")
                    print(f"  minutes_url: {meeting.minutes_url or 'none'}")
                    print(f"  video_url: {meeting.video_url or 'none'}")
                    print("  supporting_materials_url: none")

                if args.download:
                    if not agenda_path.exists():
                        agenda_path, _ = download_url(meeting.agenda_url, agenda_path)
                else:
                    supporting_paths = []

                row = {
                    "source_body": "Board of Supervisors",
                    "document_category": "agenda",
                    "record_id": meeting.meeting_id,
                    "record_date": meeting.meeting_date,
                    "record_time": meeting.meeting_time,
                    "record_title": meeting.meeting_title,
                    "meeting_type": meeting.meeting_type,
                    "source_page_url": SOURCE_PAGE,
                    "document_url": meeting.agenda_url,
                    "local_path": str(agenda_path.relative_to(ROOT)),
                    "download_status": "downloaded",
                    "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "source_search_url": search_url,
                    "notes": "",
                }

                if args.download and meeting.agenda_url not in existing:
                    write_download_row(row)
                    existing[meeting.agenda_url] = row
                elif not args.download:
                    write_discovery_rows(meeting, search_url, existing_discovery_keys)
        finally:
            await browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
