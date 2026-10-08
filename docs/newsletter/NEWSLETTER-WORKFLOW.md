# Newsletter Workflow v3 — YAML Workflow Runner

**Status:** Active
**Last Updated:** 2026-07-23

---

## Architecture

5 cron jobs (Mon–Fri, 5 AM Phoenix), each firing one isolated script:

```
Cron (5:00 AM, isolated agent session, 60s timeout)
  │
  │  nohup .venv/bin/python -u workflows/workflow-runner.py <topic>
  │        > data/runs/<topic>/launcher.log 2>&1 &
  │
  └─ workflow-runner.py:
       Reads workflows/<topic>.yaml → loops every 60s checking step states
       Each step has an instructions.md with LLM prompts
       Writes step state to data/runs/<workflow>/<run-id>/steps/
```

The runner is a dumb "read YAML, loop, check step files" orchestrator. It
does not background subtasks — each step runs synchronously via the LLM.

## Topics

| Cron Day | Topic | Workflow YAML |
|---|---|---|
| Monday | Boards & Commissions | `workflows/boards-commissions.yaml` |
| Tuesday | Housing & Development | `workflows/housing.yaml` |
| Wednesday | Public Safety & Justice | `workflows/public-safety.yaml` |
| Thursday | Water & Environment | `workflows/water-environment.yaml` |
| Friday | Transportation & Infrastructure | `workflows/transportation.yaml` |

## Directory Structure

```
workflows/
├── workflow-runner.py          # orchestrator (42KB)
├── boards-commissions.yaml
├── housing.yaml
├── public-safety.yaml
├── transportation.yaml
├── water-environment.yaml
├── <topic>/                    # per-topic step instructions
│   └── 01-classify/
│       └── instructions.md
├── shared/                     # shared step instructions
│   ├── 02-enrich/instructions.md
│   ├── 03-summarize/instructions.md
│   ├── 04-verify/instructions.md
│   ├── 05-send/instructions.md
│   └── 06-publish/instructions.md
└── templates/
    ├── newsletter.html          # HTML email template
    └── render.py                # template renderer

data/runs/<topic>/<run-id>/
├── steps/                       # per-step state files
├── launcher.log                 # runner stdout/stderr
└── ...                          # step artifacts
```

## Cron Config

All five cron jobs follow the same pattern:

```json
{
  "name": "newsletter-<topic>",
  "schedule": { "kind": "cron", "expr": "0 5 * * <day>", "tz": "America/Phoenix" },
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "cd /Users/pmains/Code/openclaw/poliscopic && nohup .venv/bin/python -u workflows/workflow-runner.py <topic> > data/runs/<topic>/launcher.log 2>&1 &",
    "timeoutSeconds": 60
  },
  "delivery": { "mode": "none" },
  "failureAlert": { "after": 1, "mode": "announce", "channel": "slack", "to": "C0BJ293PMTN" }
}
```

## Archived

The previous v2 shell-based pipeline (`scripts/newsletter/workflow.sh`,
`pipeline.sh`, `router-*.sh`, `validators/`) has been archived at
`scripts/archive/newsletter/`.
