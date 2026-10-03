"""Public article routes: front page, article detail, archive."""
from datetime import date as date_cls, timedelta
from sqlalchemy import select, desc, and_
from sqlalchemy.orm import joinedload

from flask import Blueprint, render_template, request, abort
from db import Jurisdiction, PublicBody
from db.core import session_scope
from db.models import Meeting
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


def _load_front_page_records(
    today_str: str,
    end_str: str,
    trending_ids: list[int],
) -> tuple[list[Article], list[Tag], list[Meeting], dict[int, Article]]:
    """Load front-page database records within one owned read session."""
    with session_scope() as session:
        recent = session.execute(
            select(Article).where(Article.status == "published")
            .order_by(desc(Article.published_at))
            .limit(23)
        ).scalars().all()
        tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
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
        article_map = {}
        if trending_ids:
            db_articles = session.execute(
                select(Article).where(Article.id.in_(trending_ids))
            ).scalars().all()
            article_map = {article.id: article for article in db_articles}

    return recent, tags, upcoming, article_map


@articles_bp.route("/")
def front_page():
    """Main front page — published news feed."""
    # Upcoming meetings this week
    today = date_cls.today()
    end_date = today + timedelta(days=7)
    today_str = today.isoformat()
    end_str = end_date.isoformat()

    # Trending articles (most viewed in last 24 hours)
    from analytics_db import get_trending
    _trending_data = get_trending(limit=5) or []
    trending_ids = [item["article_id"] for item in _trending_data]
    recent, tags, upcoming, article_map = _load_front_page_records(
        today_str,
        end_str,
        trending_ids,
    )

    # Featured = 3 most recent published articles (Brief 013) — no manual
    # curation; the feed below is the next 20.
    featured = recent[:3]
    articles = recent[3:23]

    # Enrich analytics records with article metadata.
    trending_rows = []
    for item in _trending_data:
        article = article_map.get(item["article_id"])
        if article and article.status == "published":
            trending_rows.append({
                "title": article.title,
                "slug": article.slug,
                "recent_views": item["recent_views"],
            })

    upcoming_display = []
    seen_dedup: set[tuple[str, str, str]] = set()
    for m in upcoming:
        # Deduplicate by (date, body, meeting_type) — occasionally two
        # meeting_ids exist for the same meeting (rescheduled entries).
        key = (m.meeting_date or "", m.body, m.meeting_type or "")
        if key in seen_dedup:
            continue
        seen_dedup.add(key)
        # Display name comes from the canonical registry (db.names), the
        # single source of truth for body names.
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

    return render_template("front_page.html", articles=articles,
                           featured=featured, tags=tags,
                           upcoming_meetings=upcoming_display,
                           trending=trending_rows)


@articles_bp.route("/articles/<slug>")
def article_detail(slug):
    with session_scope() as session:
        article = session.execute(
            select(Article)
            .options(joinedload(Article.author), joinedload(Article.tags))
            .where(Article.slug == slug)
        ).unique().scalar_one_or_none()
        if not article or article.status not in ("published", "archived"):
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
    with session_scope() as session:
        articles = session.execute(
            select(Article).where(Article.status == "archived")
            .order_by(desc(Article.archived_at))
        ).scalars().all()
        tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    return render_template("archive.html", articles=articles, tags=tags)


@articles_bp.route("/articles/tag/<tag_slug>")
def by_tag(tag_slug):
    with session_scope() as session:
        tag = session.execute(
            select(Tag).where(Tag.slug == tag_slug)
        ).scalar_one_or_none()
        if not tag:
            abort(404)
        articles = session.execute(
            select(Article).where(
                Article.tags.any(Tag.id == tag.id),
                Article.status.in_(["published", "archived"]),
            ).order_by(desc(Article.published_at))
        ).scalars().all()
        tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
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

    with session_scope() as session:
        tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()

        # Jurisdictions and bodies for filter dropdowns
        jurisdictions = session.execute(
            select(Jurisdiction).order_by(Jurisdiction.name)
        ).scalars().all()

        # Build body list — filtered by jurisdiction if one is selected
        body_q = select(PublicBody).order_by(PublicBody.name)
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
