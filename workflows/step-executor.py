#!/usr/bin/env python3
"""step-executor.py — Single-step LLM executor for agentTurn delegation.

Called by an isolated OpenClaw agentTurn session. Reads a job file staged
by workflow-runner.py, calls the LLM, writes results, and updates the step
state file. The orchestrator's poll loop detects completion.

Usage:
    python3 workflows/step-executor.py <workflow> <run-id> <step-name>

The job file is read from:
    data/runs/<workflow>/<run-id>/jobs/<step-name>-job.json
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Config ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "data" / "runs"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LLM_TIMEOUT = 600  # seconds for the HTTP request (agentTurn timeout is the real bound)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime(DATE_FORMAT)


def _load_env() -> None:
    """Load .env for secrets."""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _call_llm(prompt: str) -> str:
    """Call DeepSeek with a prompt and return the raw response text."""
    _load_env()
    try:
        import requests
    except ImportError:
        # Fallback: use urllib
        import urllib.request
        import urllib.error

        api_key = os.environ.get("DEEPSEEK_API_KEY", "") or os.environ.get("REPORTS_LLM_API_KEY", "")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY not set in .env")

        model = os.environ.get("REPORTS_LLM_MODEL", "deepseek-chat")
        base_url = os.environ.get("REPORTS_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
        url = f"{base_url}/chat/completions"

        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=LLM_TIMEOUT)
        result = json.loads(resp.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"]

    api_key = os.environ.get("DEEPSEEK_API_KEY", "") or os.environ.get("REPORTS_LLM_API_KEY", "")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY not set in .env")

    model = os.environ.get("REPORTS_LLM_MODEL", "deepseek-chat")
    base_url = os.environ.get("REPORTS_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    url = f"{base_url}/chat/completions"

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        },
        timeout=LLM_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()
    return result["choices"][0]["message"]["content"]


def _parse_llm_response(raw: str) -> object:
    """Parse an LLM response as JSON, with fallback."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        import re
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return raw


def _write_state(state_file: Path, status: str, error: str = "") -> None:
    """Write a step state file, preserving existing fields (like retries)."""
    state = {}
    if state_file.exists():
        try:
            existing = json.loads(state_file.read_text())
            state.update(existing)
        except (json.JSONDecodeError, IOError):
            pass
    state["status"] = status
    state["finished_at"] = _ts()
    if error:
        state["error"] = error
    state_file.write_text(json.dumps(state, indent=2))


def _handoff(workflow_name: str, run_id: str) -> None:
    """Set-and-forget chain (Brief 010): hand control back to the runner.

    Launches `workflow-runner.py <wf> <run-id> --advance` fully detached so
    the runner can fire the next step (or apply retry / failure logic).
    Called on both success and failure — the runner decides what happens
    next. Never blocks; the caller exits immediately after.
    """
    import subprocess
    project_root = str(Path(__file__).resolve().parent.parent)
    runner = str(Path(__file__).resolve().parent / "workflow-runner.py")
    log_path = RUNS_DIR / workflow_name / run_id / "handoff.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as logf:
            subprocess.Popen(
                [sys.executable, "-u", runner, workflow_name, run_id, "--advance"],
                cwd=project_root,
                stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        print(f"HANDOFF — workflow-runner --advance launched for {workflow_name}/{run_id}")
    except Exception as e:
        print(f"WARNING: handoff failed: {e}", file=sys.stderr)


def _write_classify_output(output_dir: Path, llm_output: dict) -> None:
    """Write classified topic files — one per topic key.

    Only top-level keys whose value is a LIST of items become files.  LLMs
    sometimes echo stray scalar keys (e.g. {"type": "json_object"}); those
    must never be written as topic files — downstream steps treat every
    *.json in the directory as a classified-items file and would crash.
    """
    manifest = {}
    ignored = []
    topics = [
        k for k in llm_output
        if not k.startswith("_") and k != "gaps" and isinstance(llm_output[k], list)
    ]
    if topics:
        for topic, items in llm_output.items():
            if topic.startswith("_"):
                continue
            if topic == "gaps":
                # gaps.json is written deterministically by workflow-runner.py
                # (PRODUCTION-CHECKLIST.md Step 1) — never let the LLM override it.
                continue
            if not isinstance(items, list):
                ignored.append(topic)
                continue
            topic_file = output_dir / f"{topic}.json"
            topic_file.write_text(json.dumps({
                "topic": topic,
                "date": str(datetime.now().date()),
                "items": items,
            }, indent=2))
            manifest[topic] = str(topic_file)
    else:
        manifest = {"_raw": str(output_dir / "llm-response.json")}

    if ignored:
        print(f"WARNING: classify output key(s) not item lists, ignored: {ignored}",
              file=sys.stderr)

    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def _write_generic_output(output_dir: Path, step_name: str, llm_output: object) -> None:
    """Write output for non-classify LLM steps (summarize, verify)."""
    if isinstance(llm_output, dict):
        out_path = output_dir / f"{step_name}-result.json"
        out_path.write_text(json.dumps(llm_output, indent=2))
    else:
        out_path = output_dir / f"{step_name}-output.json"
        out_path.write_text(json.dumps({"response": str(llm_output)}, indent=2))

    # Always write a raw copy for debugging
    raw_path = output_dir / f"{step_name}-llm-response.json"
    raw_path.write_text(json.dumps(llm_output, indent=2, default=str))


def main() -> None:
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <workflow> <run-id> <step-name>", file=sys.stderr)
        sys.exit(1)

    workflow_name = sys.argv[1]
    run_id = sys.argv[2]
    step_name = sys.argv[3]

    # ── Resolve paths ──
    run_dir = RUNS_DIR / workflow_name / run_id
    job_file = run_dir / "jobs" / f"{step_name}-job.json"
    state_file = run_dir / "steps" / f"{step_name}.state"

    if not job_file.exists():
        print(f"ERROR: Job file not found: {job_file}", file=sys.stderr)
        _write_state(state_file, "failed", f"Job file not found: {job_file}")
        _handoff(workflow_name, run_id)
        sys.exit(1)

    # ── Read job ──
    job = json.loads(job_file.read_text())
    prompt = job["prompt"]
    output_dir = Path(job["output_dir"]) if job.get("output_dir") else None
    is_first_step = job.get("is_first_step", False)

    # Ensure output directory exists
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    # ── Call LLM ──
    start = time.time()
    try:
        raw_result = _call_llm(prompt)
    except Exception as e:
        duration = time.time() - start
        error_msg = f"LLM call failed after {duration:.0f}s: {e}"
        print(f"ERROR: {error_msg}", file=sys.stderr)
        _write_state(state_file, "failed", error_msg)
        _handoff(workflow_name, run_id)
        sys.exit(1)

    elapsed = time.time() - start

    # ── Parse response ──
    try:
        llm_output = _parse_llm_response(raw_result)
    except Exception as e:
        error_msg = f"Failed to parse LLM response: {e}"
        print(f"ERROR: {error_msg}", file=sys.stderr)
        _write_state(state_file, "failed", error_msg)
        _handoff(workflow_name, run_id)
        sys.exit(1)

    # ── Write output ──
    try:
        if output_dir:
            if is_first_step:
                if isinstance(llm_output, dict):
                    _write_classify_output(output_dir, llm_output)
                else:
                    _write_generic_output(output_dir, step_name, llm_output)
            else:
                _write_generic_output(output_dir, step_name, llm_output)

        _write_state(state_file, "succeeded")
        print(f"DONE — {step_name} completed in {elapsed:.1f}s (output: {output_dir})")
        _handoff(workflow_name, run_id)
    except Exception as e:
        error_msg = f"Failed to write output: {e}"
        print(f"ERROR: {error_msg}", file=sys.stderr)
        _write_state(state_file, "failed", error_msg)
        _handoff(workflow_name, run_id)
        sys.exit(1)


if __name__ == "__main__":
    main()
