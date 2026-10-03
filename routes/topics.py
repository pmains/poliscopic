"""Topics blueprint — intelligence report pages for each subject area.

Routes:
    /topics                         — Topic index (list of all active topics)
    /topics/<slug>                  — Latest report for a topic
    /topics/<slug>/archive          — Archive of all past reports
    /topics/<slug>/<date>           — Specific archived report
"""

import json
import logging

from flask import Blueprint, abort, render_template
from sqlalchemy import desc, select

from db.core import session_scope
from db.newsroom import Topic, TopicWeeklyReport, Article

log = logging.getLogger(__name__)

topics_bp = Blueprint("topics", __name__, url_prefix="/topics")

# ── Helpers ──


def _load_report_data(report: TopicWeeklyReport) -> dict:
    """Convert a TopicWeeklyReport row into a template-friendly dict."""
    activity = {}
    metric_values = {}
    article_ids = []
    try:
        activity = json.loads(report.activity_by_jurisdiction) if report.activity_by_jurisdiction else {}
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        metric_values = json.loads(report.metric_values) if report.metric_values else {}
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        article_ids = json.loads(report.article_ids) if report.article_ids else []
    except (json.JSONDecodeError, TypeError):
        pass

    return {
        "id": report.id,
        "report_date": report.report_date,
        "summary": report.summary,
        "body_html": report.body_html,
        "activity_by_jurisdiction": sorted(
            activity.items(), key=lambda x: -x[1]["count"]
        ) if activity else [],
        "metric_values": metric_values,
        "article_ids": article_ids,
        "generated_at": report.generated_at,
    }


# ── Routes ──


@topics_bp.route("")
def topic_index() -> str:
    """List all active topics with their latest report dates."""
    with session_scope() as session:
        topics = session.execute(
            select(Topic).where(Topic.is_active.is_(True)).order_by(Topic.sort_order)
        ).scalars().all()

        topic_data = []
        for t in topics:
            latest = session.execute(
                select(TopicWeeklyReport)
                .where(TopicWeeklyReport.topic_id == t.id)
                .order_by(desc(TopicWeeklyReport.report_date))
                .limit(1)
            ).scalar_one_or_none()

            article_count = session.execute(
                select(TopicWeeklyReport)
                .where(TopicWeeklyReport.topic_id == t.id)
            ).scalars().all()

            topic_data.append({
                "slug": t.slug,
                "title": t.title,
                "description": t.description,
                "latest_report_date": latest.report_date if latest else None,
                "report_count": len(article_count),
            })

    return render_template("topic_index.html", topics=topic_data)


@topics_bp.route("/<slug>")
def topic_latest(slug: str) -> str:
    """Show the most recent report for a topic."""
    with session_scope() as session:
        topic = session.execute(
            select(Topic).where(Topic.slug == slug, Topic.is_active.is_(True))
        ).scalar_one_or_none()

        if not topic:
            abort(404)

        latest = session.execute(
            select(TopicWeeklyReport)
            .where(TopicWeeklyReport.topic_id == topic.id)
            .order_by(desc(TopicWeeklyReport.report_date))
            .limit(1)
        ).scalar_one_or_none()

        if not latest:
            # Topic exists but no reports yet
            return render_template("topic_detail.html",
                                   topic=topic, report=None)

        report = _load_report_data(latest)

        # Load featured article if set
        featured = None
        if latest.featured_article_id:
            article = session.get(Article, latest.featured_article_id)
            if article:
                featured = {"title": article.title, "slug": article.slug}

        # Load linked articles
        linked_articles = []
        if report["article_ids"]:
            articles = session.execute(
                select(Article).where(Article.id.in_(report["article_ids"]))
            ).scalars().all()
            linked_articles = [
                {"title": a.title, "slug": a.slug, "summary": a.summary}
                for a in articles
            ]

    return render_template("topic_detail.html",
                           topic=topic,
                           report=report,
                           featured=featured,
                           articles=linked_articles)


@topics_bp.route("/<slug>/archive")
def topic_archive(slug: str) -> str:
    """Show all archived reports for a topic."""
    with session_scope() as session:
        topic = session.execute(
            select(Topic).where(Topic.slug == slug, Topic.is_active.is_(True))
        ).scalar_one_or_none()

        if not topic:
            abort(404)

        reports = session.execute(
            select(TopicWeeklyReport)
            .where(TopicWeeklyReport.topic_id == topic.id)
            .order_by(desc(TopicWeeklyReport.report_date))
        ).scalars().all()

        archive = []
        for r in reports:
            archive.append({
                "report_date": r.report_date,
                "summary": r.summary[:200] if r.summary else "",
            })

    return render_template("topic_archive.html",
                           topic=topic, archive=archive)


@topics_bp.route("/<slug>/<report_date>")
def topic_report(slug: str, report_date: str) -> str:
    """Show a specific archived report by date."""
    with session_scope() as session:
        topic = session.execute(
            select(Topic).where(Topic.slug == slug, Topic.is_active.is_(True))
        ).scalar_one_or_none()

        if not topic:
            abort(404)

        report_row = session.execute(
            select(TopicWeeklyReport).where(
                TopicWeeklyReport.topic_id == topic.id,
                TopicWeeklyReport.report_date == report_date,
            )
        ).scalar_one_or_none()

        if not report_row:
            abort(404)

        report = _load_report_data(report_row)

        featured = None
        if report_row.featured_article_id:
            article = session.get(Article, report_row.featured_article_id)
            if article:
                featured = {"title": article.title, "slug": article.slug}

        linked_articles = []
        if report["article_ids"]:
            articles = session.execute(
                select(Article).where(Article.id.in_(report["article_ids"]))
            ).scalars().all()
            linked_articles = [
                {"title": a.title, "slug": a.slug, "summary": a.summary}
                for a in articles
            ]

    return render_template("topic_detail.html",
                           topic=topic,
                           report=report,
                           featured=featured,
                           articles=linked_articles,
                           is_archive=True)
