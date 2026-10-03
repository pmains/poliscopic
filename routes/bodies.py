"""Public bodies routes blueprint."""

from datetime import date

from flask import Blueprint, render_template, request

from db.core import session_scope
from poliscopic.db.repositories import (
    load_public_body_detail,
    load_public_body_directory,
)


bodies_bp = Blueprint("bodies", __name__, url_prefix="")

# ---------------------------------------------------------------------------
# Public Bodies / Members — Routes
# ---------------------------------------------------------------------------

@bodies_bp.route("/bodies")
def bodies_index():
    """List all known public bodies grouped by jurisdiction."""
    filter_jurisdiction = request.args.get("jurisdiction", "").strip()
    with session_scope() as session:
        jurisdictions, directory = load_public_body_directory(
            session, filter_jurisdiction
        )

        result = []
        for jurisdiction, bodies in directory:
            # Primary governing bodies first, then alphabetical.
            bodies = sorted(bodies, key=lambda body: (
                not (
                    body.name.endswith("City Council")
                    or body.name.endswith("Town Council")
                    or body.name in {
                        "Board of Supervisors",
                        "Maricopa County Board of Supervisors",
                    }
                ),
                body.name,
            ))
            result.append((jurisdiction, bodies))
    return render_template(
        "bodies_index.html",
        jurisdictions=result,
        all_jurisdictions=jurisdictions,
        filter_jurisdiction=filter_jurisdiction,
    )


@bodies_bp.route("/bodies/<slug>")
def body_detail(slug):
    """Show members of a public body with pagination."""
    page = request.args.get("page", 1, type=int)
    with session_scope() as session:
        detail = load_public_body_detail(session, slug, page=page, per_page=10)

    if detail is None:
        return "Body not found", 404
    return render_template(
        "body_detail.html",
        body=detail.body,
        jurisdiction=detail.jurisdiction,
        members=detail.members,
        page=detail.page,
        total_pages=detail.total_pages,
        total=detail.total,
        per_page=detail.per_page,
        today=date.today(),
    )
