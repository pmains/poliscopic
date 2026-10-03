"""Auth routes: login, logout."""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_bcrypt import check_password_hash as _check_pw
from flask_login import login_required, login_user, logout_user
from urllib.parse import urlsplit

from sqlalchemy import select

from db.core import session_scope
from db.newsroom import AdminUser

auth_bp = Blueprint("auth", __name__)


def _is_safe_next_url(target: str | None) -> bool:
    """Allow redirects only to absolute paths on this application."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return False
    parsed = urlsplit(target)
    return not parsed.scheme and not parsed.netloc


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> str:
    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    with session_scope() as session:
        user = session.execute(
            select(AdminUser).where(AdminUser.username == username)
        ).scalar_one_or_none()

    if user and _check_pw(user.password_hash, password):
        login_user(user)
        requested_next = request.args.get("next")
        next_page = requested_next if _is_safe_next_url(requested_next) else None
        return redirect(next_page or url_for("admin.dashboard"))

    flash("Invalid username or password.", "error")
    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout() -> str:
    logout_user()
    return redirect(url_for("auth.login"))
