from __future__ import annotations

"""Admin routes: dashboard, article CRUD, tag management, AI suggestions."""
import re, json, time
from datetime import datetime, timezone
from typing import Optional

from flask import (Blueprint, render_template, redirect, url_for, request,
                   flash, jsonify, current_app)
from flask_login import login_required, current_user
from sqlalchemy import select, desc, func, or_, and_
from sqlalchemy.orm import joinedload

from db.core import get_session
from db.newsroom import (
    Article, ArticleSource, Tag, article_tags, AdminUser, Notification,
    MediaImage, SkeetDraft, sync_article_fts, search_agenda_items,
    NewsletterSubscriber, NewsletterSubscription,
)


def create_notification(message: str, url: str = "", article_id: int | None = None):
    """Create an in-app admin notification."""
    session = get_session()
    notif = Notification(message=message, url=url, article_id=article_id)
    session.add(notif)
    session.commit()
    session.close()
from db.models import AgendaItem, Meeting
from db import get_session as get_db_session

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


_STOP_WORDS = {"a", "an", "the", "and", "or", "of", "for", "in", "to",
                   "with", "on", "at", "by", "is", "its", "are",
                   "was", "were", "be", "been", "being"}


def _slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    # Remove stop words
    words = [w for w in s.split() if w not in _STOP_WORDS]
    s = "-".join(words)
    s = re.sub(r"-+", "-", s)
    return s[:200]


def _unique_slug(text: str, exclude_id: int | None = None, date_prefix: str | None = None) -> str:
    """Generate a unique slug by prepending the date and appending -2, -3, etc. when conflicts exist.

    Args:
        text: Source text to slugify.
        exclude_id: Optional article ID to exclude from uniqueness check.
        date_prefix: Optional YYYY-MM-DD date to prepend. Defaults to today.
    """
    from db.newsroom import Article
    from datetime import date as _date
    base = _slugify(text)
    if not base:
        base = "untitled"
    prefix = date_prefix or str(_date.today())
    base = f"{prefix}-{base}"
    slug = base
    n = 2
    session = get_session()
    try:
        while True:
            row = session.execute(
                select(Article).where(Article.slug == slug)
            ).first()
            if row is None:
                return slug
            if exclude_id is not None and row[0].id == exclude_id:
                return slug
            slug = f"{base}-{n}"
            n += 1
    finally:
        session.close()


# ── Dashboard / Editorial Queue ──

@admin_bp.route("/")
@login_required
def dashboard() -> str:
    session = get_session()
    drafts_count = session.execute(
        select(func.count(Article.id)).where(Article.status == "draft")
    ).scalar() or 0
    published_count = session.execute(
        select(func.count(Article.id)).where(Article.status == "published")
    ).scalar() or 0
    archived_count = session.execute(
        select(func.count(Article.id)).where(Article.status == "archived")
    ).scalar() or 0
    featured_count = session.execute(
        select(func.count(Article.id)).where(Article.is_featured == True)
    ).scalar() or 0
    skeet_count = session.execute(
        select(func.count(SkeetDraft.id))
    ).scalar() or 0
    session.close()
    return render_template(
        "admin/dashboard.html",
        drafts_count=drafts_count, published_count=published_count,
        archived_count=archived_count, featured_count=featured_count,
        skeet_count=skeet_count,
    )


@admin_bp.route("/drafts")
@login_required
def drafts_list() -> str:
    session = get_session()
    drafts = session.execute(
        select(Article).where(Article.status == "draft")
        .order_by(desc(Article.created_at), desc(Article.priority))
    ).scalars().all()
    counts = _get_all_counts(session)
    session.close()
    return render_template("admin/drafts.html", drafts=drafts, counts=counts)


@admin_bp.route("/subscribers")
@login_required
def subscribers_list() -> str:
    """Newsletter subscribers: status counts, per-topic counts, and list."""
    session = get_session()
    subs = session.execute(
        select(NewsletterSubscriber)
        .options(joinedload(NewsletterSubscriber.subscriptions))
        .order_by(desc(NewsletterSubscriber.created_at))
    ).unique().scalars().all()

    rows = []
    for sub in subs:
        rows.append({
            "email": sub.email,
            "status": sub.status,
            "created_at": sub.created_at,
            "confirmed_at": sub.confirmed_at,
            "topics": sorted(x.topic for x in sub.subscriptions
                             if x.status == "active"),
            "pending_topics": sorted(x.topic for x in sub.subscriptions
                                     if x.status == "pending"),
        })

    by_status: dict = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    topic_counts: dict = {}
    for r in rows:
        if r["status"] != "active":
            continue
        for t in r["topics"]:
            topic_counts[t] = topic_counts.get(t, 0) + 1

    counts = _get_all_counts(session)
    session.close()
    from newsletter_svc import NEWSLETTER_TOPICS
    return render_template("admin/subscribers.html", rows=rows,
                           by_status=by_status, topic_counts=topic_counts,
                           topics=NEWSLETTER_TOPICS,
                           counts=counts)


@admin_bp.route("/published")
@login_required
def published_list() -> str:
    session = get_session()
    published = session.execute(
        select(Article).where(Article.status == "published")
        .order_by(desc(Article.published_at))
    ).scalars().all()
    counts = _get_all_counts(session)
    session.close()
    return render_template("admin/published.html", published=published, counts=counts)


@admin_bp.route("/archived")
@login_required
def archived_list() -> str:
    session = get_session()
    archived = session.execute(
        select(Article).where(Article.status == "archived")
        .order_by(desc(Article.archived_at))
    ).scalars().all()
    counts = _get_all_counts(session)
    session.close()
    return render_template("admin/archived.html", archived=archived, counts=counts)


@admin_bp.route("/featured")
@login_required
def featured_list() -> str:
    session = get_session()
    featured = session.execute(
        select(Article).where(Article.is_featured == True)
        .order_by(desc(Article.updated_at))
    ).scalars().all()
    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    counts = _get_all_counts(session)
    session.close()
    return render_template("admin/featured.html",
        featured=featured, tags=tags, counts=counts, now=datetime.now(timezone.utc))


@admin_bp.route("/bluesky")
@login_required
def bluesky_list() -> str:
    from sqlalchemy.orm import joinedload
    session = get_session()
    skeet_drafts = session.execute(
        select(SkeetDraft)
        .options(joinedload(SkeetDraft.article))
        .order_by(desc(SkeetDraft.created_at))
        .limit(100)
    ).unique().scalars().all()
    for d in skeet_drafts:
        _ = d.article
    counts = _get_all_counts(session)
    session.close()
    return render_template("admin/bluesky.html",
        skeet_drafts=list(skeet_drafts), counts=counts)


def _get_all_counts(session: Session) -> dict:
    """Return a dict of counts for all admin sections."""
    return {
        "drafts": session.execute(select(func.count(Article.id)).where(Article.status == "draft")).scalar() or 0,
        "published": session.execute(select(func.count(Article.id)).where(Article.status == "published")).scalar() or 0,
        "archived": session.execute(select(func.count(Article.id)).where(Article.status == "archived")).scalar() or 0,
        "featured": session.execute(select(func.count(Article.id)).where(Article.is_featured == True)).scalar() or 0,
        "bluesky": session.execute(select(func.count(SkeetDraft.id))).scalar() or 0,
        "subscribers": session.execute(
            select(func.count(NewsletterSubscriber.id))).scalar() or 0,
    }


# ── Narrative angle templates for each topic ──
_ANGLE_TEMPLATES = {
    "housing": (
        "Housing supply remains a flashpoint across the Valley. This agenda item could signal "
        "new units coming online — or a fight over density, affordability, and neighborhood character."
    ),
    "zoning": (
        "Zoning changes are where land-use battles are won and lost. This item involves "
        "rewriting the rules for a specific piece of property — who benefits and who pushes back?"
    ),
    "data-centers": (
        "Data centers are reshaping Maricopa County's industrial landscape, bringing "
        "massive investment but also concerns about water use, power demand, and job quality."
    ),
    "enforcement": (
        "Code enforcement actions reveal where cities are drawing lines — and who gets "
        "cited. This item suggests regulatory friction worth watching."
    ),
    "health": (
        "Public health decisions at the county level have downstream effects on every "
        "resident. This item touches on health policy, access, or emerging risks."
    ),
    "environment": (
        "Environmental policy in the desert is never neutral — water, solar, conservation, "
        "and contamination battles all have high stakes. This item is worth a closer look."
    ),
    "development": (
        "Development proposals tell you where a city is headed. This item involves "
        "new construction or commercial plans that could change the character of an area."
    ),
    "transportation": (
        "Transportation decisions shape commute times, safety, and economic access for "
        "years. This agenda item has real implications for how people move through the county."
    ),
    "economy": (
        "Economic development incentives are essentially bets with taxpayer money. This "
        "item involves dollars, jobs, and the question of whether the promised returns materialize."
    ),
    "water": (
        "In the arid Southwest, water is the underlying story beneath almost every "
        "development decision. This item engages water rights, conservation, or infrastructure."
    ),
}


def _generate_pitch(suggestion: dict) -> dict:
    """Generate a narrative pitch, score by impact."""
    import urllib.parse
    import re as _re

    topic = suggestion["topic"]
    title = suggestion["title"] or ""
    body = suggestion["body"] or ""
    text = suggestion["text"] or ""
    matched = suggestion["matched_keywords"] or []
    meeting_date = suggestion["meeting_date"] or ""

    # ── Impact scoring ──
    score = 0
    text_lower = (title + " " + text).lower()
    dollar_matches = _re.findall(r'\$[\d,]+', text_lower)

    for d in dollar_matches:
        val = _re.sub(r'[\$,]', '', d)
        try:
            n = int(val)
            if n >= 10000000: score += 50
            elif n >= 1000000: score += 20
            elif n >= 100000: score += 10
            elif n >= 10000: score += 5
        except ValueError: pass

    signal_scores = {"bonds-finance": 40, "policy-planning": 35, "infrastructure": 25,
                     "homelessness": 30, "development-pipeline": 15, "annexation": 20,
                     "enforcement-signals": 10}
    score += signal_scores.get(topic, 0)

    if body == "bos": score += 10

    bundle_kws = ["homeless", "shelter", "amendment", "iga with"]
    for kw in bundle_kws:
        if text_lower.count(kw) > 3:
            score += 15
            break

    suggestion["score"] = score

    # ── Land-use signal scoring ──
    # Detect specific development details: lot counts, parking spaces, square footage,
    # named districts, addresses, specific dollar figures (non-$-prefixed numbers).
    land_use_signals = _re.findall(r'(\d+)\s*(spaces?|parking|units?|lots?|acres?|sf|sq\s*ft|stories?)', text_lower)
    for num, unit in land_use_signals:
        n = int(num)
        if unit.startswith(('unit', 'lot', 'space')) and n >= 5:
            score += 8
        elif unit.startswith(('parking')) and n >= 20:
            score += 10
        elif unit.startswith(('acre')) and n >= 2:
            score += 12
        elif unit.startswith(('stor')) and n >= 3:
            score += 8
    # City center / downtown / specific district references
    district_refs = _re.findall(r'(city center|downtown|historic|overlay|specific plan)', text_lower)
    score += len(district_refs) * 5
    # Specific addresses or parcel numbers
    if _re.search(r'\d+\s+[A-Z][a-z]*(?:\s+[A-Z][a-z]*)*\s+(?:Street|Avenue|Drive|Lane|Road|Way|Boulevard|Circle|Court)\b', text):
        score += 8
    # Multiple intersections / area descriptions suggesting larger project
    if text_lower.count(' and ') >= 3 and any(w in text_lower for w in ['street', 'avenue', 'road', 'between', 'north of', 'south of']):
        score += 10

    suggestion["score"] = score

    # ── Context ──
    angle = _ANGLE_TEMPLATES.get(topic, "This agenda item could have significant local impact.")

    jurisdiction_map = {"bos": "Maricopa County", "pz": "Maricopa County PZ",
        "adj": "Maricopa County ADJ", "mesa-city-council": "Mesa", "mesa-pz": "Mesa PZ",
        "chandler-cc": "Chandler", "chandler-pz": "Chandler PZ",
        "tempe-cc": "Tempe", "scottsdale-cc": "Scottsdale", "gilbert-tc": "Gilbert",
        "mc-": "Maricopa County"}
    location = ""
    for prefix, name in jurisdiction_map.items():
        if body.startswith(prefix): location = name; break
    if not location: location = body

    topic_label = topic.replace('-', ' ').title()
    first_kw = matched[0] if matched else ''

    # Build search terms: use item title for better results when keywords are too generic
    if first_kw and (first_kw.lower() == topic.replace('-', ' ').lower() or len(title) < 25):
        # Generic keyword — search with the actual item title
        search_query = title[:80] if title else f"{location} {topic_label} {first_kw}"
    else:
        search_query = f"{location} {topic_label} {first_kw}"
    search_terms = f"{location} {search_query}"
    ddg_url = f"https://duckduckgo.com/?q={urllib.parse.quote(search_terms)}&ia=news"
    azcentral_url = f"https://www.azcentral.com/search/?q={urllib.parse.quote(search_terms)}"
    newslookup_url = f"https://newslookup.com/results?q={urllib.parse.quote(search_terms)}"

    # Build headline: avoid "Zoning: Zoning at tempe-drc" redundancy
    if first_kw.lower() == topic.replace('-', ' ').lower():
        # Keyword is same as topic — use the actual item title
        headline = title[:80] if title else f"{topic_label} Item at {location}"
    elif title and len(title) < 80 and first_kw.lower() in title.lower():
        # Keyword is contained in title — use title, it's more descriptive
        headline = title[:80]
    else:
        headline = f"{topic_label}: {first_kw.title() if first_kw else 'Item'} at {location}"

    # ── Narrative pitch HTML ──
    parts = [f'<span class="badge bg-{"danger" if score >= 30 else "warning" if score >= 15 else "secondary"}">Impact {score}</span>']
    for kw in matched[:3]:
        parts.append(f'<span class="badge bg-info text-dark me-1">{kw}</span>')
    parts.append('<br>')
    # Enrich the angle with item specifics when available
    _specifics = []
    if title and len(title) > 20:
        _specifics.append(title[:120])
    strip_sigs = _re.findall(r'(\d+)\s*(spaces?|parking|units?|acres?|dwelling)', text_lower)
    for num, unit in strip_sigs[:2]:
        _specifics.append(f'{num} {unit}')
    angle_extra = ' — ' + '; '.join(_specifics[:2]) if _specifics else ''
    parts.append(f'<strong>Why it matters:</strong> {angle}{angle_extra}')
    if meeting_date: parts.append(f'<strong>When:</strong> {meeting_date}')
    parts.append(f'<strong>Where:</strong> {location}')
    if title: parts.append(f'<strong>Item:</strong> &ldquo;{title}&rdquo;')
    parts.append(f'<strong>Suggested headline:</strong> {headline}')
    parts.append(f'<a href="{ddg_url}" target="_blank">DuckDuckGo News</a> | <a href="{newslookup_url}" target="_blank">NewsLookup</a> | <a href="{azcentral_url}" target="_blank">AZ Central</a>')

    suggestion["pitch"] = "<br>".join(parts)
    suggestion["headline"] = headline
    suggestion["location"] = location
    suggestion["angle"] = angle
    suggestion["ddg_url"] = ddg_url
    suggestion["azcentral_url"] = azcentral_url
    suggestion["newslookup_url"] = newslookup_url

    editorial_notes = []
    if score >= 30:
        editorial_notes.append("High-impact item — consider front-page feature.")
    if topic in ("bonds-finance", "policy-planning"):
        editorial_notes.append("Policy/finance story — may benefit from context about past actions.")
    if dollar_matches:
        editorial_notes.append(f"Dollar figures: {', '.join(dollar_matches[:3])}. Verify against meeting docs.")
    suggestion["editorial_notes"] = editorial_notes

    return suggestion


@admin_bp.route("/suggestions")
@login_required
def suggestions() -> str:
    """Scan agenda items for newsworthy topics and return AI-generated pitch suggestions."""
    session = get_db_session()

    jurisdiction_filter = request.args.get("jurisdiction", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")

    # Exclude dismissed suggestions
    from db.newsroom import DismissedSuggestion
    dismissed = set()
    dismissed_rows = session.execute(
        select(DismissedSuggestion)
    ).scalars().all()
    for ds in dismissed_rows:
        dismissed.add((ds.body, ds.meeting_id, ds.agenda_item_number))

    # Build jurisdiction map for filtering
    _JUR_BODY_PREFIXES = {
        "maricopa-county": "bos",
        "tempe": "tempe",
        "mesa": "mesa",
        "chandler": "chandler",
        "gilbert": "gilbert",
        "scottsdale": "scottsdale",
    }

    # Define interesting topics with keyword patterns
    topics = [
        ("housing", ["housing", "apartment", "residential", "multi-family",
                      "affordable", "townhome", "subdivision", "dwelling unit"]),
        ("zoning", ["rezon", "zone change", "zoning", "general plan amendment",
                     "land use", "zoning ordinance", "zoning code"]),
        ("data-centers", ["data center", "server farm", "hyperscale",
                          "data centre", "computing facility"]),
        ("enforcement", ["enforcement", "citation", "violation", "penalty",
                         "fine", "compliance", "cease", "desist", "revocation"]),
        ("health", ["health", "hospital", "clinic", "medical", "disease",
                    "pandemic", "vaccine", "opioid"]),
        ("environment", ["environmental", "sustainability", "solar", "renewable",
                         "emission", "climate", "conservation", "contamination"]),
        ("development", ["development", "construction", "building", "commercial",
                         "mixed-use", "retail", "office park", "industrial"]),
        ("transportation", ["transportation", "road", "highway", "transit",
                            "light rail", "bike lane", "pedestrian", "traffic"]),
        ("economy", ["economic development", "incentive", "tax", "job creation",
                     "workforce", "business park", "enterprise zone"]),
        ("water", ["water", "drought", "groundwater", "reclaimed", "wastewater",
                   "water rights", "aquifer", "conservation"]),
    ]

    suggestions = []
    seen_combos = set()

    _JUR_PREFIX = _JUR_BODY_PREFIXES.get(jurisdiction_filter, "")

    for topic_slug, keywords in topics:
        like_clauses = [AgendaItem.agenda_item_title.ilike(f"%{kw}%")
                        for kw in keywords]
        like_clauses += [AgendaItem.agenda_item_text.ilike(f"%{kw}%")
                         for kw in keywords]

        # Build filters: keywords OR'd + optional jurisdiction AND
        _filters = [or_(*like_clauses)]
        if _JUR_PREFIX:
            _filters.append(Meeting.body.like(f"{_JUR_PREFIX}%"))

        # Date range filter (all dates now normalized to YYYY-MM-DD)
        if start_date:
            _filters.append(Meeting.meeting_date >= start_date)
        if end_date:
            _filters.append(Meeting.meeting_date <= end_date)

        items = session.execute(
            select(AgendaItem, Meeting.meeting_date, Meeting.body)
            .join(Meeting, and_(
                Meeting.id == AgendaItem.meeting_db_id,
                Meeting.body == AgendaItem.body,
            ))
            .where(and_(*_filters))
            .where(Meeting.sync_status == "complete")
            .order_by(desc(Meeting.meeting_date))
            .limit(30)
        ).all()

        for (item, meeting_date, body), _ in [(r, 0) for r in items]:

            title = (item.agenda_item_title or "")[:120]
            text = (item.agenda_item_text or "")[:300]
            key = (item.meeting_id, item.agenda_item_number)
            if key in seen_combos:
                continue
            if key in dismissed:
                continue
            seen_combos.add(key)

            # Gather matching keywords for the pitch
            matched = [kw for kw in keywords
                       if kw in title.lower() or kw in text.lower()]

            suggestion = {
                "topic": topic_slug,
                "agenda_item_id": item.id,
                "body": body,
                "meeting_id": item.meeting_id,
                "meeting_date": meeting_date,
                "agenda_item_number": item.agenda_item_number,
                "title": title,
                "text": text,
                "matched_keywords": matched[:5],
                "source_url": item.source_url,
            }
            suggestions.append(_generate_pitch(suggestion))

    # ── High-significance signal search ──
    # Prioritize items the keyword approach misses: bonds, comp plans, large-dollar,
    # irrigation districts, homelessness service bundles, and policy frameworks.
    _SIGNAL_PATTERNS = {
        "bonds-finance": ["bond", "revenue", "financing", "appropriation",
                          "not to exceed $", "allocation", "budget",
                          "multifamily housing revenue"],
        "policy-planning": ["comprehensive plan", "framework 2040", "framework",
                            "general plan", "annual action plan",
                            "master plan", "area plan"],
        "infrastructure": ["irrigation water delivery", "water delivery district",
                           "road abandonment", "road file",
                           "hearing for the proposed", "setting of hearing"],
        "homelessness": ["homeless", "shelter", "rapid rehousing",
                         "emergency shelter", "homelessness outreach"],
        "development-pipeline": ["preliminary development plan", "preliminary plat",
                                 "subdivision", "planned area development",
                                 "development plan review"],
        "annexation": ["annex", "pre-annexation", "county island"],
        "enforcement-signals": ["litigation", "violation", "code", "citation",
                                "penalty", "cease", "desist"],
    }

    for signal_slug, signal_kws in _SIGNAL_PATTERNS.items():
        signal_likes = [AgendaItem.agenda_item_title.ilike(f"%{kw}%") for kw in signal_kws]
        signal_likes += [AgendaItem.agenda_item_text.ilike(f"%{kw}%") for kw in signal_kws]

        _signal_filter = [or_(*signal_likes)]
        if _JUR_PREFIX:
            _signal_filter.append(Meeting.body.like(f"{_JUR_PREFIX}%"))
        if start_date:
            _signal_filter.append(Meeting.meeting_date >= start_date)
        if end_date:
            _signal_filter.append(Meeting.meeting_date <= end_date)

        signal_items = session.execute(
            select(AgendaItem, Meeting.meeting_date, Meeting.body)
            .join(Meeting, and_(
                Meeting.id == AgendaItem.meeting_db_id,
                Meeting.body == AgendaItem.body,
            ))
            .where(and_(*_signal_filter))
            .where(Meeting.sync_status == "complete")
            .order_by(desc(Meeting.meeting_date))
            .limit(20)
        ).all()

        session_count = {}
        for (item, meeting_date, body), _ in [(r, 0) for r in signal_items]:
            key = (item.meeting_id, item.agenda_item_number)
            if key in seen_combos:
                continue
            if key in dismissed:
                continue
            seen_combos.add(key)

            title = (item.agenda_item_title or "")[:120]
            # Give signal items a higher weight by matching their keywords
            matched = [kw for kw in signal_kws if kw in title.lower() or
                       (item.agenda_item_text or "").lower().count(kw) > 0]

            sug = {
                "topic": signal_slug,
                "agenda_item_id": item.id,
                "body": body,
                "meeting_id": item.meeting_id,
                "meeting_date": meeting_date,
                "agenda_item_number": item.agenda_item_number,
                "title": title,
                "text": (item.agenda_item_text or "")[:300],
                "matched_keywords": matched[:5],
                "source_url": item.source_url,
            }
            suggestions.append(_generate_pitch(sug))

    # ── Explicit high-value items that signal patterns may miss ──
    _HIGH_VALUE_SEARCHES = [
        ("bonds-finance", ["bond", "multifamily housing revenue", "not to exceed"]),
        ("policy-planning", ["comprehensive plan", "framework 2040", "annual action plan"]),
        ("infrastructure", ["irrigation water delivery", "hearing for the proposed",
                           "road file", "palomino acres"]),
    ]
    for hv_topic, hv_kws in _HIGH_VALUE_SEARCHES:
        hv_likes = [AgendaItem.agenda_item_title.ilike(f"%{kw}%") for kw in hv_kws]
        hv_filter = [or_(*hv_likes)]
        if _JUR_PREFIX:
            hv_filter.append(Meeting.body.like(f"{_JUR_PREFIX}%"))
        if start_date:
            hv_filter.append(Meeting.meeting_date >= start_date)
        if end_date:
            hv_filter.append(Meeting.meeting_date <= end_date)

        try:
            hv_items = session.execute(
                select(AgendaItem, Meeting.meeting_date, Meeting.body)
                .join(Meeting, and_(
                    Meeting.id == AgendaItem.meeting_db_id,
                    Meeting.body == AgendaItem.body,
                ))
                .where(and_(*hv_filter))
                .where(Meeting.sync_status == "complete")
                .order_by(desc(Meeting.meeting_date))
                .limit(10)
            ).all()
            for (item, meeting_date, body), _ in [(r, 0) for r in hv_items]:
                key = (item.meeting_id, item.agenda_item_number)
                if key in seen_combos:
                    continue
                seen_combos.add(key)
                title = (item.agenda_item_title or "")[:120]
                matched = [kw for kw in hv_kws if kw in title.lower() or
                           (item.agenda_item_text or "").lower().count(kw) > 0]
                sug = {
                    "topic": hv_topic, "agenda_item_id": item.id,
                    "body": body, "meeting_id": item.meeting_id,
                    "meeting_date": meeting_date,
                    "agenda_item_number": item.agenda_item_number,
                    "title": title, "text": (item.agenda_item_text or "")[:300],
                    "matched_keywords": matched[:5],
                    "source_url": item.source_url,
                }
                suggestions.append(_generate_pitch(sug))
        except Exception as e:
            log.warning("High-value search failed for %s: %s", hv_topic, e)

    session.close()

    # Sort: signal topics first, then keyword-matched, by date
    def _sort_key(s) -> tuple:
        priority = 0 if s["topic"] in _SIGNAL_PATTERNS else 1
        return (priority, s.get("meeting_date", "") or "")
    suggestions.sort(key=_sort_key, reverse=True)

    # Group by topic for display (no jurisdiction cap — let a thousand flowers bloom)
    grouped = {}
    for s in suggestions:
        grouped.setdefault(s["topic"], []).append(s)

    # Remove jurisdiction fairness cap — let a thousand flowers bloom
    tags = get_session().execute(select(Tag).order_by(Tag.name)).scalars().all()
    return render_template(
        "admin/suggestions.html", grouped=grouped, tags=tags,
        filter_jurisdiction=jurisdiction_filter,
        filter_start_date=start_date,
        filter_end_date=end_date,
    )




def _post_article_to_bluesky(article: Article) -> dict:
    """Post an article to Bluesky if it's published and has a slug."""
    if article.status != "published" or not article.slug:
        return
    from social import post_to_bluesky, _fetch_image_bytes
    # Determine the full URL
    from flask import url_for
    base_url = "https://poliscopic.com"
    article_url = f"{base_url}/articles/{article.slug}"
    image_bytes = _fetch_image_bytes(article.featured_image) if article.featured_image else None
    post_to_bluesky(article.title, article.summary, article_url, image_bytes=image_bytes)

# ── Article CRUD ──

@admin_bp.route("/articles/new", methods=["GET", "POST"])
@login_required
def article_new() -> str:
    """Create a new article, optionally pre-populated from a suggestion."""
    session = get_session()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("Title is required.", "error")
            return redirect(url_for("admin.article_new"))

        article = Article(
            title=title,
            slug=_unique_slug(title),
            body=request.form.get("body", "").strip(),
            featured_image=request.form.get("featured_image", "").strip(),
            image_credit=request.form.get("image_credit", "").strip() or None,
            status=request.form.get("status", "draft"),
            is_featured=request.form.get("is_featured") == "on",
            author_id=current_user.id,
        )

        # Publish action overrides status dropdown
        now = datetime.now(timezone.utc)
        if request.form.get("action") == "publish":
            article.status = "published"
            article.published_at = now
        if article.status == "archived":
            article.archived_at = now

        session.add(article)
        session.flush()

        # Bluesky posting is handled by scripts/bluesky_sync.py after production sync

        # Attach tags
        tag_ids = request.form.getlist("tags")
        for tid in tag_ids:
            tag = session.get(Tag, int(tid))
            if tag:
                article.tags.append(tag)

        # Attach sources — only URL + title (text) needed
        source_urls = request.form.getlist("source_url[]")
        source_titles = request.form.getlist("source_title[]")
        for i in range(len(source_urls)):
            url = source_urls[i].strip()
            if url:
                src = ArticleSource(
                    article_id=article.id,
                    source_url=url,
                    item_title=source_titles[i].strip() if i < len(source_titles) else "",
                )
                session.add(src)

        session.commit()
        sync_article_fts(article.id)
        session.close()
        flash("Article created.", "success")
        if request.form.get("action") == "publish":
            return redirect(url_for("admin.dashboard"))
        return redirect(url_for("admin.article_edit", article_id=article.id))

    # GET — pre-fill from suggestion query params
    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    preselected_tags = []
    if request.args.get("topic"):
        tag = session.execute(
            select(Tag).where(Tag.slug == request.args.get("topic"))
        ).scalar_one_or_none()
        if tag:
            preselected_tags.append(str(tag.id))

    session.close()
    return render_template(
        "admin/article_form.html",
        article=None, tags=tags, preselected_tags=preselected_tags,
        source_url=request.args.get("source_url", ""),
        source_title=request.args.get("source_title", ""),
    )


@admin_bp.route("/articles/<int:article_id>/edit", methods=["GET", "POST"])
@login_required
def article_edit(article_id) -> str:
    session = get_session()
    article = session.get(Article, article_id)
    if not article:
        session.close()
        flash("Article not found.", "error")
        return redirect(url_for("admin.dashboard"))

    if request.method == "POST":
        article.title = request.form.get("title", article.title).strip()
        article.slug = _unique_slug(article.title, exclude_id=article.id)
        article.summary = request.form.get("summary", "").strip()
        article.body = request.form.get("body", "").strip()
        article.featured_image = request.form.get("featured_image", "").strip()
        article.image_credit = request.form.get("image_credit", "").strip() or None
        article.is_featured = request.form.get("is_featured") == "on"
        article.status = request.form.get("status", article.status)
        article.updated_at = datetime.now(timezone.utc)

        now = datetime.now(timezone.utc)
        if article.status == "published" and not article.published_at:
            article.published_at = now
        if article.status == "archived" and not article.archived_at:
            article.archived_at = now

        # Update tags
        tag_ids = [int(t) for t in request.form.getlist("tags") if t]
        article.tags = [
            session.get(Tag, tid) for tid in tag_ids
            if session.get(Tag, tid)
        ]

        # Update sources — replace all (only URL + title)
        session.query(ArticleSource).filter_by(article_id=article.id).delete()
        source_urls = request.form.getlist("source_url[]")
        source_titles = request.form.getlist("source_title[]")
        for i in range(len(source_urls)):
            url = source_urls[i].strip()
            if url:
                src = ArticleSource(
                    article_id=article.id,
                    source_url=url,
                    item_title=source_titles[i].strip() if i < len(source_titles) else "",
                )
                session.add(src)

        # Publish action overrides status dropdown
        if request.form.get("action") == "publish":
            article.status = "published"
            if not article.published_at:
                article.published_at = datetime.now(timezone.utc)

        session.commit()
        sync_article_fts(article.id)
        # Bluesky posting is handled by scripts/bluesky_sync.py after production sync
        flash("Article updated.", "success")
        if request.form.get("action") == "publish":
            return redirect(url_for("admin.dashboard"))
        return redirect(url_for("admin.article_edit", article_id=article.id))

    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    existing_sources = session.execute(
        select(ArticleSource).where(ArticleSource.article_id == article.id)
    ).scalars().all()
    session.close()
    return render_template(
        "admin/article_form.html",
        article=article, tags=tags,
        preselected_tags=[str(t.id) for t in article.tags],
        existing_sources=existing_sources,
    )


@admin_bp.route("/articles/<int:article_id>/delete", methods=["POST"])
@login_required
def article_delete(article_id) -> str:
    session = get_session()
    article = session.get(Article, article_id)
    if article:
        session.delete(article)
        session.commit()
    session.close()
    flash("Article deleted.", "success")
    return redirect(url_for("admin.drafts_list"))


@admin_bp.route("/articles/<int:article_id>/promote", methods=["POST"])
@login_required
def article_promote(article_id) -> str:
    session = get_session()
    article = session.get(Article, article_id)
    if article:
        article.status = "published"
        article.published_at = datetime.now(timezone.utc)
        session.commit()
        sync_article_fts(article.id)
    session.close()
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/articles/<int:article_id>/archive", methods=["POST"])
@login_required
def article_archive(article_id) -> str:
    session = get_session()
    article = session.get(Article, article_id)
    if article:
        article.status = "archived"
        article.archived_at = datetime.now(timezone.utc)
        session.commit()
    session.close()
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/articles/<int:article_id>/priority", methods=["POST"])
@login_required
def article_set_priority(article_id) -> str:
    """Set article priority (higher = higher in draft list)."""
    session = get_session()
    article = session.get(Article, article_id)
    if article:
        try:
            priority = int(request.form.get("priority", 0))
            article.priority = max(0, min(999, priority))
            session.commit()
        except (ValueError, TypeError):
            pass
    session.close()
    return redirect(url_for("admin.dashboard"))




@admin_bp.route("/articles/search", methods=["GET"])
def article_search() -> str:
    """Search articles by keyword (JSON). Used by the featured widget.
    No @login_required so the fetch works without redirect — the route
    returns empty results for unauthenticated requests."""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return {"results": []}
    from flask_login import current_user
    if not current_user.is_authenticated:
        return {"results": [], "error": "not authenticated"}
    session = get_session()
    articles = session.execute(
        select(Article)
        .where(
            or_(
                Article.title.ilike(f"%{q}%"),
                Article.summary.ilike(f"%{q}%"),
            ),
            Article.status != "archived",
        )
        .order_by(Article.updated_at.desc())
        .limit(20)
    ).scalars().all()
    results = []
    for a in articles:
        results.append({
            "id": a.id,
            "title": a.title[:80],
            "status": a.status,
            "is_featured": a.is_featured,
            "created": a.created_at.strftime("%b %d") if a.created_at else "",
        })
    session.close()
    return {"results": results}


@admin_bp.route("/articles/<int:article_id>/feature", methods=["POST"])
@login_required
def article_feature_toggle(article_id) -> str:
    """Toggle featured status. Enforces limit of 3 featured articles."""
    session = get_session()
    article = session.get(Article, article_id)
    if not article:
        session.close()
        flash("Article not found.", "error")
        return redirect(url_for("admin.dashboard"))

    if article.is_featured:
        # Unfeature
        article.is_featured = False
        session.commit()
        flash(f"Removed from featured: {article.title[:60]}", "success")
    else:
        # Check limit
        featured_count = session.execute(
            select(Article).where(Article.is_featured == True)
        ).scalars().all()
        if len(featured_count) >= 3:
            # Remove the oldest featured
            oldest = sorted(featured_count, key=lambda x: x.updated_at or x.created_at)[0]
            oldest.is_featured = False
            flash(f"Replaced: {oldest.title[:60]} removed from featured.", "info")
        article.is_featured = True
        flash(f"Featured: {article.title[:60]}", "success")
    
    session.commit()
    session.close()
    redirect_to = request.form.get("redirect", "")
    if redirect_to:
        return redirect(redirect_to)
    return redirect(url_for("admin.dashboard"))
@admin_bp.route("/articles/reorder", methods=["POST"])
@login_required
def articles_reorder() -> str:
    """Batch reorder drafts — accepts JSON {order: [id, id, ...]}."""
    data = request.get_json(silent=True)
    if not data or "order" not in data:
        return jsonify({"ok": False}), 400
    session = get_session()
    order = data["order"]
    for pos, article_id in enumerate(order):
        article = session.get(Article, int(article_id))
        if article and article.status == "draft":
            article.priority = len(order) - pos
    session.commit()
    session.close()
    return jsonify({"ok": True})


# ── Suggestion Dismissal ──

@admin_bp.route("/suggestions/dismiss", methods=["POST"])
@login_required
def dismiss_suggestion() -> str:
    """Dismiss a story suggestion so it doesn't reappear."""
    from db.newsroom import DismissedSuggestion
    session = get_session()
    ds = DismissedSuggestion(
        body=request.form.get("body", ""),
        meeting_id=request.form.get("meeting_id", ""),
        agenda_item_number=request.form.get("agenda_item_number", ""),
        reason=request.form.get("reason", "dismissed"),
        dismissed_by=current_user.id,
    )
    session.add(ds)
    session.commit()
    session.close()
    flash("Suggestion dismissed.", "success")
    return redirect(url_for("admin.suggestions"))


@admin_bp.route("/suggestions/draft", methods=["POST"])
@login_required
def draft_from_suggestion() -> str:
    """Generate a full draft article from a suggestion and redirect to edit."""
    session = get_session()
    body_code = request.form.get("body", "")
    meeting_id = request.form.get("meeting_id", "")
    item_number = request.form.get("agenda_item_number", "")
    topic = request.form.get("topic", "")
    source_url = request.form.get("source_url", "")

    # Look up the agenda item for full text
    from db.models import AgendaItem, Meeting
    from sqlalchemy import and_
    item = session.execute(
        select(AgendaItem)
        .join(Meeting, and_(Meeting.id == AgendaItem.meeting_db_id,
                           Meeting.body == AgendaItem.body))
        .where(AgendaItem.body == body_code)
        .where(AgendaItem.meeting_id == meeting_id)
        .where(AgendaItem.agenda_item_number == item_number)
        .limit(1)
    ).scalar_one_or_none()

    title = (item.agenda_item_title or "Untitled")[:120] if item else "Untitled"
    item_text = (item.agenda_item_text or "")[:2000] if item else ""

    # Look up meeting date for slug prefix
    meeting_date_prefix = ""
    if item:
        m = session.execute(
            select(Meeting.meeting_date)
            .where(Meeting.body == body_code, Meeting.meeting_id == meeting_id)
        ).scalar_one_or_none()
        if m:
            meeting_date_prefix = m

    # Generate draft body from the agenda item text
    from datetime import date
    draft_body = f"**Background**\n\nThe {body_code} considered this item recently.\n\n**Details**\n\n{item_text}\n\n**What's Next**\n\nThis item was on the agenda. Check the meeting page for the outcome."
    draft_summary = title[:200]

    # Create the draft article
    article = Article(
        title=title,
        slug=_unique_slug(title, date_prefix=meeting_date_prefix or None),
        summary=draft_summary,
        body=draft_body,
        status="draft",
        author_id=current_user.id,
    )
    session.add(article)
    session.flush()

    # Attach source
    if source_url:
        src = ArticleSource(
            article_id=article.id,
            body=body_code,
            meeting_id=meeting_id,
            agenda_item_number=item_number,
            source_url=source_url,
            item_title=title,
        )
        session.add(src)

    session.commit()
    sync_article_fts(article.id)
    session.close()

    flash("Draft created from suggestion. Edit, add sources, and publish.", "success")
    return redirect(url_for("admin.article_edit", article_id=article.id))


@admin_bp.route("/suggestions/split", methods=["POST"])
@login_required
def split_suggestion() -> str:
    """Create a parent article placeholder for a multi-part story."""
    from db.newsroom import DismissedSuggestion
    session = get_session()
    parent = Article(
        title=request.form.get("title", "Multi-part story").strip(),
        slug=_unique_slug(request.form.get("title", "multi-part-story")),
        summary="A series of related stories on this topic.",
        status="draft",
        author_id=current_user.id,
    )
    session.add(parent)
    session.commit()
    # Mark the original suggestion as dismissed with a split reason
    ds = DismissedSuggestion(
        body=request.form.get("body", ""),
        meeting_id=request.form.get("meeting_id", ""),
        agenda_item_number=request.form.get("agenda_item_number", ""),
        reason="split",
        dismissed_by=current_user.id,
    )
    session.add(ds)
    session.commit()
    session.close()
    flash(f"Parent article created. Sub-articles can be added.", "success")
    return redirect(url_for("admin.article_edit", article_id=parent.id))


# ── Tag Management ──

@admin_bp.route("/tags", methods=["GET", "POST"])
@login_required
def manage_tags() -> str:
    session = get_session()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            if name:
                existing = session.execute(
                    select(Tag).where(Tag.name == name)
                ).scalar_one_or_none()
                if not existing:
                    tag = Tag(name=name, slug=_slugify(name),
                              description=request.form.get("description", ""))
                    session.add(tag)
                    session.commit()
                    flash(f"Tag '{name}' added.", "success")
        elif action == "delete":
            tag_id = request.form.get("tag_id")
            if tag_id:
                tag = session.get(Tag, int(tag_id))
                if tag:
                    session.delete(tag)
                    session.commit()
                    flash(f"Tag deleted.", "success")
        elif action == "rename":
            tag_id = request.form.get("tag_id")
            name = request.form.get("name", "").strip()
            if tag_id and name:
                tag = session.get(Tag, int(tag_id))
                if tag:
                    tag.name = name
                    tag.slug = _slugify(name)
                    session.commit()
                    flash(f"Tag renamed.", "success")

    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    # Get article counts per tag
    from sqlalchemy import func
    from db.newsroom import article_tags
    counts = {}
    rows = session.execute(
        select(article_tags.c.tag_id, func.count(article_tags.c.article_id))
        .group_by(article_tags.c.tag_id)
    ).all()
    for tag_id, cnt in rows:
        counts[tag_id] = cnt
    session.close()
    return render_template("admin/tags.html", tags=tags, tag_counts=counts)


@admin_bp.route("/notifications")
@login_required
def notifications() -> str:
    """List all admin notifications."""
    session = get_session()
    notifs = session.execute(
        select(Notification).order_by(desc(Notification.created_at)).limit(100)
    ).scalars().all()
    session.close()
    return render_template("admin/notifications.html", notifications=notifs)


@admin_bp.route("/notifications/count")
@login_required
def notifications_count() -> str:
    """Return unread notification count as JSON."""
    session = get_session()
    count = session.execute(
        select(func.count(Notification.id)).where(Notification.is_read == False)
    ).scalar()
    session.close()
    from flask import jsonify
    return jsonify({"count": count})


@admin_bp.route("/notifications/mark-read", methods=["POST"])
@login_required
def notifications_mark_read() -> str:
    """Mark all notifications as read."""
    session = get_session()
    session.execute(
        Notification.__table__.update().values(is_read=True)
    )
    session.commit()
    session.close()
    return redirect(url_for("admin.notifications"))


@admin_bp.route("/notifications/<int:notif_id>/delete", methods=["POST"])
@login_required
def notification_delete(notif_id) -> str:
    """Delete a single notification."""
    session = get_session()
    notif = session.get(Notification, notif_id)
    if notif:
        session.delete(notif)
        session.commit()
    session.close()
    return redirect(url_for("admin.notifications"))

# ── Image management ──

import os as _os

_UPLOAD_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "static", "uploads")
_os.makedirs(_UPLOAD_DIR, exist_ok=True)


@admin_bp.route("/images")
@login_required
def images_index() -> str:
    """Image library — upload, browse, search."""
    session = get_session()
    tag_filter = request.args.get("tag", "").strip()
    q = request.args.get("q", "").strip()

    query = select(MediaImage).order_by(desc(MediaImage.uploaded_at))
    if tag_filter:
        query = query.where(MediaImage.tags.contains(tag_filter))
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(MediaImage.alt_text.ilike(like), MediaImage.original_name.ilike(like))
        )
    images = session.execute(query).scalars().all()

    # Collect all unique tags
    all_tags: set[str] = set()
    for img in images:
        for t in img.tags.split(","):
            t = t.strip()
            if t:
                all_tags.add(t)

    session.close()
    return render_template(
        "admin/images.html",
        images=images,
        all_tags=sorted(all_tags),
        tag_filter=tag_filter,
        q=q,
    )


@admin_bp.route("/images/upload", methods=["POST"])
@login_required
def images_upload() -> str:
    """Upload one or more image files."""
    files = request.files.getlist("files")
    if not files:
        flash("No files selected.", "error")
        return redirect(url_for("admin.images_index"))

    from PIL import Image as PILImage
    import uuid

    session = get_session()
    uploaded = []
    for f in files:
        if not f.filename:
            continue
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else "jpg"
        new_name = f"{uuid.uuid4().hex}.{ext}"
        save_path = _os.path.join(_UPLOAD_DIR, new_name)
        f.save(save_path)

        # Read dimensions
        w, h = 0, 0
        try:
            with PILImage.open(save_path) as img:
                w, h = img.size
        except Exception:
            pass

        tags = request.form.get("tags", "").strip()
        alt = request.form.get(f"alt_{f.filename}", "").strip()

        img = MediaImage(
            filename=new_name,
            original_name=f.filename,
            alt_text=alt,
            tags=tags,
            file_size=_os.path.getsize(save_path),
            width=w,
            height=h,
            uploaded_by=current_user.id,
        )
        session.add(img)
        uploaded.append(f.filename)

    session.commit()
    session.close()
    flash(f"Uploaded {len(uploaded)} image(s).", "success")
    return redirect(url_for("admin.images_index"))


@admin_bp.route("/images/<int:img_id>/delete", methods=["POST"])
@login_required
def images_delete(img_id) -> str:
    """Delete an image."""
    session = get_session()
    img = session.get(MediaImage, img_id)
    if img:
        path = _os.path.join(_UPLOAD_DIR, img.filename)
        if _os.path.exists(path):
            _os.remove(path)
        session.delete(img)
        session.commit()
        flash("Image deleted.", "success")
    session.close()
    return redirect(url_for("admin.images_index"))


@admin_bp.route("/images/<int:img_id>/edit", methods=["POST"])
@login_required
def images_edit(img_id) -> str:
    """Update image metadata (alt text, tags)."""
    session = get_session()
    img = session.get(MediaImage, img_id)
    if img:
        img.alt_text = request.form.get("alt_text", img.alt_text)
        img.tags = request.form.get("tags", img.tags)
        session.commit()
        flash("Image updated.", "success")
    session.close()
    return redirect(url_for("admin.images_index"))


@admin_bp.route("/images/api")
@login_required
def images_api() -> str:
    """Return images as JSON for the article image picker."""
    session = get_session()
    q = request.args.get("q", "").strip()
    tag = request.args.get("tag", "").strip()

    query = select(MediaImage).order_by(desc(MediaImage.uploaded_at))
    if tag:
        query = query.where(MediaImage.tags.contains(tag))
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(MediaImage.alt_text.ilike(like), MediaImage.original_name.ilike(like))
        )
    images = session.execute(query).scalars().all()
    session.close()

    return jsonify([
        {
            "id": img.id,
            "url": img.url,
            "filename": img.filename,
            "alt_text": img.alt_text,
            "tags": img.tags,
            "width": img.width,
            "height": img.height,
        }
        for img in images
    ])


# ── Style check ──


def _check_inline_links(body: str) -> bool:
    return body.count("](/") + body.count("](http") > 0

_STYLE_RULES = {
    "inline_links": {
        "label": "Inline Links",
        "desc": "Link to sources directly in the article body.",
        "check": _check_inline_links,
        "threshold": 1,
        "pass_msg": "Has inline links",
        "fail_msg": "No inline links found",
    },
    "min_sources": {
        "label": "Minimum Sources",
        "desc": "Aim for at least 3 sources.",
        "check": lambda body, sources: len(sources) >= 3,
        "threshold": 3,
        "pass_msg": "Has enough sources",
        "fail_msg": lambda n: f"Only {n} source(s) — add more",
    },
    "em_dash_count": {
        "label": "Em-Dashes",
        "desc": "Max 2 em-dashes per article.",
        "check": lambda body: len(__import__('re').findall(r'\u2014|---|&mdash;', body)),
        "threshold": 2,
        "pass_msg": "Em-dash count OK",
        "fail_msg": lambda n: f"{n} em-dashes (max 2)",
    },
    "narrative_angle": {
        "label": "Narrative Angle",
        "desc": "Does the article answer 'so what?'",
        "check": lambda body: any(kw in body.lower() for kw in ["question", "but", "however", "whether", "why", "at stake"]),
        "threshold": 1,
        "pass_msg": "Has narrative framing",
        "fail_msg": "No narrative angle detected",
    },
}


@admin_bp.route("/articles/<int:article_id>/style-check")
@login_required
def article_style_check(article_id) -> str:
    from db.core import get_session
    session = get_session()
    article = session.get(Article, article_id)
    if not article:
        session.close()
        return jsonify({"error": "Article not found"}), 404

    body = article.body or ""
    sources = list(article.sources)

    results = []
    score = 0
    total = len(_STYLE_RULES)

    for rule_id, rule in _STYLE_RULES.items():
        check = rule["check"]
        import sys
        if rule_id == "inline_links":
            print(f"Style check debug: body length={len(body)}, count = {body.count(chr(93)+chr(40)+chr(47))}", file=sys.stderr)
        try:
            result = check(body)
        except TypeError:
            result = check(body, sources)

        if isinstance(result, bool):
            passed = result
            msg = rule["pass_msg"] if passed else rule["fail_msg"]
        elif isinstance(result, int):
            passed = result <= rule["threshold"]
            key = "pass_msg" if passed else "fail_msg"
            msg = rule[key]
            if callable(msg):
                msg = msg(result)
        elif isinstance(result, list):
            passed = len(result) <= rule["threshold"]
            key = "pass_msg" if passed else "fail_msg"
            msg = rule[key]
            if callable(msg):
                msg = msg(result)
        else:
            passed = True
            msg = str(result)

        if callable(msg):
            msg = msg() if passed else msg(result)

        if passed:
            score += 1

        results.append({
            "id": rule_id,
            "label": rule["label"],
            "passed": passed,
            "message": msg,
        })

    session.close()
    return jsonify({
        "score": f"{score}/{total}",
        "all_passed": score == total,
        "checks": results,
    })


# ── Skeet Drafts ──

@admin_bp.route("/skeet-drafts")
@login_required
def skeet_drafts_list() -> str:
    """Redirect to the unified /admin/bluesky page."""
    return redirect(url_for("admin.bluesky_list"))


@admin_bp.route("/skeet-drafts/create/<int:article_id>", methods=["POST"])
@login_required
def skeet_draft_create(article_id) -> str:
    """Auto-generate a skeet draft for an article."""
    session = get_session()
    article = session.get(Article, article_id)
    if not article:
        session.close()
        flash("Article not found.", "error")
        return redirect(url_for("admin.dashboard"))

    # Check if draft already exists
    existing = session.execute(
        select(SkeetDraft).where(
            SkeetDraft.article_id == article_id,
            SkeetDraft.status.in_(["draft", "approved"]),
        )
    ).scalar_one_or_none()
    if existing:
        session.close()
        flash("Skeet draft already exists for this article.", "info")
        return redirect(url_for("admin.article_edit", article_id=article_id))

    # Generate draft text from the article
    import textwrap
    draft_text = article.title[:256]
    if article.summary:
        remaining = 300 - len(draft_text) - 3
        if remaining > 20:
            draft_text += "\n\n" + article.summary[:remaining]

    draft = SkeetDraft(
        article_id=article.id,
        draft_text=draft_text,
        status="draft",
        image_path=article.featured_image or "",
    )
    session.add(draft)
    session.commit()
    session.close()
    flash("Skeet draft created.", "success")
    return redirect(url_for("admin.skeet_drafts_list"))


@admin_bp.route("/skeet-drafts/<int:draft_id>/edit", methods=["POST"])
@login_required
def skeet_draft_edit(draft_id) -> str:
    """Update draft text, image, or status."""
    session = get_session()
    draft = session.get(SkeetDraft, draft_id)
    if not draft:
        session.close()
        flash("Draft not found.", "error")
        return redirect(url_for("admin.skeet_drafts_list"))

    text = request.form.get("draft_text", "").strip()
    if text:
        draft.draft_text = text[:300]
    image_path = request.form.get("image_path", "").strip()
    if image_path:
        draft.image_path = image_path

    action = request.form.get("action", "")
    if action == "approve":
        draft.status = "approved"
    elif action == "skip":
        draft.status = "skipped"
    elif action == "draft":
        draft.status = "draft"

    draft.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.close()
    flash("Skeet draft updated.", "success")
    return redirect(url_for("admin.skeet_drafts_list"))


@admin_bp.route("/skeet-drafts/<int:draft_id>/delete", methods=["POST"])
@login_required
def skeet_draft_delete(draft_id) -> str:
    """Delete a skeet draft."""
    session = get_session()
    draft = session.get(SkeetDraft, draft_id)
    if draft:
        session.delete(draft)
        session.commit()
    session.close()
    flash("Skeet draft deleted.", "success")
    return redirect(url_for("admin.skeet_drafts_list"))


@admin_bp.route("/skeet-drafts/<int:draft_id>/post", methods=["POST"])
@login_required
def skeet_draft_post(draft_id) -> str:
    """Post an approved skeet draft to Bluesky immediately."""
    session = get_session()
    draft = session.get(SkeetDraft, draft_id)
    if not draft:
        session.close()
        flash("Draft not found.", "error")
        return redirect(url_for("admin.skeet_drafts_list"))

    article = session.get(Article, draft.article_id)
    if not article:
        session.close()
        flash("Article not found.", "error")
        return redirect(url_for("admin.skeet_drafts_list"))

    if draft.status not in ("approved", "draft"):
        session.close()
        flash("Only approved drafts can be posted.", "error")
        return redirect(url_for("admin.skeet_drafts_list"))

    from flask import url_for
    base_url = "https://poliscopic.com"
    article_url = f"{base_url}/articles/{article.slug}"

    # Fetch image for the link card
    image_bytes = None
    if draft.image_path:
        try:
            import urllib.request
            if draft.image_path.startswith(("http://", "https://")):
                with urllib.request.urlopen(draft.image_path, timeout=15) as resp:
                    image_bytes = resp.read()
            else:
                import os as _os
                local_path = _os.path.join(
                    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                    "static", draft.image_path.lstrip("/"),
                )
                if _os.path.exists(local_path):
                    with open(local_path, "rb") as f:
                        image_bytes = f.read()
        except Exception as e:
            log.warning("Image fetch failed for skeet draft %d: %s", draft_id, e)

    from social import post_to_bluesky as _do_post
    uri = _do_post(
        title=article.title,
        summary=article.summary,
        url=article_url,
        image_bytes=image_bytes,
        draft_text=draft.draft_text,
    )

    if uri:
        draft.status = "posted"
        draft.bluesky_post_uri = uri
        draft.posted_at = datetime.now(timezone.utc)
        session.commit()
        session.close()
        flash("Posted to Bluesky!", "success")
    else:
        session.close()
        flash("Failed to post to Bluesky.", "error")

    return redirect(url_for("admin.skeet_drafts_list"))
