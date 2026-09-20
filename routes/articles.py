"""Public article routes: front page, article detail, archive."""
import re
from datetime import datetime, timezone, date as date_cls, timedelta
from sqlalchemy import select, desc, and_
from sqlalchemy.orm import joinedload

from flask import Blueprint, render_template, request, abort
from db import get_session, Jurisdiction, PublicBody
from db.names import get_display_name
from db.newsroom import Article, Tag, search_articles, search_agenda_items, search_supporting_documents, search_entities

articles_bp = Blueprint("articles", __name__)


def _code_to_name(code: str) -> str:
    """Safely convert a body code to a human-readable name."""
    if not code:
        return code

    # Tempe subcommittees — full names
    _sub_names = {
        "tempe-animal-welfare-subcommittee": "Animal Welfare Subcommittee",
        "tempe-community-engagement-subcommittee": "Community Engagement Subcommittee",
        "tempe-drink-spiking-subcommittee": "Drink Spiking Subcommittee",
        "tempe-mixed-use-space-subcommittee": "Mixed-Use Space Subcommittee",
        "tempe-mobility-safety-subcommittee": "Mobility Safety Subcommittee",
        "tempe-town-lake-subcommittee": "Town Lake Subcommittee",
        "tempe-term-limits-subcommittee": "Term Limits Subcommittee",
        "tempe-advocacy-review-subcommittee": "Advocacy Review Subcommittee",
    }
    if code in _sub_names:
        return f"Tempe {_sub_names[code]}"

    # Maricopa County boards
    _mc_names = {
        "bos": "Maricopa County Board of Supervisors",
        "pz": "Maricopa County Planning & Zoning",
        "adj": "Maricopa County Board of Adjustment",
        "health": "Maricopa County Board of Health",
        "tab": "Maricopa County Transportation Advisory Board",
        "ida": "Maricopa County Industrial Development Authority",
    }
    if code in _mc_names:
        return _mc_names[code]

    if code.startswith("mc-"):
        # Convert mc-audit → Audit Advisory, mc-mcso-corp → MCSO CORP, etc.
        rest = code[3:]  # strip "mc-"
        label = rest.replace("-", " ").upper()
        # Known MCACC board names
        _mcacc = {
            "audit": "Audit Advisory Committee",
            "benefit trust": "Benefit Board of Trustees",
            "community action": "Community Action Commission",
            "cdac": "Community Development Advisory Committee",
            "eed policy": "Early Education Division Policy Council",
            "flood advisory": "Flood Control Advisory Board",
            "home": "HOME Consortium",
            "mclepc": "Local Emergency Planning Committee",
            "mcao psprs": "MCAO PSPRS Local Board",
            "mcso corp": "MCSO CORP Local Board",
            "mcso psprs": "MCSO PSPRS Local Board",
            "merit": "Merit Systems Commission",
            "psfc": "Public Safety Funding Committee",
            "risk trust": "Self-Insured Risk Trust Fund",
            "smart savings": "Smart Savings Committee",
            "stadium": "Stadium District Board",
            "trp": "Travel Reduction Program",
            "air pollution": "Air Pollution Hearing Board",
            "bcab": "Building Code Advisory Board",
            "flood stakeholder": "Flood Control Stakeholder Group",
        }
        return _mcacc.get(rest.replace("-", " "), f"Maricopa County {label}")

    parts = code.split("-")
    if len(parts) >= 2:
        city = parts[0].title()
        suffix = parts[-1].upper()
        if suffix == "CC":
            return f"{city} City Council"
        if suffix in ("PZ", "PC"):
            label = "Planning Commission" if suffix == "PC" else "Planning & Zoning"
            return f"{city} {label}"
        if suffix in ("DRC", "DRB"):
            return f"{city} Development Review Commission"
        if suffix == "BOA":
            return f"{city} Board of Adjustment"
        if suffix == "HPC":
            return f"{city} Historic Preservation Commission"
        if suffix == "HA":
            return f"{city} Housing Authority"
        if suffix == "JRC":
            return f"{city} Joint Review Committee"
        if suffix == "RIO":
            return f"{city} Rio Salado CFD"
        if suffix == "RMT":
            return f"{city} Risk Management Trust"
        if suffix == "TC":
            return f"{city} Town Council"
        return f"{city} {suffix}"
    return code.title()


@articles_bp.route("/")
def front_page():
    """Main front page — published news feed."""
    session = get_session()
    # Featured = 3 most recent published articles (Brief 013) — no manual
    # curation; the feed below is the next 20.
    recent = session.execute(
        select(Article).where(Article.status == "published")
        .order_by(desc(Article.published_at))
        .limit(23)
    ).scalars().all()
    featured = recent[:3]
    articles = recent[3:23]

    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()

    # Upcoming meetings this week
    from db.models import Meeting
    today = date_cls.today()
    end_date = today + timedelta(days=7)
    today_str = today.isoformat()
    end_str = end_date.isoformat()

    upcoming = session.execute(
        select(Meeting)
        .where(
            and_(
                Meeting.meeting_date >= today_str,
                Meeting.meeting_date <= end_str,
                Meeting.sync_status.in_(["complete", "pending"]),
            )
        )
        .order_by(Meeting.meeting_date, Meeting.body)
        .limit(15)
    ).scalars().all()

    # Trending articles (most viewed in last 24 hours)
    from analytics_db import get_trending
    _trending_data = get_trending(limit=5)
    # Enrich with article metadata
    trending_rows = []
    if _trending_data:
        trending_ids = [t['article_id'] for t in _trending_data]
        from sqlalchemy import select as _sel
        db_articles = session.execute(
            _sel(Article).where(Article.id.in_(trending_ids))
        ).scalars().all()
        article_map = {a.id: a for a in db_articles}
        for t in _trending_data:
            a = article_map.get(t['article_id'])
            if a and a.status == 'published':
                trending_rows.append({
                    'title': a.title,
                    'slug': a.slug,
                    'recent_views': t['recent_views'],
                })

    # Body code to human-readable name lookup
    _body_names = {
        # Maricopa County
        "bos": "Maricopa County Board of Supervisors",
        "mc-audit": "Maricopa County Audit Advisory Committee",
        "mc-benefit-trust": "Maricopa County Benefit Board of Trustees",
        "mc-community-action": "Maricopa County Community Action Commission",
        "mc-cdac": "Maricopa County Community Development Advisory Committee",
        "mc-eed-policy": "Maricopa County Early Education Division Policy Council",
        "mc-flood-advisory": "Maricopa County Flood Control Advisory Board",
        "mc-home": "Maricopa County HOME Consortium",
        "mc-mclepc": "Maricopa County Local Emergency Planning Committee",
        "mc-mcao-psprs": "Maricopa County MCAO PSPRS Local Board",
        "mc-mcso-corp": "Maricopa County MCSO CORP Local Board",
        "mc-mcso-psprs": "Maricopa County MCSO PSPRS Local Board",
        "mc-merit": "Maricopa County Merit Systems Commission",
        "mc-psfc": "Maricopa County Public Safety Funding Committee",
        "mc-risk-trust": "Maricopa County Self-Insured Risk Trust Fund Board of Trustees",
        "mc-smart-savings": "Maricopa County Smart Savings Committee",
        "mc-stadium": "Maricopa County Stadium District Board",
        "mc-trp": "Maricopa County Travel Reduction Program",
        "mc-air-pollution": "Maricopa County Air Pollution Hearing Board",
        "mc-bcab": "Maricopa County Building Code Advisory Board",
        "mc-flood-stakeholder": "Maricopa County Flood Control District Stakeholder Group",
        "pz": "Maricopa County Planning & Zoning",
        "adj": "Maricopa County Board of Adjustment",
        "drain": "Maricopa County Drainage Review",
        "health": "Maricopa County Board of Health",
        "tab": "Maricopa County Transportation Board",
        "ida": "Maricopa County IDA",
        # Tempe
        "tempe-cc": "Tempe City Council",
        "tempe-drc": "Tempe Development Review Commission",
        "tempe-pz": "Tempe Planning & Zoning Commission",
        "tempe-boa": "Tempe Board of Adjustment",
        "tempe-hpc": "Tempe Historic Preservation Commission",
        "tempe-ha": "Tempe Housing Authority",
        "tempe-jrc": "Tempe Joint Review Committee",
        "tempe-rio": "Tempe Rio Salado CFD",
        "tempe-rmt": "Tempe Risk Management Trust",
        "tempe-animal-welfare-subcommittee": "Tempe Animal Welfare Subcommittee",
        "tempe-community-engagement-subcommittee": "Tempe Community Engagement Subcommittee",
        "tempe-drink-spiking-subcommittee": "Tempe Drink Spiking Subcommittee",
        "tempe-mixed-use-space-subcommittee": "Tempe Mixed-Use Space Subcommittee",
        "tempe-mobility-safety-subcommittee": "Tempe Mobility Safety Subcommittee",
        "tempe-town-lake-subcommittee": "Tempe Town Lake Subcommittee",
        "tempe-term-limits-subcommittee": "Tempe Term Limits Subcommittee",
        "tempe-advocacy-review-subcommittee": "Tempe Advocacy Review Subcommittee",
        # Mesa
        "mesa-city-council": "Mesa City Council",
        "mesa-pz": "Mesa Planning & Zoning Board",
        "mesa-city-council": "Mesa City Council",
        "mesa-design-review-board": "Mesa Design Review Board",
        "mesa-board-of-adjustment": "Mesa Board of Adjustment",
        "mesa-historic-preservation-board": "Mesa Historic Preservation Board",
        # Chandler (from BODY_MAP)
        "chandler-cc": "Chandler City Council",
        "chandler-pz": "Chandler Planning & Zoning Commission",
        "chandler-drc": "Chandler Development Review Commission",
        "chandler-boa": "Chandler Board of Adjustment",
        "chandler-hpc": "Chandler Historic Preservation Commission",
        "chandler-ida": "Chandler Industrial Development Authority",
        "chandler-prb": "Chandler Parks and Recreation Board",
        "chandler-lb": "Chandler Library Board",
        "chandler-mf": "Chandler Museum Foundation",
        "chandler-cf": "Chandler Cultural Foundation",
        "chandler-arts": "Chandler Arts Commission",
        "chandler-tc": "Chandler Transportation Commission",
        "chandler-mvc": "Chandler Military and Veterans Affairs Commission",
        "chandler-hhsc": "Chandler Housing and Human Services Commission",
        "chandler-hrc": "Chandler Human Relations Commission",
        "chandler-dvc": "Chandler Domestic Violence Commission",
        "chandler-pha": "Chandler Public Housing Authority",
        "chandler-nac": "Chandler Neighborhood Advisory Committee",
        "chandler-yc": "Chandler Mayor's Youth Commission",
        "chandler-pdc": "Chandler Mayor's Committee for People with Disabilities",
        "chandler-eda": "Chandler Economic Development Advisory Board",
        "chandler-psprs-f": "Chandler PSPRS Board Fire",
        "chandler-psprs-p": "Chandler PSPRS Board Police",
        "chandler-hcc": "Chandler Housing and Community Services Corporation",
        "chandler-cpr": "Chandler Citizens' Panel Review",
        "chandler-hct": "Chandler Health Care Benefits Trust Board",
        "chandler-wct": "Chandler Workers' Compensation Trust Board",
        "chandler-air": "Chandler Airport Commission",
        # Scottsdale
        "scottsdale-cc": "Scottsdale City Council",
        "scottsdale-pc": "Scottsdale Planning Commission",
        "scottsdale-boa": "Scottsdale Board of Adjustment",
        "scottsdale-drb": "Scottsdale Development Review Board",
        "scottsdale-hpc": "Scottsdale Historic Preservation Commission",
        "scottsdale-baba": "Scottsdale Building Appeals Board",
        # Gilbert
        "gilbert-tc": "Gilbert Town Council",
        # Peoria
        "peoria-planning-zoning": "Peoria Planning & Zoning Commission",
        "peoria-boa": "Peoria Board of Adjustment",
        "peoria-sub": "Peoria Subcommittee",
        # Glendale
        "glendale-cc": "Glendale City Council",
        "glendale-pc": "Glendale Planning Commission",
        "glendale-boa": "Glendale Board of Adjustment",
        # Surprise
        "surprise-cc": "Surprise City Council",
        "surprise-pz": "Surprise Planning & Zoning",
        # Phoenix
        "phoenix-cc": "Phoenix City Council",
        "phoenix-pc": "Phoenix Planning Commission",
        "phoenix-village-planning": "Phoenix Village Planning Committees",
        "phoenix-historic-preservation": "Phoenix Historic Preservation Commission",
        "phoenix-zoning-adjustment": "Phoenix Board of Zoning Adjustment",
        "phoenix-human-services": "Phoenix Human Services Commission",
        "phoenix-human-relations": "Phoenix Human Relations Commission",
        "phoenix-environmental-quality": "Phoenix Environmental Quality Commission",
        "phoenix-disability-issues": "Phoenix Disability Issues Commission",
        "phoenix-womens-commission": "Phoenix Women's Commission",
        "phoenix-heritage-commission": "Phoenix Heritage Commission",
        "phoenix-license-appeal": "Phoenix License Appeal Board",
        "phoenix-fire-pension": "Phoenix Fire Pension Board",
        "phoenix-police-pension": "Phoenix Police Pension Board",
        "phoenix-copers-board": "Phoenix COPERS Board",
        "phoenix-cs": "Phoenix Community Services Subcommittee",
        "phoenix-ti": "Phoenix Transportation & Infrastructure Subcommittee",
        "phoenix-ed": "Phoenix Economic Development Subcommittee",
        "phoenix-ps": "Phoenix Public Safety Subcommittee",
        # Gilbert
        "glendale-cc": "Glendale City Council",
        "peoria-cc": "Peoria City Council",
        # Buckeye
        "buckeye-cc": "Buckeye City Council",
        "buckeye-pz": "Buckeye Planning & Zoning",
        # Goodyear
        "goodyear-cc": "Goodyear City Council",
        "buckeye-cc": "Buckeye City Council",
        "avondale-cc": "Avondale City Council",
        "el-mirage-cc": "El Mirage City Council",
        "goodyear-pz": "Goodyear Planning & Zoning",
        "peoria-planning-zoning": "Peoria Planning & Zoning Commission",
        "surprise-cc": "Surprise City Council",
        "gilbert-tc": "Gilbert Town Council",
        # MCACC bodies (Maricopa County AgendaCenter)
        "mc-audit": "Audit Advisory Committee",
        "mc-benefit-trust": "Benefit Board of Trustees",
        "mc-community-action": "Community Action Commission",
        "mc-cdac": "Community Development Advisory Committee",
        "mc-eed-policy": "Early Education Division Policy Council",
        "mc-flood-advisory": "Flood Control Advisory Board",
        "mc-home": "HOME Consortium",
        "mc-mclepc": "Maricopa County Local Emergency Planning Committee",
        "mc-mcao-psprs": "MCAO PSPRS Local Board",
        "mc-mcso-corp": "MCSO Correctional Officer Retirement Plan Local Board",
        "mc-mcso-psprs": "MCSO PSPRS Local Board",
        "mc-merit": "Merit Systems Commission",
        "mc-psfc": "Public Safety Funding Committee",
        "mc-risk-trust": "Self-Insured Risk Trust Fund Board of Trustees",
        "mc-smart-savings": "Smart Savings Committee (Deferred Compensation)",
        "mc-stadium": "Stadium District Board",
        "mc-trp": "Travel Reduction Program",
        "mc-air-pollution": "Air Pollution Hearing Board",
        "mc-bcab": "Building Code Advisory Board",
        "mc-flood-stakeholder": "Flood Control District Stakeholder Group",
    }
    upcoming_display = []
    seen_dedup: set[tuple[str, str, str]] = set()
    for m in upcoming:
        # Deduplicate by (date, body, meeting_type) — occasionally two
        # meeting_ids exist for the same meeting (rescheduled entries).
        key = (m.meeting_date or "", m.body, m.meeting_type or "")
        if key in seen_dedup:
            continue
        seen_dedup.add(key)
        # Display name comes from the canonical registry (db.names), which is
        # the single source of truth for body names.  The local _body_names map
        # that used to live here was a second source of truth that had drifted:
        # it carried 19 dead entries for codes the data never used, while 1,732
        # meetings sat on codes it lacked entirely.  It was removed 2026-09-18
        # (docs/briefs/034-dev-body-cleanup-changes-2026-09-18.md).
        #
        # "with-jurisdiction" qualifies county bodies ("Maricopa County Board
        # of Adjustment") and leaves municipal names unchanged, since those
        # already carry their city.
        display = (get_display_name("with-jurisdiction", m.body)
                   or _code_to_name(m.body))
        upcoming_display.append({
            "body": m.body,
            "display_name": display,
            "meeting_id": m.meeting_id,
            "meeting_date": m.meeting_date,
            "meeting_type": m.meeting_type,
        })

    session.close()
    return render_template("front_page.html", articles=articles,
                           featured=featured, tags=tags,
                           upcoming_meetings=upcoming_display,
                           trending=trending_rows)


@articles_bp.route("/articles/<slug>")
def article_detail(slug):
    session = get_session()
    article = session.execute(
        select(Article)
        .options(joinedload(Article.author), joinedload(Article.tags))
        .where(Article.slug == slug)
    ).unique().scalar_one_or_none()
    if not article or article.status not in ("published", "archived"):
        session.close()
        abort(404)
    # Track view in analytics DB (persists across sync.sh overwrites)
    try:
        from analytics_db import track_page_view
        track_page_view(article.id)
    except Exception:
        pass
    article = session.execute(
        select(Article)
        .options(joinedload(Article.author), joinedload(Article.tags))
        .where(Article.id == article.id)
    ).unique().scalar_one_or_none()
    session.close()
    # Newsletter digests get an inline subscribe widget for their topic.
    newsletter_topic = None
    try:
        from newsletter_svc import topic_for_article_slug, NEWSLETTER_TOPICS
        t = topic_for_article_slug(article.slug)
        if t:
            newsletter_topic = {"slug": t, "label": NEWSLETTER_TOPICS.get(t, t)}
    except Exception:
        newsletter_topic = None
    return render_template("article.html", article=article,
                           newsletter_topic=newsletter_topic)


@articles_bp.route("/articles/archive")
def archive():
    session = get_session()
    articles = session.execute(
        select(Article).where(Article.status == "archived")
        .order_by(desc(Article.archived_at))
    ).scalars().all()
    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    session.close()
    return render_template("archive.html", articles=articles, tags=tags)


@articles_bp.route("/articles/tag/<tag_slug>")
def by_tag(tag_slug):
    session = get_session()
    tag = session.execute(
        select(Tag).where(Tag.slug == tag_slug)
    ).scalar_one_or_none()
    if not tag:
        session.close()
        abort(404)
    articles = session.execute(
        select(Article).where(
            Article.tags.any(Tag.id == tag.id),
            Article.status.in_(["published", "archived"]),
        ).order_by(desc(Article.published_at))
    ).scalars().all()
    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    session.close()
    return render_template("by_tag.html", tag=tag, articles=articles, tags=tags)


@articles_bp.route("/search")
def search():
    q = request.args.get("q", "").strip()
    # Multi-select scope: checkboxes send multiple ?scope= values.
    # Default = everything except articles.
    scopes = request.args.getlist("scope")
    if not scopes:
        scopes = ["agendas", "documents", "entities"]
    sort = request.args.get("sort", "date")  # date, relevance
    from_date = request.args.get("from", "").strip() or None
    to_date = request.args.get("to", "").strip() or None
    jurisdiction = request.args.get("jurisdiction", "").strip() or None
    body = request.args.get("body", "").strip() or None
    articles = []
    agenda_items = []
    tags = []
    entities = []
    entities_truncated = False

    session = get_session()
    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()

    # Jurisdictions and bodies for filter dropdowns
    jurisdictions = session.execute(
        select(Jurisdiction).order_by(Jurisdiction.name)
    ).scalars().all()

    # Build body list — filtered by jurisdiction if one is selected
    from sqlalchemy import select as _select
    body_q = _select(PublicBody).order_by(PublicBody.name)
    if jurisdiction:
        jur_ids = [
            j.id for j in jurisdictions
            if j.slug == jurisdiction or j.name == jurisdiction
        ]
        if jur_ids:
            body_q = body_q.where(PublicBody.jurisdiction_id == jur_ids[0])
    all_bodies = session.execute(body_q).scalars().all()

    articles_truncated = False
    agenda_truncated = False
    documents_truncated = False
    documents = []

    if q:
        if "articles" in scopes:
            articles, articles_truncated = search_articles(q)
        if "agendas" in scopes:
            agenda_items, agenda_truncated = search_agenda_items(
                q, sort=sort, from_date=from_date, to_date=to_date,
                jurisdiction=jurisdiction, body=body,
            )
        if "documents" in scopes:
            documents, documents_truncated = search_supporting_documents(
                q, sort=sort, from_date=from_date, to_date=to_date,
                jurisdiction=jurisdiction, body=body,
            )
        if "entities" in scopes:
            entities, entities_truncated = search_entities(q)

    session.close()
    return render_template("search.html", q=q, scopes=scopes, sort=sort,
                           from_date=from_date or "", to_date=to_date or "",
                           jurisdiction=jurisdiction or "", body=body or "",
                           articles=articles, agenda_items=agenda_items,
                           documents=documents,
                           entities=entities,
                           articles_truncated=articles_truncated,
                           agenda_truncated=agenda_truncated,
                           documents_truncated=documents_truncated,
                           entities_truncated=entities_truncated,
                           tags=tags,
                           jurisdictions=jurisdictions, all_bodies=all_bodies)
