"""Theme preview routes — demo different color schemes on front-page content."""

from flask import Blueprint, render_template
from sqlalchemy import select, desc, and_
from sqlalchemy.orm import joinedload

from db.core import get_session
from db.newsroom import Article, Tag

themes_bp = Blueprint("themes", __name__, url_prefix="/themes")

# ── Theme definitions ──

THEMES = {
    1: {
        "name": "Navy & Orange",
        "desc": "Dark blue primary, bold orange accent — authoritative and warm.",
        "colors": {
            "primary": "#05235B",
            "secondary": "#0456AF",
            "accent": "#ED6800",
            "highlight": "#FAB003",
        },
    },
    2: {
        "name": "Medium Blue & Yellow",
        "desc": "Lighter blue base with golden yellow highlights — bright and modern.",
        "colors": {
            "primary": "#0456AF",
            "secondary": "#05235B",
            "accent": "#FAB003",
            "highlight": "#ED6800",
        },
    },
    3: {
        "name": "Orange Burst",
        "desc": "Dark blue nav, orange takes the lead as primary action color.",
        "colors": {
            "primary": "#05235B",
            "secondary": "#ED6800",
            "accent": "#ED6800",
            "highlight": "#FAB003",
        },
        "overrides": """
            a:hover, a:focus {
                color: #05235B !important;
            }
            a.badge.bg-primary:hover {
                background-color: #05235B !important;
                color: #fff !important;
            }
            a.badge.bg-secondary:hover {
                background-color: #05235B !important;
                color: #fff !important;
            }
        """,
    },
    4: {
        "name": "Golden Touch",
        "desc": "Muted blues with yellow as the standout accent — clean, editorial.",
        "colors": {
            "primary": "#05235B",
            "secondary": "#ED6800",
            "accent": "#FAB003",
            "highlight": "#0456AF",
        },
    },
}


@themes_bp.route("/")
def theme_index() -> str:
    """Gallery of available themes."""
    return render_template("themes.html", themes=THEMES)


def _theme_css(t: dict) -> str:
    """Generate CSS overrides for a theme.

    Design principles:
    1. Primary buttons = dark blue (#05235B) for ALL themes, not accent colors
    2. No orange-on-blue or blue-on-orange — hover swaps to neutral (white)
       or to dark blue with white text
    3. Outline variants for all scheme colors
    4. Success/warning/info relate to the 4 theme colors
    5. Dusty/pastel background variants for all colors
    """
    c = t["colors"]
    p = c["primary"]     # dark blue — always #05235B
    s = c["secondary"]   # accent color for links, badges, outlines
    a = c["accent"]       # button accent, navbar stripe
    h = c["highlight"]    # minor highlights

    # Dusty/pastel variants (8% tint for backgrounds)
    def _dusty(hex_color: str, pct: int = 8) -> str:
        return f"color-mix(in srgb, {hex_color} {pct}%, white)"

    return f"""
        :root {{
            --theme-primary: {p};
            --theme-secondary: {s};
            --theme-accent: {a};
            --theme-highlight: {h};
        }}

        /* ── Navbar ── */
        .navbar-dark.bg-dark {{
            background-color: {p} !important;
            border-top: 4px solid {a} !important;
        }}
        .navbar-brand {{ color: #fff !important; }}
        .navbar .nav-link {{ color: rgba(255,255,255,.85) !important; }}
        .navbar .nav-link:hover {{ color: #fff !important; }}

        /* ── Primary buttons = dark blue (all themes) ── */
        .btn-primary {{
            background-color: {p} !important;
            border-color: {p} !important;
            color: #fff !important;
        }}
        .btn-primary:hover {{
            background-color: {a} !important;
            border-color: {a} !important;
            color: #fff !important;
        }}
        .btn-outline-primary {{
            border-color: {p} !important;
            color: {p} !important;
        }}
        .btn-outline-primary:hover {{
            background-color: {p} !important;
            color: #fff !important;
        }}

        /* ── Secondary buttons = accent color (orange/yellow) ── */
        .btn-secondary {{
            background-color: {a} !important;
            border-color: {a} !important;
            color: #fff !important;
        }}
        .btn-secondary:hover {{
            background-color: {p} !important;
            border-color: {p} !important;
            color: #fff !important;
        }}
        .btn-outline-secondary {{
            border-color: {a} !important;
            color: {a} !important;
        }}
        .btn-outline-secondary:hover {{
            background-color: {a} !important;
            color: #fff !important;
        }}

        /* ── Outline variants for highlight colors ── */
        .btn-outline-accent {{
            border-color: {a} !important;
            color: {a} !important;
        }}
        .btn-outline-accent:hover {{
            background-color: {a} !important;
            color: #fff !important;
        }}
        .btn-outline-highlight {{
            border-color: {h} !important;
            color: {h} !important;
        }}
        .btn-outline-highlight:hover {{
            background-color: {h} !important;
            color: {p} !important;
        }}
        .btn-outline-primary-alt {{
            border-color: {s} !important;
            color: {s} !important;
        }}
        .btn-outline-primary-alt:hover {{
            background-color: {s} !important;
            color: #fff !important;
        }}

        /* ── Links: secondary color, hover to primary ── */
        a {{ color: {s}; }}
        a:hover {{ color: {p}; }}

        /* ── Badges ── */
        .badge.bg-primary {{
            background-color: {s} !important;
            color: #fff !important;
        }}
        .badge.bg-secondary {{
            background-color: {a} !important;
            color: #fff !important;
        }}
        a.badge.bg-primary:hover {{
            background-color: {p} !important;
            color: #fff !important;
        }}
        a.badge.bg-secondary:hover {{
            background-color: {p} !important;
            color: #fff !important;
        }}

        /* ── List groups, cards ── */
        .list-group-item-action:hover {{
            background-color: {_dusty(a)} !important;
        }}
        .card {{
            border-color: {_dusty(p, 20)} !important;
        }}

        /* ── Text & border helpers ── */
        .text-primary {{ color: {s} !important; }}
        .text-secondary {{ color: {a} !important; }}
        .border-primary {{ border-color: {a} !important; }}
        .bg-primary {{ background-color: {p} !important; }}

        /* ── Dashboard stat cards ── */
        .bg-soft-primary {{
            background-color: {_dusty(p)} !important;
        }}
        .bg-soft-secondary {{
            background-color: {_dusty(s)} !important;
        }}
        .bg-soft-accent {{
            background-color: {_dusty(a)} !important;
        }}
        .bg-soft-highlight {{
            background-color: {_dusty(h)} !important;
        }}

        /* ── Pagination ── */
        .page-link {{ color: {s}; }}
        .page-item.active .page-link {{
            background-color: {s};
            border-color: {s};
        }}

        /* ── Accordion ── */
        .accordion-button:not(.collapsed) {{
            background-color: {_dusty(s, 10)};
            color: {p};
        }}

        /* ── Dividers, blockquotes ── */
        hr {{
            border-color: {_dusty(h, 30)} !important;
        }}
        blockquote {{
            border-left-color: {a} !important;
        }}

        /* ── Alerts ── */
        .alert-primary {{
            background-color: {_dusty(p)} !important;
            border-color: {_dusty(p, 30)} !important;
            color: {p} !important;
        }}
        .alert-secondary {{
            background-color: {_dusty(s)} !important;
            border-color: {_dusty(s, 30)} !important;
            color: {p} !important;
        }}

        /* ── Progress bars ── */
        .progress-bar {{
            background-color: {s} !important;
        }}
        .progress-bar.bg-success {{
            background-color: {_dusty(a, 60)} !important;
        }}
        .progress-bar.bg-warning {{
            background-color: {h} !important;
        }}

        /* ── Tables ── */
        .table-striped > tbody > tr:nth-of-type(odd) {{
            background-color: {_dusty(p, 3)} !important;
        }}
        .table-hover > tbody > tr:hover {{
            background-color: {_dusty(h, 10)} !important;
        }}

        /* ── Misc ── */
        .btn-outline-light:hover {{
            background-color: {h} !important;
            border-color: {h} !important;
        }}
    """ + t.get("overrides", "")


@themes_bp.route("/<int:theme_id>")
def theme_preview(theme_id):
    """Render the front page with a theme applied."""
    theme = THEMES.get(theme_id)
    if not theme:
        return "Theme not found", 404

    session = get_session()
    articles = session.execute(
        select(Article).where(Article.status == "published")
        .order_by(desc(Article.published_at))
        .limit(20)
    ).scalars().all()

    # Brief 013: featured = 3 most recent published (mirrors front_page)
    featured = session.execute(
        select(Article).where(Article.status == "published")
        .order_by(desc(Article.published_at))
        .limit(3)
    ).scalars().all()

    tags = session.execute(select(Tag).order_by(Tag.name)).scalars().all()
    session.close()

    return render_template(
        "theme_preview.html",
        articles=articles,
        featured=featured,
        tags=tags,
        theme=theme,
        theme_css=_theme_css(theme),
    )
