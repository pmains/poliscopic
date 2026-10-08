"""
City of Glendale meeting and agenda extraction via AgendaQuick (Destiny Software).

Uses ``scraper.destiny_common`` (HTML-parser-based) for all parsing.
"""

from __future__ import annotations
import logging
from typing import Optional

from scraper.platforms.destiny_common import (
    BASE_URL,
    build_month_url as _build_month_url,
    extract_meeting_type,
    fetch_page,
    parse_agenda_items as _parse_agenda_items,
    parse_meetings as _parse_meetings,
)

log = logging.getLogger(__name__)

PUBLIC_BODY_CODE = "glendale-cc"
SOURCE_SYSTEM = "agendaquick"
SOURCE_INSTANCE_URL = "https://public.destinyhosted.com"

GLENDALE_ID = "45363"

# Body map: maps body-name keywords → (slug, code).
# Codes are from the <select name="mt"> on the Glendale AgendaQuick page.
BODY_MAP: dict[str, tuple[str, str]] = {
    "city council": ("glendale-city-council", "glendale-cc"),
    "planning commission": ("glendale-planning-commission", "glendale-pc"),
    "board of adjustment": ("glendale-board-of-adjustment", "glendale-boa"),
    "historic preservation commission": ("glendale-historic-preservation-commission", "glendale-hpc"),
    "historic preservation": ("glendale-historic-preservation-commission", "glendale-hpc"),
    "parks and recreation advisory commission": ("glendale-parks-recreation-advisory", "glendale-parac"),
    "parks and recreation": ("glendale-parks-recreation-advisory", "glendale-parac"),
    "arts commission": ("glendale-arts-commission", "glendale-ac"),
    "airport commission": ("glendale-airport-advisory", "glendale-avac"),
    "aviation advisory": ("glendale-airport-advisory", "glendale-avac"),
    "aviation": ("glendale-airport-advisory", "glendale-avac"),
    "library advisory board": ("glendale-library-advisory-board", "glendale-lab"),
    "library": ("glendale-library-advisory-board", "glendale-lab"),
    "commission on community and culture": ("glendale-community-culture", "glendale-cocc"),
    "community and culture": ("glendale-community-culture", "glendale-cocc"),
    "community & culture": ("glendale-community-culture", "glendale-cocc"),
    "citizens transportation oversight": ("glendale-transportation-oversight", "glendale-citoc"),
    "transportation oversight": ("glendale-transportation-oversight", "glendale-citoc"),
    "citizens utility advisory": ("glendale-utility-advisory", "glendale-ciuac"),
    "utility advisory": ("glendale-utility-advisory", "glendale-ciuac"),
    "citizen bicycle": ("glendale-bicycle-advisory", "glendale-cbac"),
    "citizens active transportation": ("glendale-active-transportation", "glendale-catac"),
    "active transportation": ("glendale-active-transportation", "glendale-catac"),
    "human relations commission": ("glendale-human-relations", "glendale-hrc"),
    "human relations": ("glendale-human-relations", "glendale-hrc"),
    "public safety personnel retirement": ("glendale-psprs", "glendale-psprb"),
    "psprs": ("glendale-psprs", "glendale-psprb"),
    "industrial development authority": ("glendale-ida", "glendale-ida"),
    "ida": ("glendale-ida", "glendale-ida"),
    "bond committee": ("glendale-bond-committee", "glendale-bc"),
    "bond": ("glendale-bond-committee", "glendale-bc"),
    "risk management trust": ("glendale-risk-management", "glendale-rmtfb"),
    "workers' compensation trust": ("glendale-workers-comp", "glendale-wctfb"),
    "workers compensation": ("glendale-workers-comp", "glendale-wctfb"),
    "personnel board": ("glendale-personnel-board", "glendale-pb"),
    "commission on persons with disabilities": ("glendale-persons-disabilities", "glendale-cmpd"),
    "persons with disabilities": ("glendale-persons-disabilities", "glendale-cmpd"),
    "community development advisory": ("glendale-community-development-advisory", "glendale-codac"),
    "audit": ("glendale-government-services", "glendale-gsc"),
    "audit committee": ("glendale-government-services", "glendale-gsc"),
    "government services committee": ("glendale-government-services", "glendale-gsc"),
    "commission on diverse cultures": ("glendale-diverse-cultures", "glendale-cdc"),
    "council compensation": ("glendale-council-compensation", "glendale-ccc"),
    "business council": ("glendale-business-council", "glendale-bcc"),
    "municipal property corporation": ("glendale-municipal-property", "glendale-mpc"),
    # Public notices are council postings, not a separate governing body.
    "public notices": ("glendale-city-council", "glendale-cc"),
    "code review": ("glendale-code-review", "glendale-ccr"),
    "abatement hearing": ("glendale-abatement-hearing", "glendale-ah"),
    "judicial selection": ("glendale-judicial-selection", "glendale-jsab"),
    "west valley": ("glendale-west-valley-dv", "glendale-wvdv"),
}

DEFAULT_BODY_SLUGS = ["glendale-city-council"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ── Body resolution ──


def _resolve_body(body_name: str) -> tuple[str, str]:
    """Match a body-name string to (slug, code)."""
    import re as _re
    key = _re.sub(r'\s+', ' ', body_name).lower().strip()
    for pattern, (slug, code) in BODY_MAP.items():
        if pattern in key:
            return slug, code
    return "glendale-city-council", "glendale-cc"


def extract_meeting_type(body_name: str) -> str:
    """Extract meeting type. Delegates to destiny_common."""
    return extract_meeting_type(body_name)


# ── Month URL builder ──


def build_month_url(year: int, month: int, mt: str = "ALL") -> str:
    """Build Destiny monthly view URL for Glendale."""
    return (
        f"{BASE_URL}/agenda_publish.cfm?id={GLENDALE_ID}"
        f"&mt={mt}&get_month={month}&get_year={year}"
    )


# ── Meeting search ──


def search_glendale_meetings(
    year: int,
    body_slugs: Optional[list[str]] = None,
    start_month: int = 1,
    end_month: int = 12,
) -> list[dict]:
    """Search Glendale meetings for a given year, month by month.

    Args:
        year: The year to search.
        body_slugs: Optional list of body slugs to filter by.
        start_month: First month to fetch (1-12, default 1).
        end_month: Last month to fetch (1-12, default 12).
    """
    all_m: list[dict] = []
    start_month = max(1, min(12, start_month))
    end_month = max(start_month, min(12, end_month))
    for m in range(start_month, end_month + 1):
        try:
            html = fetch_page(build_month_url(year, m), timeout=15)
            all_m.extend(_parse_meetings(html, BODY_MAP))
        except Exception as e:
            log.warning("Glendale %d-%02d failed: %s", year, m, e)
    if body_slugs is not None:
        all_m = [m for m in all_m if m["body_slug"] in body_slugs]
    log.info("Glendale %d: %d meetings" % (year, len(all_m)))
    return all_m


# ── Agenda item parsing (delegated to destiny_common) ──


def parse_agenda_items(html: str, meeting_seq: str) -> list[dict]:
    """Parse agenda items. Delegates to destiny_common."""
    return _parse_agenda_items(html, meeting_seq)


# ── Section normalization ──


def _normalize_section(label: str) -> str:
    """Normalize Glendale agenda section labels to canonical names."""
    if "CALL TO ORDER" in label:
        return "Call to Order"
    if "ROLL CALL" in label:
        return "Roll Call"
    if "PRAYER" in label or "INVOCATION" in label:
        return "Prayer/Invocation"
    if "POSTING" in label:
        return "Posting of Colors"
    if "PLEDGE" in label:
        return "Pledge of Allegiance"
    if "APPROVAL" in label and "MINUTES" in label:
        return "Approval of Minutes"
    if "CONSENT" in label and "RESOLUTION" in label:
        return "Consent Resolutions"
    if "CONSENT" in label:
        return "Consent Agenda"
    if "RESOLUTION" in label:
        return "Resolutions"
    if "BOARDS" in label or "COMMISSIONS" in label:
        return "Boards, Commissions & Other Bodies"
    if "COUNCIL COMMENTS" in label:
        return "Council Comments"
    if "CITIZEN COMMENTS" in label or "PUBLIC COMMENTS" in label:
        return "Citizen Comments"
    if "PUBLIC HEARING" in label:
        return "Public Hearings"
    if "UNFINISHED BUSINESS" in label:
        return "Unfinished Business"
    if "NEW BUSINESS" in label:
        return "New Business"
    if "ADJOURN" in label:
        return "Adjournment"
    return label.title()


# ── Vote / Results PDF parsing (unchanged) ──


def fetch_results_pdf_bytes(results_url: str) -> Optional[bytes]:
    import urllib.request
    try:
        req = urllib.request.Request(results_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Results PDF not available: %s", e)
        return None


def extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    import subprocess, tempfile, os
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            pdf_path = f.name
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        log.debug("pdftotext failed: %s", e)
        return None
    finally:
        try:
            os.unlink(pdf_path)
        except (NameError, OSError):
            pass


def parse_results_votes(text: str) -> dict:
    """Parse Glendale Results PDF (Destiny format)."""
    supervisors: list[dict] = []
    votes: list[dict] = []
    seen_sup: set[str] = set()
    lines = text.split("\n")
    i = 0
    vote_count_re = re.compile(r"(\d+)-(\d+)")
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        all_vc = list(vote_count_re.finditer(line))
        if not all_vc:
            i += 1
            continue
        vc = all_vc[-1]
        ayes_count = int(vc.group(1))
        nays_count = int(vc.group(2))
        result = "Carried Unanimously" if nays_count == 0 else "Carried"
        votes.append({
            "agenda_item_number": "",
            "motion_result": result,
            "supervisor_votes": [],
            "vote_text": line.strip(),
        })
        i += 1
    return {"supervisors": supervisors, "votes": votes}


# ── CLI entry point (local testing) ──


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Glendale AgendaQuick scraper (local test)")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--month", type=int, default=None, help="Single month (1-12)")
    parser.add_argument("--body", default=None, help="Body slug to filter by")
    parser.add_argument("--agenda", default=None, help="Agenda seq to parse and print")
    args = parser.parse_args()

    if args.agenda:
        url = (
            f"{BASE_URL}/agenda_publish.cfm?id={GLENDALE_ID}"
            f"&mt=ALL&get_month=1&get_year=2026&dsp=ag&seq={args.agenda}"
        )
        html = fetch_page(url)
        items = parse_agenda_items(html, args.agenda)
        print(f"Agenda {args.agenda}: {len(items)} items")
        for it in items:
            print(f"  #{it['agenda_item_number']}: {it['agenda_item_title'][:80]}")
        return

    body_slugs = [args.body] if args.body else DEFAULT_BODY_SLUGS
    months = [args.month] if args.month else range(1, 13)

    all_meetings = []
    for m in months:
        html = fetch_page(build_month_url(args.year, m))
        meetings = _parse_meetings(html, BODY_MAP)
        all_meetings.extend(meetings)

    if args.body:
        all_meetings = [m for m in all_meetings if m["body_slug"] in body_slugs]
    print(f"Glendale {args.year}: {len(all_meetings)} meetings")
    for m in all_meetings:
        print(f"{m['meeting_date']} {m['body_code']:20s} {m['body_name'][:60]}")


if __name__ == "__main__":
    main()
