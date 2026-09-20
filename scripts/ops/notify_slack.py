#!/usr/bin/env python3
"""Post a plain notification to Slack, without ever exposing the token.

OpenClaw automation delivery cannot reach this workspace's Slack channel
("Unsupported channel: slack"), so the watchdog run posts its alert through this
small checked-in notifier instead.

Token handling: the bot token is read from the ``SLACK_BOT_TOKEN`` environment
variable (supplied by the operator's environment, never by this file) and is
never printed, logged, or echoed - every failure path is scrubbed of it before
anything is written to stdout/stderr.

Usage:
    python3 scripts/ops/notify_slack.py --text "message" --channel C0B7549FZE1

Exit codes: 0 delivered, 2 delivery refused/failed, 3 usage or configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://slack.com/api/chat.postMessage"


def _post(token: str, channel: str, text: str, timeout: int) -> dict:
    payload = json.dumps({"channel": channel, "text": text}).encode()
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
        return {"ok": False, "error": f"http_{exc.code}"}
    except Exception as exc:  # pragma: no cover - network dependent
        return {"ok": False, "error": type(exc).__name__}


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post a Slack notification")
    parser.add_argument("--text", required=True, help="message body")
    parser.add_argument(
        "--channel",
        default=os.environ.get("POLISCOPIC_SLACK_CHANNEL", ""),
        help="channel id (defaults to $POLISCOPIC_SLACK_CHANNEL)",
    )
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args(argv)

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("notify_slack: SLACK_BOT_TOKEN is not set", file=sys.stderr)
        return 3
    if not args.channel:
        print(
            "notify_slack: no channel given (use --channel or $POLISCOPIC_SLACK_CHANNEL)",
            file=sys.stderr,
        )
        return 3

    result = _post(token, args.channel, args.text, args.timeout)
    if result.get("ok"):
        # Only non-secret identifiers are reported back.
        print(
            json.dumps(
                {
                    "delivered": True,
                    "channel": result.get("channel"),
                    "ts": result.get("ts"),
                }
            )
        )
        return 0

    detail = _scrub(str(result.get("error") or "unknown_error"), token)
    print(f"notify_slack: delivery failed: {detail}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
