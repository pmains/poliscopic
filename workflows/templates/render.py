"""render.py — Deterministic newsletter HTML renderer.

Takes verified report data and renders it through the HTML template.
No LLM involved in formatting — consistent output every time.
"""

import html as _html
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

TEMPLATE_PATH = Path(__file__).parent / "newsletter.html"
POLISCOPIC_BASE = "https://poliscopic.com"

# Markdown inline link: [label](url). Used for §1 overview links.
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

# Machine-facing item tokens the editorial step used to emit for claim
# anchoring, e.g. "(bos item 44)", "(phoenix-cc items 102 and 103)",
# "(item 90)". Never reach readers (Pete directive 2026-09-08).
ITEM_TOKEN_RE = re.compile(
    r"\(\s*(?:[A-Za-z][A-Za-z0-9-]*\s+)?item[s]?\s+"
    r"\d[0-9A-Za-z.\-]*(?:\s+and\s+\d[0-9A-Za-z.\-]*)*\s*\)"
)

# US street addresses (house number + name + suffix), e.g. "1338 W Lobo
# Avenue". Gmail auto-linkifies bare addresses into Google Maps links;
# the fix (Pete directive 2026-09-08, per SO 46341944) is to beat Gmail to
# it: wrap addresses in our own <a> styled to look like plain text, so
# Gmail never sees an unlinked address pattern.
STREET_ADDR_RE = re.compile(
    r"\b\d{1,6}\s+"
    r"(?:(?:N|S|E|W|North|South|East|West)\.?\s+)?"
    r"[A-Za-z0-9.'\-]+(?:\s+[A-Za-z0-9.'\-]+){0,3}\s+"
    r"(?:Ave(?:nue)?|St(?:reet)?|Rd(?:oad)?|Blvd(?:evard)?|Dr(?:ive)?|"
    r"Ln(?:ane)?|Way|Ct(?:ourt)?|Pl(?:ace)?|Trl(?:ail)?|Pkwy(?:arkway)?|"
    r"Hwy(?:ighway)?|Cir(?:cle)?|Loop|Ter(?:race)?|Pass|Run|Crossing|Xing)"
    r"\b"
)

# <a> we wrap addresses in so Gmail's auto-linker leaves them alone.
# Inline styles make it look exactly like surrounding text (no link look).
_ADDR_ANCHOR_TPL = '<a href="" style="color: inherit; text-decoration: none !important; cursor: default;">{addr}</a>'

# Tokens that must never appear inside a matched street address (filters out
# intersections "17 and Rose Garden Lane" and acreage "160 acres at Deer
# Valley Road").
_ADDR_STOPWORDS = {
    "a", "an", "about", "acres", "acre", "and", "at", "block", "by",
    "feet", "for", "from", "ft", "in", "lots", "lot", "near", "of",
    "on", "parcel", "roughly", "the", "to", "with", "within",
}


def _protect_street_addresses(escaped_text: str) -> str:
    """Wrap bare US street addresses in plain-looking <a> tags.

    Operates on HTML-escaped plain text (addresses already inside our own
    links — e.g. poliscopic deep-link anchors — are untouched, so no nested
    anchors). Gmail's client-side address auto-linker then has no bare
    address to turn into a Google Maps link.

    Candidates are validated so intersection/junction phrases ("I-17 and
    Rose Garden Lane", "160 acres at Deer Valley Road") are NOT wrapped:
    the street-name portion must contain an uppercase letter and no
    stopwords.
    """

    def _valid(m: re.Match) -> bool:
        candidate = m.group(0)
        street_part = candidate
        m2 = re.match(r"\d{1,6}\s+(?:[NESWnesw]\s+)?", candidate)
        if m2:
            street_part = candidate[m2.end():]
        toks = [t.strip(".") for t in street_part.split()]
        toks = [t for t in toks if t]
        if not toks:
            return False
        if any(t.lower() in _ADDR_STOPWORDS for t in toks[:-1]):
            return False
        if not any(c.isupper() for c in street_part):
            return False
        return True

    def _repl(m: re.Match) -> str:
        if not _valid(m):
            return m.group(0)
        return _ADDR_ANCHOR_TPL.format(addr=m.group(0))

    return STREET_ADDR_RE.sub(_repl, escaped_text)


def _strip_item_tokens(text: str) -> str:
    """Remove stray machine-facing item tokens (defensive; instructions ban
    them upstream, this guarantees none can ship to email or website)."""
    return re.sub(r" {2,}", " ", ITEM_TOKEN_RE.sub("", text))


def _summary_paragraph_html(text: str, allowed_links: set[str]) -> str:
    """Escape prose and convert markdown [label](deep-link) to an <a> tag.

    Only hrefs that exactly match an allowed poliscopic deep link from this
    run's items become anchors (Pete directive 2026-09-08: no machine tokens
    like "(bos item 44)" reach readers — items are linked on natural phrases,
    and the renderer does the linking deterministically). Any other link
    syntax is reduced to its label text so an invented/hallucinated URL can
    never reach the email.
    """
    text = _strip_item_tokens(text)
    parts: list[str] = []
    pos = 0
    for m in MD_LINK_RE.finditer(text):
        parts.append(_protect_street_addresses(_html.escape(text[pos : m.start()])))
        label, url = m.group(1), m.group(2)
        if url in allowed_links:
            parts.append(
                f'<a href="{_html.escape(url, quote=True)}" '
                f'style="color: #2266cc; text-decoration: underline;">'
                f'{_html.escape(label)}</a>'
            )
        else:
            parts.append(_protect_street_addresses(_html.escape(label)))  # keep label, drop the link
        pos = m.end()
    parts.append(_protect_street_addresses(_html.escape(text[pos:])))
    return "".join(parts)



def _item_html(item: dict) -> str:
    """Render a single agenda item as HTML."""
    item_number = item.get("agenda_item_number", "")
    body = item.get("body", "")
    meeting_id = item.get("meeting_id", "")
    title = item.get("agenda_item_title", "Untitled Item")
    reason = item.get("reason", "")
    meeting_title = item.get("meeting_title", "")
    meeting_date = item.get("meeting_date", "")

    link = f"{POLISCOPIC_BASE}/meetings/{body}/{meeting_id}#item-{item_number}"
    meeting_label = f"{meeting_title} ({meeting_date})"

    reason_html = _protect_street_addresses(
        _strip_item_tokens(_html.escape(reason or ""))
    )

    return f"""<h3 style="color: #333; font-size: 1em; margin-top: 1.5em; margin-bottom: 0.3em;">
  <a href="{link}" style="color: #2266cc; text-decoration: none;">{title}</a>
</h3>
<p style="color: #555; line-height: 1.5; margin-top: 0.3em; margin-bottom: 0.3em;">
  {reason_html}
</p>
<p style="color: #888; font-size: 0.85em; margin-top: 0; font-weight: bold;">
  <a href="{link}" style="color: #2266cc; font-weight: bold;">{meeting_label} — Item {item_number}</a>
</p>"""


def _items_html(items: list[dict]) -> str:
    """Render a list of items as HTML."""
    if not items:
        return '<p style="color: #888;">None this period.</p>'
    return "\n".join(_item_html(item) for item in items)


def render_newsletter(
    topic_name: str,
    date_str: str,
    items: list[dict],
    summary: str = "",
    no_items_message: str = "No relevant items found for this period.",
    data_gaps: Optional[list[dict]] = None,
) -> str:
    """Render the full newsletter HTML from verified report data.

    Items are automatically split into past (meeting_date < today) and
    upcoming (meeting_date >= today) sections.

    Args:
        topic_name: Display name (e.g., "Transportation & Infrastructure")
        date_str: Formatted date string (e.g., "July 21, 2026")
        items: List of agenda item dicts with keys:
            agenda_item_number, body, meeting_id, meeting_title,
            meeting_date, agenda_item_title, reason, relevance
        summary: Optional highlights paragraph (from verify step)
        no_items_message: Text to show when no items exist
        data_gaps: Optional list of §3 data-gap entries (body, meeting_date,
            note/gap_reason) rendered as a "Data gaps & notes" block.

    Returns:
        Complete HTML string ready to send via email.
    """
    template = TEMPLATE_PATH.read_text()

    today = date.today()

    # Body code → human-readable display name
    BODY_NAMES = {
        "bos": "Maricopa County Board of Supervisors",
        "apache-junction-cc": "Apache Junction City Council",
        "avondale-cc": "Avondale City Council",
        "buckeye-cc": "Buckeye City Council",
        "buckeye-pz": "Buckeye Planning & Zoning",
        "chandler-cc": "Chandler City Council",
        "chandler-pz": "Chandler Planning & Zoning",
        "el-mirage-cc": "El Mirage City Council",
        "el-mirage-pz": "El Mirage Planning & Zoning",
        "glendale-cc": "Glendale City Council",
        "glendale-pz": "Glendale Planning & Zoning",
        "goodyear-cc": "Goodyear City Council",
        "mesa-city-council": "Mesa City Council",
        "mesa-pz": "Mesa Planning & Zoning",
        "peoria-cc": "Peoria City Council",
        "peoria-planning-zoning": "Peoria Planning & Zoning",
        "phoenix-cc": "Phoenix City Council",
        "phoenix-pz": "Phoenix Planning & Zoning",
        "scottsdale-cc": "Scottsdale City Council",
        "scottsdale-pz": "Scottsdale Planning & Zoning",
        "surprise-cc": "Surprise City Council",
        "surprise-pz": "Surprise Planning & Zoning",
        "tempe-cc": "Tempe City Council",
        "tempe-pz": "Tempe Planning & Zoning",
        "tempe-boa": "Tempe Board of Adjustment",
        "tucson-cc": "Tucson City Council",
        "maricopa-county": "Maricopa County",
    }

    past_items = []
    upcoming_items = []

    for item in items:
        md = item.get("meeting_date", "")
        try:
            item_date = date.fromisoformat(md) if md else today
        except ValueError:
            item_date = today

        if item_date < today:
            past_items.append(item)
        else:
            upcoming_items.append(item)

    # Sort: newest first for past, soonest first for upcoming
    past_items.sort(key=lambda i: i.get("meeting_date", ""), reverse=True)
    upcoming_items.sort(key=lambda i: i.get("meeting_date", ""))

    # Summary — medium font, 1.5 line-height (§1 headline from verify).
    # The editorial overview may be multiple paragraphs (\n\n-separated).
    # HTML collapses newlines, so each paragraph gets its own <p> — otherwise
    # a multi-paragraph summary renders as one wall of text.
    # §1 overview prose links items inline with markdown [anchor](deep_link)
    # (Pete directive 2026-09-08). The renderer converts those links to HTML
    # anchors deterministically, allowlisted to THIS run's item deep links —
    # no machine tokens like "(bos item 44)", no invented URLs.
    allowed_links = set()
    for item in items:
        dl = item.get("poliscopic_deep_link") or ""
        if dl:
            allowed_links.add(dl)
        else:
            body = item.get("body", "")
            meeting_id = item.get("meeting_id", "")
            num = item.get("agenda_item_number", "")
            if body and meeting_id and num:
                allowed_links.add(f"{POLISCOPIC_BASE}/meetings/{body}/{meeting_id}#item-{num}")

    summary_html = ""
    if summary:
        paras = [p.strip() for p in re.split(r"\n\s*\n", summary) if p.strip()]
        if len(paras) <= 1:
            paras = [p.strip() for p in summary.split("\n") if p.strip()]
        para_html = "\n".join(
            (
                f'  <p style="color: #444; margin: 0; font-size: 1.2em; line-height: 1.5;">{_summary_paragraph_html(p, allowed_links)}</p>'
                if i == 0
                else f'  <p style="color: #444; margin-top: 0.9em; font-size: 1.2em; line-height: 1.5;">{_summary_paragraph_html(p, allowed_links)}</p>'
            )
            for i, p in enumerate(paras)
        )
        summary_html = (
            f'<div style="background: #f5f5f5; padding: 1em; border-radius: 6px; margin-bottom: 1.5em;">\n'
            f'{para_html}\n'
            f'</div>'
        )

    # §3 data gaps are ADMIN content (daily ops digest, Brief 007), NOT
    # subscriber copy (Pete directive 2026-08-26). Never render them in the
    # newsletter. The digest (scripts/sync/sync_digest.py) reports gaps.
    data_gaps_html = ""

    # Both §1 and §3 render in the summary slot of the template
    summary_html = summary_html + data_gaps_html

    # Group items by jurisdiction, with headers
    def _group_by_jurisdiction(items: list) -> str:
        if not items:
            return '<p style="color: #888;">None this period.</p>'
        groups: dict[str, list] = {}
        for item in items:
            body = item.get("body", "").lower()
            jur = BODY_NAMES.get(body, "") or item.get("jurisdiction_name", "") or body.upper()
            groups.setdefault(jur, []).append(item)
        html_parts = []
        for jur_name in sorted(groups.keys()):
            html_parts.append(f'<h2 style="color: #444; font-size: 1.1em; margin-top: 1.5em; margin-bottom: 0.5em;">{jur_name}</h2>')
            for item in groups[jur_name]:
                html_parts.append(_item_html(item))
        return "\n".join(html_parts)

    past_html = _group_by_jurisdiction(past_items)
    upcoming_html = _group_by_jurisdiction(upcoming_items)

    # The send step (workflow-runner) personalizes this per recipient:
    # __UNSUBSCRIBE_URL__ is replaced with a signed one-click unsubscribe
    # link; __MANAGE_URL__ with a signed manage-preferences link.
    unsubscribe = ("You are receiving this email because you subscribed to the "
                  f"{topic_name} digest. "
                  "<a href=\"__UNSUBSCRIBE_URL__\" style=\"color:#bbb;\">Unsubscribe</a> "
                  "&middot; "
                  "<a href=\"__MANAGE_URL__\" style=\"color:#bbb;\">Manage preferences</a>")
    unsubscribe_fallback = (f"You are receiving this email because you subscribed to the "
                            f"{topic_name} digest. Manage or unsubscribe via the Poliscopic "
                            "newsletter page: https://poliscopic.com/newsletter")

    html = (
        template
        .replace("{{ TOPIC_NAME }}", topic_name)
        .replace("{{ DATE }}", date_str)
        .replace("{{ SUMMARY_HTML }}", summary_html)
        .replace("{{ PAST_ITEMS_HTML }}", past_html)
        .replace("{{ UPCOMING_ITEMS_HTML }}", upcoming_html)
    )
    # Footer placeholders are personalized by the send step; fall back to a
    # neutral footer when no personalization happens (preview / unit tests).
    if "__UNSUBSCRIBE_URL__" in unsubscribe or "{{ UNSUBSCRIBE_TEXT }}" in template:
        html = html.replace("{{ UNSUBSCRIBE_TEXT }}", unsubscribe)
    else:
        html = html.replace("{{ UNSUBSCRIBE_TEXT }}", unsubscribe_fallback)

    return html


def _merge_verified_items(verify_result: dict, enriched_items: list[dict]) -> list[dict]:
    """Merge verify-result items (with revised_summary) back into enriched items.

    The verify step returns items with only a subset of fields (body,
    agenda_item_number, title, summary, revised_summary, claims). This
    merges those back into the full enriched items so the renderer has
    all the metadata fields (meeting_id, meeting_title, meeting_date, etc.).
    """
    verify_items = verify_result.get("items", [])
    if not verify_items:
        return enriched_items

    # Build lookup keyed by (body, agenda_item_number)
    verify_lookup = {}
    for vi in verify_items:
        key = (str(vi.get("body", "")), str(vi.get("agenda_item_number", "")))
        verify_lookup[key] = vi

    merged = []
    for item in enriched_items:
        key = (str(item.get("body", "")), str(item.get("agenda_item_number", "")))
        if key in verify_lookup:
            vi = verify_lookup[key]
            # Attach the verified summary and claims
            if vi.get("revised_summary"):
                item["reason"] = vi["revised_summary"]
            elif vi.get("summary"):
                item["reason"] = vi["summary"]
            item["claims"] = vi.get("claims", [])
        merged.append(item)

    return merged


def render_from_verified(
    verified_dir: Path,
    topic_key: str,
    topic_display_name: str,
) -> Optional[str]:
    """Render a newsletter from the verified directory output.

    Args:
        verified_dir: Path to the run's verified/ directory
        topic_key: The topic key in the manifest (e.g., "transportation")
        topic_display_name: Human-readable topic name

    Returns:
        Tuple of (HTML string, subject line) ready to send via email, or None
        if no approved data found.
    """
    verify_result_path = verified_dir / "verify-result.json"
    if not verify_result_path.exists():
        return None

    verify_result = json.loads(verify_result_path.read_text())
    is_approved = verify_result.get("approved", verify_result.get("status") == "approved")
    if not is_approved:
        return None

    # §1 headline text (from verify passthrough of the editorial output)
    summary = verify_result.get("_summary", "")

    # §3 data gaps (from verify passthrough of the editorial output)
    data_gaps = verify_result.get("data_gaps", []) or []

    # Subject line (editorial subject, e.g. "Housing & Development Watch — …")
    subject = verify_result.get("subject", "")

    # Read enriched topic file for full item data
    items = []
    report_path = verified_dir / f"{topic_key}.json"
    if not report_path.exists():
        manifest_path = verified_dir / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            for key, path in manifest.items():
                p = Path(path)
                if p.exists():
                    report_path = p
                    break
    if report_path.exists():
        report = json.loads(report_path.read_text())
        enriched_items = report.get("items", [])
        # Merge verify-result (revised_summary) back into enriched items
        items = _merge_verified_items(verify_result, enriched_items)

    today = datetime.now().strftime("%B %d, %Y")
    html = render_newsletter(topic_display_name, today, items, summary=summary, data_gaps=data_gaps)
    if not subject:
        subject = f"{topic_display_name} — {today}"
    return html, subject
