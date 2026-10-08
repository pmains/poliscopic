#!/usr/bin/env python3
"""workflow-runner.py — Dumb multi-step workflow orchestrator.

Reads a workflow YAML definition, loops every 60s checking step states,
and delegates LLM-driven steps to isolated agentTurn sessions.
No cron dependency — a single nohup'd orchestrator.

Usage:
    nohup python3 workflows/workflow-runner.py newsletter \
        > data/runs/newsletter/router.log 2>&1 &

State contract:
    Each step reads the run directory, does its work, and writes:
        data/runs/<workflow>/<run-id>/steps/<step-name>.state
    {"status": "succeeded|failed|running", "started_at": "...", "finished_at": "...", "error": "..."}
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import yaml
from sqlalchemy import create_engine, text as sa_text

# ── Config ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
WORKFLOWS_DIR = PROJECT_ROOT / "workflows"
RUNS_DIR = PROJECT_ROOT / "data" / "runs"
POLL_INTERVAL_SEC = 5
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime(DATE_FORMAT)


def _ts_epoch() -> float:
    return time.time()


# ── Logging ──
def setup_logging(run_dir: Path, workflow_name: str, run_id: str) -> logging.Logger:
    logger = logging.getLogger(f"router-{run_id}")
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
    formatter.converter = time.gmtime

    # File handler
    log_path = run_dir / "router.log"
    fh = logging.FileHandler(log_path)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Stderr (caught by nohup's redirect)
    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    return logger


# ── JSONL Run Log ──
def log_event(run_log: Path, event: dict[str, Any]) -> None:
    """Append a JSONL event to the run log."""
    with open(run_log, "a") as f:
        f.write(json.dumps(event, default=str) + "\n")


# ── Workflow YAML Loading ──
def load_workflow(workflow_name: str) -> dict[str, Any]:
    path = WORKFLOWS_DIR / f"{workflow_name}.yaml"
    if not path.exists():
        print(f"Workflow file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        data = yaml.safe_load(f)
    if not data or "steps" not in data:
        print(f"Workflow {workflow_name} has no steps defined", file=sys.stderr)
        sys.exit(1)
    return data


# ── Run Directory Management ──
def get_or_create_run_dir(workflow_name: str, logger: logging.Logger,
                          force_new: bool = False) -> tuple[str, Path]:
    """Find today's existing run or create a new one."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    workflow_dir = RUNS_DIR / workflow_name
    workflow_dir.mkdir(parents=True, exist_ok=True)

    # Check for existing run today — match new format (date-prefixed) only
    # Old-format dirs like "boards-commissions-2026-07-21-..." are ignored.
    # force_new (--new-run) skips reuse so retrospective runs get a fresh dir.
    for entry in sorted(workflow_dir.iterdir(), reverse=True):
        if force_new:
            break
        if entry.is_dir() and entry.name.startswith(today) and "--" in entry.name:
            state_file = entry / "workflow.state"
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text())
                    if state.get("status") == "succeeded":
                        logger.info(f"Run {entry.name} already completed. Exiting.")
                        return entry.name, entry
                except (json.JSONDecodeError, IOError):
                    pass
            return entry.name, entry

    # Create new run
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    run_id = f"{today}--{stamp}"
    run_dir = workflow_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "steps").mkdir(exist_ok=True)

    # Write initial workflow state
    wf_state = {"status": "running", "started_at": _ts(), "workflow": workflow_name, "run_id": run_id}
    (run_dir / "workflow.state").write_text(json.dumps(wf_state, indent=2))

    # Create/update latest symlink
    latest_link = workflow_dir / "latest"
    if latest_link.exists() or latest_link.is_symlink():
        latest_link.unlink()
    latest_link.symlink_to(run_dir.name)

    return run_id, run_dir


# ── Step State ──
def read_step_state(state_file: Path, default: Optional[dict] = None) -> dict:
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return default or {}


def write_step_state(state_file: Path, state: dict) -> None:
    state_file.write_text(json.dumps(state, indent=2))


def step_should_run(state: dict) -> bool:
    """A step should run if it has no state or its state is not a terminal status."""
    status = state.get("status", "")
    return status not in ("succeeded", "failed")


def step_is_stuck_running(state: dict, timeout_min: int) -> bool:
    """A step is stuck if it's been 'running' past its timeout."""
    if state.get("status") != "running":
        return False
    started = state.get("started_at", "")
    if not started:
        return False
    try:
        started_dt = datetime.strptime(started, DATE_FORMAT).replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started_dt).total_seconds() / 60
        return elapsed >= timeout_min
    except ValueError:
        return False


def workflow_is_over_budget(run_dir: Path, max_run_minutes: int) -> bool:
    """Return whether a persisted run exceeded its cross-process budget."""
    try:
        state = json.loads((run_dir / "workflow.state").read_text())
        started = datetime.strptime(
            state.get("started_at", ""), DATE_FORMAT
        ).replace(tzinfo=timezone.utc)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    elapsed_minutes = (
        datetime.now(timezone.utc) - started
    ).total_seconds() / 60
    return elapsed_minutes >= max_run_minutes


# ── Retry Logic ──
def should_retry(state: dict, max_retries: int) -> bool:
    """Should we retry a failed step?"""
    if state.get("status") != "failed":
        return False
    retries = state.get("retries", 0)
    return retries < max_retries


# ── Step Context Helpers ──
def _load_env():
    """Load .env for secrets."""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _owner_emails_from_env() -> list[str]:
    """Return normalized operator addresses without embedding personal data."""
    _load_env()
    return [
        email.strip().lower()
        for email in os.environ.get("NEWSLETTER_OWNER_EMAILS", "").split(",")
        if email.strip()
    ]


def _get_db_engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        url = os.environ.get("DEV_DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL not set in .env")
    return create_engine(url)


# Optional window override for retrospective runs — set by main() from CLI
# args (--window-start / --window-end / --days-back). Empty = default
# today..today+13. Used by _gather_db_context().
WINDOW_OVERRIDE: dict[str, str] = {}


def _gather_db_context(logger: logging.Logger,
                       jurisdictions: list[str] | None = None) -> dict:
    """Fetch agenda items and data-gap candidates from the database.

    Returns structured context for the LLM to classify.

    Window follows PRODUCTION-CHECKLIST.md Step 1: meetings with
    meeting_date BETWEEN today AND today+13 (Phoenix local time). The
    returned ``gaps`` list holds meetings inside the window that have 0
    extracted agenda items — the checklist requires these to be captured,
    never silently dropped.
    """
    _load_env()
    try:
        engine = _get_db_engine()
    except ValueError as e:
        logger.warning(f"Database not available: {e}")
        return {"error": str(e), "items": [], "gaps": []}

    from datetime import timedelta
    from zoneinfo import ZoneInfo
    phx_now = datetime.now(ZoneInfo("America/Phoenix"))
    # Default window covers BOTH template sections: This Past Week
    # (today-7..today) and Coming Up (today..today+13). Overridable via CLI
    # (--window-start/--window-end/--days-back) for retrospective editions.
    today = WINDOW_OVERRIDE.get("start") or (phx_now - timedelta(days=7)).strftime("%Y-%m-%d")
    plus13 = WINDOW_OVERRIDE.get("end") or (phx_now + timedelta(days=13)).strftime("%Y-%m-%d")
    if WINDOW_OVERRIDE:
        logger.info(f"  Window override active: {today}..{plus13}")
    else:
        logger.info(f"  Window: {today}..{plus13} (past 7 days + next 13)")

    items_query = sa_text("""
        SELECT
            a.agenda_item_id,
            a.agenda_item_number,
            a.agenda_item_title,
            a.agenda_item_text,
            a.body,
            a.meeting_id,
            a.sort_order,
            m.meeting_date,
            m.meeting_type,
            m.meeting_title,
            m.source_url,
            j.name AS jurisdiction_name
        FROM agenda_items a
        JOIN meetings m ON a.body = m.body AND a.meeting_id = m.meeting_id
        LEFT JOIN jurisdictions j ON m.jurisdiction_id = j.id
        WHERE m.meeting_date BETWEEN :today AND :plus13{jur_clause}
        ORDER BY m.meeting_date DESC, a.body, a.agenda_item_number
    """)
    # Data-gap capture (PRODUCTION-CHECKLIST.md Step 1): meetings in the
    # window with 0 extracted agenda items (e.g. Phoenix Formal 8/26 & 9/9,
    # stale "complete" sync — see Brief 006). Never silently drop them.
    gaps_query = sa_text("""
        SELECT
            m.body,
            m.meeting_id,
            m.meeting_date,
            m.meeting_type,
            m.meeting_title,
            m.source_url,
            j.name AS jurisdiction_name
        FROM meetings m
        LEFT JOIN jurisdictions j ON m.jurisdiction_id = j.id
        WHERE m.meeting_date BETWEEN :today AND :plus13{jur_clause}
          AND NOT EXISTS (
              SELECT 1 FROM agenda_items a
              WHERE a.body = m.body AND a.meeting_id = m.meeting_id
          )
        ORDER BY m.meeting_date, m.body
    """)
    # Jurisdiction scoping (Pete 2026-09-17): the Boards & Commissions
    # digest is Maricopa County bodies ONLY.  Without a hard filter the
    # candidate pool spanned every city/town in the region, so the Monday
    # newsletter carried Chandler, Tempe, Tucson, MAG items, etc.
    jur_clause = ""
    params: dict = {"today": today, "plus13": plus13}
    if jurisdictions:
        jur_clause = " AND j.name = ANY(:jlist)"
        params["jlist"] = list(jurisdictions)
        logger.info(f"  Jurisdiction filter: {', '.join(jurisdictions)}")
    items_query = sa_text(items_query.text.format(jur_clause=jur_clause))
    gaps_query = sa_text(gaps_query.text.format(jur_clause=jur_clause))
    try:
        with engine.connect() as conn:
            rows = conn.execute(items_query, params).fetchall()
            grow = conn.execute(gaps_query, params).fetchall()
        items = [dict(row._mapping) for row in rows]
        gaps = []
        for row in grow:
            g = dict(row._mapping)
            # Subscriber-facing only (Pete directive 2026-08-26): §3 must
            # never carry internal diagnostics (brief numbers, sync state,
            # extraction internals). Those belong in the daily ops digest
            # (scripts/sync/sync_digest.py, Brief 007), not subscriber copy.
            g["gap_reason"] = (
                "No agenda items published yet — re-check after the next scrape"
            )
            gaps.append(g)
        logger.info(f"  DB: {len(items)} agenda items in window {today}..{plus13}; "
                    f"{len(gaps)} meetings with 0 extracted items (gaps)")
        return {
            "items": items,
            "gaps": gaps,
            "count": len(items),
            "date_range": f"{today} to {plus13}",
        }
    except Exception as e:
        logger.warning(f"Database query failed: {e}")
        return {"error": str(e), "items": [], "gaps": []}


def _gather_file_context(input_dir: Path, logger: logging.Logger) -> dict:
    """Read all files from the previous step's output directory."""
    context = {}
    if not input_dir.exists():
        logger.warning(f"  Input dir not found: {input_dir}")
        return {"error": f"Directory not found: {input_dir}", "files": {}}

    for fpath in sorted(input_dir.iterdir()):
        if fpath.is_file() and fpath.suffix in (".json", ".md", ".txt"):
            try:
                content = fpath.read_text()
                if fpath.suffix == ".json":
                    try:
                        context[fpath.stem] = json.loads(content)
                    except json.JSONDecodeError:
                        context[fpath.stem] = content
                else:
                    context[fpath.stem] = content
            except Exception as e:
                context[fpath.stem] = f"[error reading: {e}]"

    logger.info(f"  Read {len(context)} files from {input_dir.name}")
    return context


def _prescreen_embeddings(items: list[dict], logger: logging.Logger,
                           top_n: int = 50,
                           query: str = "") -> list[dict]:
    """Pre-screen agenda items by embedding similarity to a topic query.

    Generates embeddings for all item texts via local Ollama (nomic-embed-text),
    computes cosine similarity against the query, and returns only the top-N
    most relevant items. Falls back to all items on error.

    If *query* is empty, falls back to a generic housing/development query
    (for backwards compatibility).
    """
    if not items or len(items) <= top_n:
        return items

    if not query:
        query = (
            "Housing and development policy issues: rezoning, zoning map amendments, "
            "zoning code amendments, general plan amendments, affordable housing, "
            "ADUs, accessory dwelling units, multifamily housing, apartments, "
            "subdivision maps, lot splits, housing authority, development agreements, "
            "land use changes, planned area developments, residential development, "
            "housing trust funds, HOME funds, CDBG, housing moratoriums, "
            "use permits, variance requests, setback variances, garage conversions, "
            "special use permits, home-based businesses, transitional housing, "
            "homeless services, community development block grants, "
            "capital improvement plans, housing stability programs, "
            "abatement appeals, code enforcement"
        )

    # Build texts to embed: meeting_type + body + title (skip stripped full text)
    texts = [query] + [
        f"{item.get('meeting_type', '')} {item.get('body', '')} {item.get('agenda_item_title', '')} {item.get('agenda_item_text', '')[:1000]}"
        for item in items
    ]

    try:
        import requests
        resp = requests.post(
            "http://localhost:11434/api/embed",
            json={"model": "nomic-embed-text", "input": texts},
            timeout=120,
        )
        resp.raise_for_status()
        embeddings = resp.json()["embeddings"]

        if not embeddings or len(embeddings) != len(texts):
            raise ValueError(f"Expected {len(texts)} embeddings, got {len(embeddings)}")

        query_emb = np.array(embeddings[0])
        query_norm = np.linalg.norm(query_emb)

        scored = []
        for i, item in enumerate(items):
            item_emb = np.array(embeddings[i + 1])
            sim = np.dot(query_emb, item_emb) / (query_norm * np.linalg.norm(item_emb))
            scored.append((sim, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        filtered = [item for sim, item in scored[:top_n]]

        logger.info(f"  Embedding pre-screen: {len(items)} \u2192 {len(filtered)} items")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("  Top 10 similarity scores:")
            for sim, item in scored[:10]:
                title = item.get("agenda_item_title", "?")[:60]
                logger.debug(f"    {sim:.4f} {title}")

        return filtered

    except Exception as e:
        logger.warning(f"  Embedding pre-screen failed: {e}. Using all {len(items)} items.")
        return items





# ── Delegate to AgentTurn ──


def _delegate_step(
    workflow_name: str,
    step_name: str,
    run_dir: Path,
    prompt: str,
    output_dir: Path,
    state_file: Path,
    is_first_step: bool,
    timeout_min: int,
    logger: logging.Logger,
) -> None:
    """Write a job file and fire an agentTurn via the OpenClaw gateway.

    The step-executor.py script (running as an isolated agentTurn session)
    reads the job file, calls the LLM, writes results, and updates the step
    state file. The orchestrator's poll loop detects completion.
    """
    # 1. Write job file
    jobs_dir = run_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_file = jobs_dir / f"{step_name}-job.json"
    job_data = {
        "prompt": prompt,
        "step_name": step_name,
        "workflow": workflow_name,
        "run_id": run_dir.name,
        "output_dir": str(output_dir.resolve()) if output_dir else "",
        "state_file": str(state_file.resolve()),
        "is_first_step": is_first_step,
    }
    job_file.write_text(json.dumps(job_data, indent=2))
    logger.info(f"{step_name}: wrote job file to {job_file}")

    # 2. Launch step-executor DETACHED (set-and-forget chain — Brief 010).
    #    No cron agentTurn, no wrapper model call: the executor is a plain
    #    short-lived Python process that calls the LLM directly and hands
    #    back to the runner (--resume) when done. This removes the second
    #    cron/model-call surface that was timing out.
    project_root = str(PROJECT_ROOT.resolve())
    exec_log = run_dir / f"executor-{step_name}.log"
    with open(exec_log, "ab") as logf:
        proc = subprocess.Popen(
            [sys.executable, "-u", "workflows/step-executor.py",
             workflow_name, run_dir.name, step_name],
            cwd=project_root,
            stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    logger.info(f"{step_name}: step-executor launched detached (pid {proc.pid}, log {exec_log.name})")


# ── Post-Step Hooks ──
def _handle_send_step(run_dir: Path, output_dir: Path, logger: logging.Logger, run_log: Path, workflow_name: Optional[str] = None) -> None:
    """Post-step hook for the 'send' step: email the formatted newsletter."""
    result_file = output_dir / "send-llm-response.json"
    if not result_file.exists():
        logger.warning("send: no LLM response file found, trying send-output.json")
        result_file = output_dir / "send-output.json"
        if not result_file.exists():
            logger.warning("send: no output file found at all, skipping SMTP")
            return

    try:
        data = json.loads(result_file.read_text())
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"send: could not parse output: {e}")
        return

    html = ""
    subject = ""

    if isinstance(data, dict):
        html = data.get("html", data.get("response", ""))
        subject = data.get("subject", "")
        if not data.get("approved", True):
            # Verify found issues — log and continue with empty items
            verify_path = run_dir / "verified" / "verify-result.json"
            blocking = []
            if verify_path.exists():
                try:
                    vdata = json.loads(verify_path.read_text())
                    blocking = vdata.get("blocking_issues", [])
                except (json.JSONDecodeError, IOError):
                    pass
            rejection_reason = "; ".join(blocking) if blocking else "verify step rejected content"
            raise RuntimeError(
                f"send aborted: verify rejected content ({rejection_reason}) — DO NOT SEND"
            )
    elif isinstance(data, str):
        html = data

    if not html:
        logger.info("send: no HTML content to send")
        return

    # Save HTML for inspection — neutralize footer placeholders so the
    # artifact is readable; the send loop personalizes per recipient.
    html_path = run_dir / "newsletter.html"
    html_path.write_text(html
        .replace("__UNSUBSCRIBE_URL__", "https://poliscopic.com/newsletter")
        .replace("__MANAGE_URL__", "https://poliscopic.com/newsletter"))
    logger.info(f"send: HTML saved to {html_path}")

    # ── SMTP Send ──
    _load_env()
    smtp_pw = os.environ.get("EMAIL_APP_PASSWORD", "")
    if not smtp_pw:
        logger.warning("send: EMAIL_APP_PASSWORD not set — skipping send")
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    topic = workflow_name or ""

    # ── Recipients: active DB subscribers + owner fallbacks ────────────
    # Subscribers live in the site DB.  When PROD_DATABASE_URL is set (real
    # scheduled sends) recipients come from there; otherwise (local test
    # runs) the default engine is used and only owner emails are returned
    # when the DB has no rows.  NEWSLETTER_SEND_OWNER_ONLY=1 forces owner-
    # only delivery (safe for --new-run smoke tests).
    recipients: list[dict] = []
    owner_only = os.environ.get("NEWSLETTER_SEND_OWNER_ONLY", "").lower() in ("1", "true", "yes")
    # newsletter_svc lives in scripts/ but the runner only puts the project
    # root on sys.path — without this the import fails and EVERY digest
    # silently falls back to owner-only delivery (found 2026-09-17).
    _scripts_dir = str(PROJECT_ROOT / "scripts")
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    try:
        from newsletter_svc import active_recipients, owner_emails
        if owner_only:
            recipients = [{"email": e, "topic": topic} for e in owner_emails()]
        else:
            prod_url = os.environ.get("PROD_DATABASE_URL", "")
            recipients = active_recipients(topic, engine_url=prod_url or None)
    except Exception as e:
        logger.error(
            f"send: recipient lookup failed ({e}) — owner fallback; "
            "SUBSCRIBERS WILL NOT RECEIVE THIS DIGEST"
        )
        recipients = [
            {"email": email, "topic": topic}
            for email in _owner_emails_from_env()
        ]

    if not recipients:
        logger.warning("send: no recipients — nothing sent")
        return

    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    for r in recipients:
        recipient = r["email"]
        # Per-recipient signed unsubscribe/manage URLs in the footer.
        personal = html
        try:
            from newsletter_svc import make_action_url
            if "__UNSUBSCRIBE_URL__" in personal:
                personal = personal.replace("__UNSUBSCRIBE_URL__",
                    make_action_url(recipient, "unsubscribe", topic=topic or None))
            if "__MANAGE_URL__" in personal:
                personal = personal.replace("__MANAGE_URL__",
                    make_action_url(recipient, "manage"))
        except Exception as e:
            logger.error(f"send: footer URL failed for {recipient}: {e}")
        # Drop any remaining placeholders (shouldn't happen).
        personal = (personal.replace("__UNSUBSCRIBE_URL__", "https://poliscopic.com/newsletter")
                            .replace("__MANAGE_URL__", "https://poliscopic.com/newsletter"))

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject or f"Poliscopic: Maricopa County Governance Digest — {today}"
        msg["From"] = "Poliscopic <contact@poliscopic.com>"
        msg["To"] = recipient

        text_part = MIMEText("View this email in HTML mode.", "plain")
        html_part = MIMEText(personal, "html")
        msg.attach(text_part)
        msg.attach(html_part)

        try:
            import smtplib
            with smtplib.SMTP("mail.privateemail.com", 587) as server:
                server.starttls()
                server.login("contact@poliscopic.com", smtp_pw)
                server.send_message(msg)
            logger.info(f"send: emailed to {recipient}")
            log_event(run_log, {"ts": _ts(), "event": "email_sent", "to": recipient, "subject": msg["Subject"]})
        except Exception as e:
            logger.error(f"send: failed to email {recipient}: {e}")
            raise

    # ── Write idempotency marker ──
    wf = workflow_name or "unknown"
    idempotency_path = PROJECT_ROOT / "data" / "runs" / wf / "sent.idempotency"
    idempotency_path.parent.mkdir(parents=True, exist_ok=True)
    idempotency_path.write_text(f"{wf}:{today}\n")
    logger.info("send: idempotency marker written")


# ── Publish Step (Deterministic Single-Article Publication) ──


class StepFailure(RuntimeError):
    """A step produced a TYPED failure result and must not be recorded as success.

    The publish handler used to write ``{"status": "failed"}`` to
    ``publish-result.json`` and then RETURN, so the step wrapper recorded
    ``succeeded``: no retry, no failure email, and the workflow was left
    ``running`` forever. Raising instead routes the failure into the existing
    exception path, which marks the step failed, makes it retry eligible, and lets
    ``any_step_failed`` fail the whole workflow.
    """

    def __init__(self, message: str, result: Optional[dict] = None) -> None:
        super().__init__(message)
        self.result: dict = result or {}


class PublishResultError(ValueError):
    """A publish-result artifact is missing, malformed, or internally inconsistent."""


PUBLISH_TERMINAL = ("succeeded", "failed", "skipped")


def write_publish_result(output_dir: Optional[Path], result: dict) -> Optional[Path]:
    """Persist the typed publish result. Written BEFORE any raise, so the
    successful development publication is preserved as evidence even when the
    production push fails."""
    if output_dir is None:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "publish-result.json"
    path.write_text(json.dumps(result, indent=2))
    return path


def read_publish_result(run_dir: Path) -> dict:
    """Read and VALIDATE the publish result for a run.

    Downstream consumers must use this rather than trusting the step state: the
    two disagreed for days. Raises :class:`PublishResultError` on a missing,
    malformed or inconsistent artifact, so a caller can never mistake absence for
    success.
    """
    path = Path(run_dir) / "publish-output" / "publish-result.json"
    if not path.is_file():
        raise PublishResultError(f"no publish result at {path}")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise PublishResultError(f"publish result is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise PublishResultError("publish result is not a JSON object")
    status = data.get("status")
    if status not in PUBLISH_TERMINAL:
        raise PublishResultError(f"publish result has no terminal status: {status!r}")
    push = data.get("production_push")
    if status == "succeeded":
        if not isinstance(push, dict) or push.get("status") != "succeeded":
            raise PublishResultError(
                "publish result claims success without a successful production push")
    if status == "failed" and not data.get("reason"):
        raise PublishResultError("publish result is failed but carries no reason")
    return data


def _handle_publish_step(
    workflow_name: str,
    run_dir: Path,
    input_dir: Optional[Path],
    output_dir: Optional[Path],
    logger: logging.Logger,
) -> None:
    """Deterministic publish step: create ONE article per newsletter run.

    Reads verify-result.json from the verified dir. If approved with items,
    runs scripts/publish_newsletter_article.py (creates the single article
    in dev) and scripts/editorial_sync.py (pushes editorial tables to prod).
    Writes publish-result.json to the output dir, then RAISES on any failure so the
    step (and the workflow) cannot be recorded as successful.
    """
    result: dict = {"status": "skipped", "reason": "no verified output",
                    "dev_publication": None, "production_push": None}
    verify_path = input_dir / "verify-result.json" if input_dir else None
    if verify_path and verify_path.exists():
        try:
            vdata = json.loads(verify_path.read_text())
        except (json.JSONDecodeError, IOError):
            vdata = {}
        approved = vdata.get("approved", vdata.get("status") == "approved")
        items = vdata.get("items", [])
        if not approved:
            result = {**result, "reason": "run not approved"}
        elif not items:
            result = {**result, "reason": "no items — nothing to publish"}
        else:
            # 1. Create the single article in dev (idempotent by slug)
            publish_cmd = [
                sys.executable, "-u", "scripts/publish_newsletter_article.py",
                workflow_name, "--run-id", run_dir.name,
            ]
            p = subprocess.run(publish_cmd, cwd=str(PROJECT_ROOT.resolve()),
                               capture_output=True, text=True, timeout=300)
            logger.info(f"publish: {p.stdout.strip()}")
            dev_ok = p.returncode == 0
            result["dev_publication"] = {
                "status": "succeeded" if dev_ok else "failed",
                "returncode": p.returncode,
                "stdout_tail": p.stdout.strip()[-2000:],
                "reason": None if dev_ok else p.stderr.strip()[-500:],
            }
            if not dev_ok:
                # No production attempt without a development article: publishing
                # nothing to production would desync the two databases.
                result["status"] = "failed"
                result["reason"] = result["dev_publication"]["reason"]
                result["production_push"] = {
                    "status": "not_attempted",
                    "reason": "development publication failed",
                }
            else:
                # 2. Sync editorial tables to prod. ONE attempt per invocation: a
                # refusal here is terminal, never retried against production.
                sync_cmd = [sys.executable, "-u", "scripts/editorial_sync.py"]
                s = subprocess.run(sync_cmd, cwd=str(PROJECT_ROOT.resolve()),
                                   capture_output=True, text=True, timeout=900)
                push_ok = s.returncode == 0
                result["production_push"] = {
                    "status": "succeeded" if push_ok else "refused_or_failed",
                    "returncode": s.returncode,
                    "reason": None if push_ok else (
                        s.stderr.strip()[-800:] or s.stdout.strip()[-800:]),
                }
                result["status"] = "succeeded" if push_ok else "failed"
                result["reason"] = result["production_push"]["reason"]
    write_publish_result(output_dir, result)
    logger.info(f"publish: {result}")
    if result["status"] == "failed":
        failed_phase = ("development publication" if result.get("dev_publication")
                        and result["dev_publication"].get("status") == "failed"
                        else "production editorial sync")
        raise StepFailure(
            f"publish: {failed_phase} failed/refused "
            f"({str(result.get('reason'))[:300]})", result)
    if result["status"] == "skipped":
        logger.info(f"publish: skipped ({result.get('reason')})")


# ── Enrich Step (Deterministic DB Enrichment) ──
def _handle_enrich_step(
    input_dir: Path,
    output_dir: Path,
    logger: logging.Logger,
    step_name: str = "enrich",
) -> None:
    """Enrich classified items with full text + supporting documents from DB.

    This is a deterministic step — no LLM call. Reads classified topic files,
    looks up full agenda item text and supporting documents from the database,
    and writes enriched topic files for the summarize step.
    """
    _load_env()
    try:
        engine = _get_db_engine()
    except ValueError as e:
        logger.warning(f"{step_name}: database not available: {e}")
        # Copy input dir as-is if no DB
        import shutil
        for f in sorted(input_dir.iterdir()):
            if f.is_file():
                shutil.copy2(f, output_dir / f.name)
        return

    # Read all classified topic files
    topic_files = sorted(input_dir.glob("*.json"))
    if not topic_files:
        logger.warning(f"{step_name}: no topic files found in {input_dir}")
        return

    logger.info(f"{step_name}: found {len(topic_files)} topic files to enrich")

    from sqlalchemy import text as sa_text

    total_items = 0
    total_docs = 0

    for tfile in topic_files:
        try:
            topic_data = json.loads(tfile.read_text())
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"{step_name}: cannot read {tfile.name}: {e}")
            continue

        if not isinstance(topic_data, dict):
            logger.warning(
                f"{step_name}: {tfile.name} is not a JSON object "
                f"({type(topic_data).__name__}) — passing through unchanged"
            )
            enriched_path = output_dir / tfile.name
            with open(enriched_path, "w") as f:
                json.dump(topic_data, f, indent=2)
            continue

        items = topic_data.get("items", [])
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            # Not a classified-items file (e.g. a stray key the LLM emitted,
            # such as {"type": "json_object"}) — never iterate it as items.
            logger.warning(
                f"{step_name}: {tfile.name} has non-list items "
                f"({type(items).__name__}) — passing through unchanged"
            )
            enriched_path = output_dir / tfile.name
            with open(enriched_path, "w") as f:
                json.dump(topic_data, f, indent=2)
            continue

        if not items:
            # Pass through unchanged
            enriched_path = output_dir / tfile.name
            with open(enriched_path, "w") as f:
                json.dump(topic_data, f, indent=2)
            continue

        # Build lookup keys for DB queries
        meeting_bodies = set()
        for item in items:
            b = item.get("body", "")
            m = item.get("meeting_id", "")
            n = item.get("agenda_item_number", "")
            if b and m and n:
                meeting_bodies.add((b, m, n))

        if not meeting_bodies:
            # Pass through unchanged
            enriched_path = output_dir / tfile.name
            with open(enriched_path, "w") as f:
                json.dump(topic_data, f, indent=2)
            continue

        # Fetch full text from agenda_items
        text_lookup = {}
        doc_lookup = {}
        try:
            with engine.connect() as conn:
                # Build dynamic params for tuple IN
                params = {}
                clauses = []
                for i, (b, m, n) in enumerate(meeting_bodies):
                    pb = f"b{i}"
                    pm = f"m{i}"
                    pn = f"n{i}"
                    clauses.append(f"(body = :{pb} AND meeting_id = :{pm} AND agenda_item_number = :{pn})")
                    params[pb] = b
                    params[pm] = m
                    params[pn] = n
                where_clause = " OR ".join(clauses)

                # Fetch agenda item text
                rows = conn.execute(sa_text(f"""
                    SELECT body, meeting_id, agenda_item_number,
                           agenda_item_text, source_url
                    FROM agenda_items
                    WHERE {where_clause}
                """), params).fetchall()
                for row in rows:
                    key = (str(row.body), str(row.meeting_id), str(row.agenda_item_number))
                    text_lookup[key] = {
                        "agenda_item_text": row.agenda_item_text or "",
                        "source_url": row.source_url or "",
                    }

                # Fetch supporting documents
                doc_body_meetings = set()
                for b, m, n in meeting_bodies:
                    doc_body_meetings.add((b, m))
                doc_params = {}
                doc_clauses = []
                for i, (b, m) in enumerate(doc_body_meetings):
                    pb = f"db{i}"
                    pm = f"dm{i}"
                    doc_clauses.append(f"(body = :{pb} AND meeting_id = :{pm})")
                    doc_params[pb] = b
                    doc_params[pm] = m
                if doc_clauses:
                    doc_where = " OR ".join(doc_clauses)
                    drows = conn.execute(sa_text(f"""
                        SELECT body, meeting_id, agenda_item_number,
                               document_title, document_url,
                               LEFT(text_content, 5000) as text_snippet
                        FROM supporting_documents
                        WHERE {doc_where}
                    """), doc_params).fetchall()
                    for row in drows:
                        key = (str(row.body), str(row.meeting_id), str(row.agenda_item_number))
                        if key not in doc_lookup:
                            doc_lookup[key] = []
                        doc_lookup[key].append({
                            "title": row.document_title or "",
                            "url": row.document_url or "",
                            "text": row.text_snippet or "",
                        })

            logger.info(f"{step_name}: enriched {len(meeting_bodies)} items from {tfile.name}")
        except Exception as e:
            logger.warning(f"{step_name}: DB enrichment failed for {tfile.name}: {e}")
            # Pass through unchanged
            enriched_path = output_dir / tfile.name
            with open(enriched_path, "w") as f:
                json.dump(topic_data, f, indent=2)
            continue

        # Attach enrichment to items
        enriched_items = []
        for item in items:
            key = (str(item.get("body", "")), str(item.get("meeting_id", "")), str(item.get("agenda_item_number", "")))
            if key in text_lookup:
                item["agenda_item_text"] = text_lookup[key]["agenda_item_text"]
                item["source_url"] = text_lookup[key]["source_url"]
            if key in doc_lookup:
                item["supporting_documents"] = doc_lookup[key]
                total_docs += len(doc_lookup[key])
            enriched_items.append(item)
            total_items += 1

        # Write enriched topic file
        topic_data["items"] = enriched_items
        enriched_path = output_dir / tfile.name
        with open(enriched_path, "w") as f:
            json.dump(topic_data, f, indent=2)
        logger.info(f"{step_name}: wrote enriched {tfile.name} ({len(enriched_items)} items)")

    logger.info(f"{step_name}: enriched {total_items} items with {total_docs} supporting docs")


# ── Execute a Step ──
def run_step(
    workflow_name: str,
    step: dict,
    run_dir: Path,
    run_log: Path,
    logger: logging.Logger,
    embedding_query: str = "",
    jurisdictions: list[str] | None = None,
) -> None:
    """Execute a single workflow step.

    Universal executor: reads the step's instructions.md, gathers context
    (from DB for first steps, from previous step's output for others),
    calls the LLM, and writes the response to the output directory.

    The router handles all boilerplate — the LLM provides the intelligence.
    No step-specific code needed.
    """
    step_name = step["name"]
    instruction_path = step.get("instructions", "")
    timeout_min = step.get("timeout_min", 30)
    max_retries = step.get("max_retries", 2)
    input_dir_raw = step.get("input_dir", "")
    output_dir_raw = step.get("output_dir", "")

    state_file = run_dir / "steps" / f"{step_name}.state"
    current_state = read_step_state(state_file)

    def resolve(value: str) -> str:
        return value.replace("{RUN_DIR}", str(run_dir))
    resolved_instructions = resolve(instruction_path)
    input_dir = Path(resolve(input_dir_raw)) if input_dir_raw else None
    output_dir = Path(resolve(output_dir_raw)) if output_dir_raw else None

    # Already done?
    if current_state.get("status") == "succeeded":
        logger.info(f"Step {step_name}: already succeeded")
        return

    # Retry?
    retry_count = current_state.get("retries", 0)
    if current_state.get("status") == "failed" and should_retry(current_state, max_retries):
        retry_count += 1
        logger.info(f"Step {step_name}: retrying (attempt {retry_count + 1}/{max_retries + 1})")
        log_event(run_log, {"ts": _ts(), "event": "step_retry", "step": step_name, "attempt": retry_count + 1})

    # Stuck check
    if step_is_stuck_running(current_state, timeout_min):
        logger.error(f"Step {step_name}: timed out")
        write_step_state(state_file, {
            "status": "failed", "error": f"stuck running after {timeout_min}m",
            "finished_at": _ts(), "retries": retry_count,
        })
        log_event(run_log, {"ts": _ts(), "event": "step_timeout", "step": step_name, "timeout_min": timeout_min})
        return

    if current_state.get("status") == "running":
        logger.info(f"Step {step_name}: still running (previous attempt)")
        return

    # ── Fire the step ──
    logger.info(f"Step {step_name}: starting")

    write_step_state(state_file, {
        "status": "running", "started_at": _ts(), "retries": retry_count,
    })
    log_event(run_log, {"ts": _ts(), "event": "step_started", "step": step_name})

    try:
        # 1. Read instructions
        instructions_text = ""
        if resolved_instructions and Path(resolved_instructions).exists():
            instructions_text = Path(resolved_instructions).read_text()
        else:
            raise FileNotFoundError(f"Instructions not found: {resolved_instructions}")

        # 2. Carry forward input files (so next step has the full context)
        is_first_step = (input_dir is None)
        if not is_first_step and output_dir and input_dir:
            import shutil
            output_dir.mkdir(parents=True, exist_ok=True)
            for f in sorted(input_dir.iterdir()):
                if f.is_file():
                    shutil.copy2(f, output_dir / f.name)
            logger.info(f"{step_name}: carried {len(list(input_dir.iterdir()))} files from {input_dir.name}")

        # PRODUCTION-CHECKLIST.md Step 0: every run starts by reading the
        # checklist; it is pasted into the first-step (classify) context.
        checklist_text = ""
        if is_first_step:
            logger.info("reading PRODUCTION-CHECKLIST.md")
            checklist_path = PROJECT_ROOT / "docs" / "newsletter" / "PRODUCTION-CHECKLIST.md"
            if checklist_path.exists():
                checklist_text = checklist_path.read_text()
            else:
                logger.warning("PRODUCTION-CHECKLIST.md not found — checklist context omitted")

        if is_first_step:
            # First step: gather data from the database
            logger.info(f"{step_name}: gathering database context...")
            context = _gather_db_context(logger, jurisdictions=jurisdictions)
            # Truncate full text to 500 chars for classify (preserve key content)
            for item in context.get("items", []):
                if "agenda_item_text" in item and item["agenda_item_text"]:
                    item["agenda_item_text"] = item["agenda_item_text"][:2000]

            # Embedding pre-screen: semantic filter (nomic-embed-text)
            items = _prescreen_embeddings(context.get("items", []), logger,
                                          top_n=200, query=embedding_query)
            context["items"] = items
            context["count"] = len(items)

            # Fall through to prompt-building + DeepSeek delegation
            # (No Qwen binary filter — the 3B model had too many false negatives on
            # mixed-content items like USE PERMITS with entertainment + housing subtopics.
            # DeepSeek handles 150 items easily with its 1M context window.)
            logger.info(f"{step_name}: {len(items)} items from embedding pre-screen, delegating DeepSeek classify...")
            context_label = "Agenda Items to Classify"

            # Data-gap capture (PRODUCTION-CHECKLIST.md Step 1): meetings in
            # the window with 0 extracted items. Written deterministically —
            # the LLM never decides whether a gap exists.
            if output_dir:
                output_dir.mkdir(parents=True, exist_ok=True)
                gaps_file = output_dir / "gaps.json"
                gaps = context.get("gaps", [])
                gaps_file.write_text(json.dumps({
                    "topic": "gaps",
                    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "items": gaps,
                }, indent=2))
                logger.info(f"{step_name}: wrote {len(gaps)} data-gap entr{'y' if len(gaps) == 1 else 'ies'} to {gaps_file.name}")

        elif input_dir and input_dir.exists():
            # Subsequent step: read previous step's output
            logger.info(f"{step_name}: reading input from {input_dir}")
            context = _gather_file_context(input_dir, logger)
            context_label = "Previous Step Output"
        else:
            context = "No input available."
            context_label = "Input"

        # 3. Build prompt and call LLM
        checklist_section = ""
        if is_first_step and checklist_text:
            checklist_section = (
                "\n# Production Checklist (MANDATORY — read before responding)\n\n"
                f"{checklist_text}\n"
            )

        prompt = f"""# Instructions

{instructions_text}
{checklist_section}
# {context_label}

{json.dumps(context, indent=2, default=str)}

# Response Format

Respond with a valid JSON object following the format described in the instructions above.
"""

        # ── Template-based send (deterministic, no LLM) ──
        if step_name == "send" and input_dir:
            logger.info(f"{step_name}: rendering template...")
            try:
                from workflows.templates.render import render_from_verified
                # Prefer the curated display_name from the workflow YAML
                # (e.g. "Water & Environment Weekly Digest"); fall back to the
                # old mechanical derivation from the workflow name.
                _wf_name = workflow_name
                _display = _wf_name.replace("-", " ").title()
                try:
                    _wf = load_workflow(_wf_name)
                    _display = _wf.get("display_name") or _display
                except SystemExit:
                    pass  # load_workflow exits if missing; keep fallback
                rendered = render_from_verified(
                    verified_dir=input_dir,
                    topic_key=_wf_name,
                    topic_display_name=_display,
                )
                html = ""
                subject = ""
                if rendered:
                    html, subject = rendered
                    output_dir.mkdir(parents=True, exist_ok=True)
                    result = {"html": html, "subject": subject, "approved": True}
                    result_path = output_dir / "send-result.json"
                    with open(result_path, "w") as f:
                        json.dump(result, f, indent=2)
                    logger.info(f"{step_name}: rendered {len(html)} bytes to {result_path}")
                else:
                    result = {"approved": False, "reason": "No approved content"}
                    result_path = output_dir / "send-result.json"
                    with open(result_path, "w") as f:
                        json.dump(result, f, indent=2)
                    logger.warning(f"{step_name}: no approved content to render")
                elapsed = 0.1
                llm_output = result if html else {}
            except Exception as e:
                logger.exception(f"{step_name}: template render failed: {e}")
                raise
        # ── Enrich step: deterministic, no LLM ──
        elif step_name == "enrich":
            logger.info(f"{step_name}: enriching items with full text + supporting docs...")
            _handle_enrich_step(input_dir, output_dir, logger, step_name)
            elapsed = 0.1

        # ── Publish step: deterministic, no LLM ──
        elif step_name == "publish":
            logger.info(f"{step_name}: publishing single newsletter article...")
            _handle_publish_step(workflow_name, run_dir, input_dir, output_dir, logger)
            elapsed = 0.1

        # ── LLM steps: delegate to isolated agentTurn ──
        else:
            logger.info(f"{step_name}: delegating to agentTurn...")
            _delegate_step(workflow_name, step_name, run_dir, prompt, output_dir, state_file, is_first_step, timeout_min, logger)
            # Delegate writes step state on completion — poll loop detects it
            return

        # 4. Write output (inline steps only — LLM steps return above)
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)

            if step_name == "enrich":
                logger.info(f"{step_name}: output written by enrich handler")
            elif step_name == "publish":
                logger.info(f"{step_name}: output written by publish handler")
            elif step_name == "send":
                llm_output = result
                if isinstance(llm_output, dict):
                    output_path = output_dir / f"{step_name}-result.json"
                    with open(output_path, "w") as f:
                        json.dump(llm_output, f, indent=2)
                    logger.info(f"{step_name}: wrote to {output_path}")
                # Always write a copy for debugging
                raw_path = output_dir / f"{step_name}-llm-response.json"
                with open(raw_path, "w") as f:
                    json.dump(llm_output, f, indent=2, default=str)

        logger.info(f"Step {step_name}: succeeded ({elapsed:.1f}s)")
        write_step_state(state_file, {
            "status": "succeeded", "finished_at": _ts(), "duration_s": round(elapsed, 1),
        })
        log_event(run_log, {"ts": _ts(), "event": "step_completed", "step": step_name, "status": "succeeded", "duration_s": round(elapsed, 1)})

        # ── Post-step hooks ──
        if step_name == "send" and output_dir:
            _handle_send_step(run_dir, output_dir, logger, run_log, workflow_name=workflow_name)

    except Exception as e:
        logger.exception(f"Step {step_name}: failed: {e}")

        if retry_count < max_retries:
            logger.warning(f"{step_name}: will retry ({retry_count + 1}/{max_retries})")
            write_step_state(state_file, {
                "status": "failed", "error": str(e),
                "finished_at": _ts(), "retries": retry_count + 1,
            })
            log_event(run_log, {"ts": _ts(), "event": "step_failed", "step": step_name, "error": str(e), "retry": True})
        else:
            logger.error(f"{step_name}: no retries remaining")
            write_step_state(state_file, {
                "status": "failed", "error": str(e),
                "finished_at": _ts(), "retries": retry_count,
            })
            log_event(run_log, {"ts": _ts(), "event": "step_failed", "step": step_name, "error": str(e), "retry": False})


# ── Workflow Logic ──
def get_workflow_status(run_dir: Path) -> str:
    """Return 'running', 'succeeded', or 'failed' for the overall workflow."""
    wf_state_file = run_dir / "workflow.state"
    if wf_state_file.exists():
        try:
            return json.loads(wf_state_file.read_text()).get("status", "running")
        except (json.JSONDecodeError, IOError):
            return "running"
    return "running"


def all_steps_done(workflow: dict, run_dir: Path) -> bool:
    """Check if all workflow steps have succeeded."""
    for step in workflow["steps"]:
        state_file = run_dir / "steps" / f"{step['name']}.state"
        state = read_step_state(state_file)
        if state.get("status") != "succeeded":
            return False
    return True


def any_step_failed(workflow: dict, run_dir: Path) -> tuple[bool, Optional[str]]:
    """Check if any step has reached a terminal failure (retries exhausted)."""
    max_retries_default = 2
    for step in workflow["steps"]:
        state_file = run_dir / "steps" / f"{step['name']}.state"
        state = read_step_state(state_file)
        if state.get("status") == "failed":
            max_r = step.get("max_retries", max_retries_default)
            retries = state.get("retries", 0)
            if retries >= max_r:
                return True, step["name"]
    return False, None


def verify_gate_passed(run_dir: Path) -> tuple[bool, list[str]]:
    """Hard gate (PRODUCTION-CHECKLIST.md Step 5).

    Once the verify step has produced a verdict, it must approve. If the
    verdict is missing, rejected, or unreadable, the gate fails and the run
    must not send. A missing verdict file means the verify step has not
    finished yet — the gate does not apply (pass).
    """
    result_file = run_dir / "verified" / "verify-result.json"
    if not result_file.exists():
        # Verify step finished but wrote no verdict → the gate cannot pass.
        verify_state = run_dir / "steps" / "verify.state"
        if verify_state.exists():
            try:
                st = json.loads(verify_state.read_text())
                if st.get("status") == "succeeded":
                    return False, ["verify step succeeded but wrote no verify-result.json"]
            except (json.JSONDecodeError, IOError):
                pass
        return True, []
    try:
        data = json.loads(result_file.read_text())
    except (json.JSONDecodeError, IOError) as e:
        return False, [f"verify-result.json unreadable: {e}"]
    approved = data.get("approved")
    if approved is None:
        approved = data.get("status") == "approved"
    blocking = data.get("blocking_issues", []) or []
    if not approved:
        if not blocking:
            blocking = ["verify step rejected content (no blocking issues listed)"]
        return False, blocking
    return True, []


def find_next_step(workflow: dict, run_dir: Path) -> Optional[dict]:
    """
    Find the first step that's ready to run.

    A step is ready if:
    1. All steps before it are 'succeeded'
    2. It's not already 'succeeded' or currently 'running'
    3. It hasn't exhausted its retries
    """
    max_retries_default = 2
    for i, step in enumerate(workflow["steps"]):
        state_file = run_dir / "steps" / f"{step['name']}.state"
        state = read_step_state(state_file)

        # Check predecessors
        predecessors_succeeded = True
        for j in range(i):
            prev_state_file = run_dir / "steps" / f"{workflow['steps'][j]['name']}.state"
            prev_state = read_step_state(prev_state_file)
            if prev_state.get("status") != "succeeded":
                predecessors_succeeded = False
                break

        if not predecessors_succeeded:
            continue

        # Check this step
        if state.get("status") == "succeeded":
            continue
        if state.get("status") == "running":
            continue

        max_r = step.get("max_retries", max_retries_default)
        if state.get("status") == "failed" and state.get("retries", 0) >= max_r:
            continue

        return step

    return None


def send_failure_email(workflow_name: str, run_id: str, run_dir: Path, logger: logging.Logger) -> None:
    """Send a failure notification email via SMTP."""
    try:
        recipients = _owner_emails_from_env()
        if not recipients:
            logger.warning(
                "NEWSLETTER_OWNER_EMAILS not set — cannot send failure email"
            )
            return
        wf_state = json.loads((run_dir / "workflow.state").read_text())
        steps_dir = run_dir / "steps"
        step_details = []
        for sf in sorted(steps_dir.glob("*.state")):
            step_details.append(f"  {sf.stem}: {sf.read_text().strip()}")

        body = (
            f"Workflow: {workflow_name}\n"
            f"Run ID: {run_id}\n"
            f"Status: {wf_state.get('status', 'unknown')}\n"
            f"Error: {wf_state.get('error', 'unknown')}\n\n"
            f"Steps:\n" + "\n".join(step_details) + "\n\n"
            f"Run directory: {run_dir}\n"
        )

        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(body)
        msg["Subject"] = f"WORKFLOW FAILED: {workflow_name}/{run_id}"
        msg["From"] = "Poliscopic <contact@poliscopic.com>"
        msg["To"] = ", ".join(recipients)

        smtp_pw = os.environ.get("EMAIL_APP_PASSWORD", "")

        if smtp_pw:
            with smtplib.SMTP("mail.privateemail.com", 587) as server:
                server.starttls()
                server.login("contact@poliscopic.com", smtp_pw)
                server.send_message(msg)
            logger.info("Failure email sent to configured owner recipients")
        else:
            logger.warning("EMAIL_APP_PASSWORD not found in .env — cannot send failure email")

    except Exception as e:
        logger.warning(f"Failed to send failure email: {e}")


# ── Main Loop ──
def main():
    import argparse
    parser = argparse.ArgumentParser(
        prog="workflow-runner.py",
        description="Run a newsletter workflow. Window defaults to today..today+13; "
                    "pass --window-start/--window-end or --days-back for a past "
                    "range (retrospective editions), and --new-run for a fresh run dir.",
    )
    parser.add_argument("workflow_name", help="topic name, e.g. housing")
    parser.add_argument("--window-start", help="YYYY-MM-DD (inclusive) — override window start")
    parser.add_argument("--window-end", help="YYYY-MM-DD (inclusive) — override window end")
    parser.add_argument("--days-back", type=int,
                        help="retrospective: window = (today-N)..today")
    parser.add_argument("--new-run", action="store_true",
                        help="create a fresh run dir instead of reusing today's")
    parser.add_argument("--owner-only", action="store_true",
                        help="send only to owner emails (safe for smoke tests; "
                             "never to DB subscribers)")
    parser.add_argument("--advance", action="store_true",
                        help="advance an existing run (set-and-forget chain handoff)")
    parser.add_argument("run_id_arg", nargs="?", help="run-id (with --advance)")
    args = parser.parse_args()

    workflow_name = args.workflow_name
    if args.owner_only:
        os.environ["NEWSLETTER_SEND_OWNER_ONLY"] = "1"
    logger = logging.getLogger("router-init")

    # Load workflow
    workflow = load_workflow(workflow_name)

    # ── Advance mode (set-and-forget chain handoff) ──
    # The step-executor calls us back after each step; we do ONE bounded
    # advance (run deterministic steps inline, fire the next LLM executor
    # detached) and exit. No poll loop anywhere.
    if args.advance:
        if not args.run_id_arg:
            print("--advance requires a run-id", file=sys.stderr)
            sys.exit(1)
        run_id = args.run_id_arg
        run_dir = RUNS_DIR / workflow_name / run_id
        if not run_dir.is_dir():
            print(f"Run dir not found: {run_dir}", file=sys.stderr)
            sys.exit(1)
        logger = setup_logging(run_dir, workflow_name, run_id)
        run_log = run_dir / "run.log"
        logger.info(f"Advancing workflow: {workflow_name}/{run_id}")
        _advance_chain(workflow, run_dir, run_log, logger, workflow_name, run_id)
        return

    # Window override: retrospective editions query a past range directly
    # instead of the default today..today+13 (Pete 2026-08-26).
    if args.days_back is not None:
        from datetime import timedelta as _td
        from zoneinfo import ZoneInfo as _zi
        _now = datetime.now(_zi("America/Phoenix"))
        WINDOW_OVERRIDE["start"] = (_now - _td(days=args.days_back)).strftime("%Y-%m-%d")
        WINDOW_OVERRIDE["end"] = _now.strftime("%Y-%m-%d")
    if args.window_start:
        WINDOW_OVERRIDE["start"] = args.window_start
    if args.window_end:
        WINDOW_OVERRIDE["end"] = args.window_end
    if WINDOW_OVERRIDE:
        logger.info("Window override: %s..%s",
                    WINDOW_OVERRIDE.get("start"), WINDOW_OVERRIDE.get("end"))

    # PID-based idempotency — don't launch a duplicate router
    workflow_dir = RUNS_DIR / workflow_name
    workflow_dir.mkdir(parents=True, exist_ok=True)
    pid_file = workflow_dir / "router.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            # Check if that PID is still running
            try:
                os.kill(old_pid, 0)  # No-op signal, just checks existence
                logger.info(f"Router already running (PID {old_pid}). Exiting.")
                sys.exit(0)
            except OSError:
                pass  # Old PID dead, proceed
        except (ValueError, OSError):
            pass  # Corrupt or unreadable, proceed
    pid_file.write_text(str(os.getpid()))

    # Get or create run
    run_id, run_dir = get_or_create_run_dir(workflow_name, logger, force_new=args.new_run)
    logger = setup_logging(run_dir, workflow_name, run_id)
    run_log = run_dir / "run.log"

    logger.info(f"Starting workflow: {workflow_name}/{run_id}")
    log_event(run_log, {"ts": _ts(), "event": "workflow_started", "workflow": workflow_name, "run_id": run_id})

    # Check if already done
    wf_status = get_workflow_status(run_dir)
    if wf_status != "running":
        logger.info(f"Workflow is already {wf_status}. Exiting.")
        return

    # ── Set-and-forget chain (Brief 010) ──
    # The cron fires this once; each invocation advances the workflow by
    # exactly one LLM step (or runs all remaining deterministic steps
    # inline), then exits. The step-executor hands back via --advance when
    # its step finishes. No process lives longer than one step; no polling
    # anywhere; no per-step cron agentTurns.
    _advance_chain(workflow, run_dir, run_log, logger, workflow_name, run_id)


def _advance_chain(workflow, run_dir, run_log, logger, workflow_name, run_id) -> None:
    """Advance the set-and-forget chain one LLM step, then exit.

    Deterministic steps (enrich, send) run inline in a bounded loop; the
    first LLM step found is fired as a detached step-executor and this
    process exits. The executor hands back via `--advance` when done.
    """
    while True:
        max_run_minutes = workflow.get("max_run_minutes", 120)
        if workflow_is_over_budget(run_dir, max_run_minutes):
            error = f"workflow exceeded {max_run_minutes} minute run budget"
            logger.error(error)
            log_event(run_log, {
                "ts": _ts(),
                "event": "workflow_timeout",
                "max_run_minutes": max_run_minutes,
            })
            (run_dir / "workflow.state").write_text(json.dumps({
                "status": "failed",
                "error": error,
                "finished_at": _ts(),
            }, indent=2))
            send_failure_email(workflow_name, run_id, run_dir, logger)
            sys.exit(1)

        # Mark stuck-running steps as failed (executor died mid-step)
        for step in workflow["steps"]:
            state_file = run_dir / "steps" / f"{step['name']}.state"
            state = read_step_state(state_file)
            if step_is_stuck_running(state, step.get("timeout_min", 30)):
                logger.error(f"Step {step['name']}: stuck running — marking failed")
                write_step_state(state_file, {
                    "status": "failed",
                    "error": f"stuck running after {step.get('timeout_min', 30)}m",
                    "finished_at": _ts(),
                    "retries": state.get("retries", 0),
                })

        # Check for step failures (retries exhausted)
        failed, failed_step = any_step_failed(workflow, run_dir)
        if failed:
            logger.error(f"Workflow failed at step: {failed_step}")
            log_event(run_log, {"ts": _ts(), "event": "workflow_failed", "failed_step": failed_step})
            (run_dir / "workflow.state").write_text(json.dumps({
                "status": "failed", "error": f"step '{failed_step}' failed",
            }, indent=2))
            send_failure_email(workflow_name, run_id, run_dir, logger)
            sys.exit(1)

        # Hard gate (PRODUCTION-CHECKLIST.md Step 5): approve → continue;
        # reject → correct-and-continue happens inside verify; if the final
        # verdict is still rejected, block the send.
        gate_passed, gate_issues = verify_gate_passed(run_dir)
        if not gate_passed:
            logger.error(
                f"verify: HARD GATE FAILED — run blocked, DO NOT SEND "
                f"({len(gate_issues)} blocking issue(s))"
            )
            log_event(run_log, {"ts": _ts(), "event": "verify_gate_failed", "issues": gate_issues})
            (run_dir / "workflow.state").write_text(json.dumps({
                "status": "failed",
                "error": f"verify hard gate failed: {'; '.join(gate_issues[:3])}",
            }, indent=2))
            send_failure_email(workflow_name, run_id, run_dir, logger)
            sys.exit(1)

        # All done
        if all_steps_done(workflow, run_dir):
            logger.info("All steps completed successfully")
            log_event(run_log, {"ts": _ts(), "event": "workflow_completed"})
            (run_dir / "workflow.state").write_text(json.dumps({
                "status": "succeeded", "finished_at": _ts(),
            }, indent=2))
            sys.exit(0)

        # Find and run next step
        next_step = find_next_step(workflow, run_dir)
        if next_step is None:
            # No step ready — an executor is in flight (it will hand back
            # via --advance) or the workflow is already complete.
            logger.info("No step ready — exiting (executor in flight or done)")
            sys.exit(0)

        logger.info(f"Ready to run: {next_step['name']}")
        run_step(workflow_name, next_step, run_dir, run_log, logger,
                 embedding_query=workflow.get("embedding_query", ""),
                 jurisdictions=workflow.get("jurisdictions") or None)

        # Deterministic steps ran inline — loop to the next step.
        # LLM steps fired a detached executor — exit; it will advance.
        if next_step["name"] in ("enrich", "send"):
            continue
        logger.info(f"Fired LLM step '{next_step['name']}' — exiting; executor will advance")
        sys.exit(0)


if __name__ == "__main__":
    main()
