"""Shared builders for the Stage 3 processing receipt, backfill, and validator tests."""

import hashlib
import itertools
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.kg import stage2_backup_verify as backup_verify
from scripts.kg import stage3_processing_backfill as B
from scripts.kg import stage3_processing_plan_inputs as P
from scripts.kg import stage3_processing_plan_validator as V
from scripts.kg import stage3_processing_receipt as R
from scripts.kg import stage3_processing_receipt_store_packet as SP
from scripts.kg.stage2_artifacts import write_immutable

NEW = datetime(2026, 9, 13, tzinfo=timezone.utc)
OLD = datetime(2026, 9, 12, tzinfo=timezone.utc)
NOW = "2026-09-14T12:00:00+00:00"
LATER = "2026-09-14T13:00:00+00:00"
EARLIER = "2026-09-14T11:00:00+00:00"
TARGET = {"tier": "development", "database": "poliscopic_dev",
          "dialect": "postgresql", "host": "192.0.2.10", "port": 5432}
STAMP = "2026-09-14T12:00:00Z"
_SEQUENCE = itertools.count()


def _dir(tmp_path):
    """Artifacts are write-once, so every call needs its own directory."""
    path = tmp_path / f"artifact-{next(_SEQUENCE)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def row(**changes):
    value = {"id": 1, "body": "x", "document_type": "Agenda",
             "document_url": "https://example.test/a.pdf",
             "text_content": "retained body", "text_extraction_method": "pymupdf",
             "text_extracted_at": NEW, "scraped_at": OLD, "content_hash": "raw",
             "meeting_db_id": 1, "agenda_item_db_id": None, "swept_at": None}
    value.update(changes)
    return value


def receipt_for(source, **changes):
    """A canonical, correctly signed receipt; overrides are re-signed, never stale."""
    value = R.build_receipt(source, status="success", reason="", recorded_at=NOW)
    value.update(changes)
    return resign_receipt(value)


def resign_receipt(payload):
    payload.pop("digest", None)
    payload["digest"] = R.receipt_digest(payload)
    return payload


def resign_plan(plan):
    plan.pop("digest", None)
    plan["digest"] = V.plan_digest(plan)
    return plan


def evidence(tmp_path, *, population=2, target=None, kinds=None):
    """Write the two authoritative evidence artifacts and return their bindings."""
    target = dict(target or TARGET)
    kinds = dict(kinds or V.EVIDENCE_COMPONENTS)
    directory = _dir(tmp_path)
    elig_path = directory / "eligibility.json"
    eligibility = {"kind": kinds["source_eligibility"], "version": "test",
                   "target": target, "population_sha256": "e" * 64,
                   "accounting": {"discovered": population}}
    elig_digest = write_immutable(elig_path, eligibility)
    proc_path = directory / "processing-identity.json"
    processing = {"kind": kinds["processing_identity"], "version": "test",
                  "target": target, "population_sha256": "f" * 64,
                  "accounting": {"documents": population},
                  "eligibility_binding": {"path": str(elig_path), "digest": elig_digest}}
    proc_digest = write_immutable(proc_path, processing)
    binding = {
        "source_eligibility": {"path": str(elig_path), "digest": elig_digest,
                               "kind": eligibility["kind"], "target": target},
        "processing_identity": {"path": str(proc_path), "digest": proc_digest,
                                "kind": processing["kind"], "target": target},
    }
    state = {"verified": True, "problems": [],
             "source_eligibility": {"artifact_digest": elig_digest,
                                    "population_sha256": eligibility["population_sha256"],
                                    "accounting": eligibility["accounting"]},
             "processing_identity": {"artifact_digest": proc_digest,
                                     "population_sha256": processing["population_sha256"],
                                     "accounting": processing["accounting"]}}
    return binding, state


def receipt_set(tmp_path, receipts, *, target=None):
    """A canonically loadable receipt-set artifact binding the given receipts."""
    target = dict(target or TARGET)
    path = _dir(tmp_path) / "receipt-set.json"
    payload = {"kind": P.RECEIPT_SET_KIND, "version": "kg-stage3-processing-receipt-set/1.0",
               "target": target, "count": len(receipts),
               "receipts": [dict(item) for item in receipts]}
    digest = write_immutable(path, payload)
    return {"source": path.name, "path": str(path), "digest": digest,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "count": len(receipts)}


def selection_binding(tmp_path, rows, *, evidence_binding, limit=None, offset=0, target=None):
    """The write-once selection snapshot binding for exactly these rows."""
    target = dict(target or TARGET)
    bounded = B.select(rows, limit=limit, offset=offset)
    path = _dir(tmp_path) / "selection.json"
    snapshot = P.build_selection_snapshot(
        created_at=STAMP, target=target, bound=bounded["bound"],
        entries=[P.classification_entry(item) for item in bounded["rows"]],
        evidence=evidence_binding, code_hashes=V.code_hashes())
    digest = write_immutable(path, snapshot)
    return {"path": str(path), "digest": digest, "kind": P.SELECTION_KIND, "target": target,
            "identity_sha256": snapshot["identity_sha256"],
            "selected": bounded["bound"]["selected"],
            "population": bounded["bound"]["population"],
            "order": bounded["bound"]["order"]}


def plan_inputs(rows, receipts=None, *, tmp_path, population=None, **kwargs):
    """The exact inputs a plan is a pure function of, ready for a rebuild."""
    binding, state = evidence(
        tmp_path, population=len(rows) if population is None else population)
    selection = selection_binding(tmp_path, rows, evidence_binding=binding,
                                  limit=kwargs.get("limit"), offset=kwargs.get("offset", 0))
    receipt_binding = receipt_set(tmp_path, receipts) if receipts else \
        {"source": "none", "count": 0}
    return dict(rows=rows, receipts=receipts, target=TARGET, hashes=V.code_hashes(),
                evidence=binding, current_state_validation=state,
                selection_binding=selection, receipts_binding=receipt_binding, **kwargs)


def build_plan(rows, receipts=None, *, tmp_path, population=None, **kwargs):
    inputs = plan_inputs(rows, receipts, tmp_path=tmp_path, population=population, **kwargs)
    return B.build_dry_plan(created_at=STAMP, **inputs)


# --- receipt-store (design-only packet) fixtures ---------------------------------

STORE_STAMP = "2026-09-14T12:00:00Z"
BACKUP_AT = "2026-09-14T12:00:00+00:00"
DUMP_STARTED = "2026-09-14T12:05:00+00:00"


def append_action(source=None, **changes):
    """A real append action, produced by the accepted merge contract."""
    body = receipt_for(source or row())
    action = R.merge_receipts([], [body])["actions"][0]
    action.update(changes)
    return action


def bounded_plan(tmp_path):
    """A small, fully valid plan artifact (never the 43 MB authoritative one)."""
    plan = build_plan([row(id=1)], None, tmp_path=tmp_path)
    path = tmp_path / "bounded-plan.json"
    write_immutable(path, plan)
    return path


def packet_for(tmp_path, *, plan=None):
    return SP.build_packet(plan_path=plan or bounded_plan(tmp_path), created_at=STORE_STAMP)


def baseline_fixture(tmp_path, *, database="poliscopic_dev", schema_sha=None):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    counts = {"agenda_items": 1}
    baseline = {"kind": backup_verify.BASELINE_KIND, "version": "2.0",
                "created_at": STORE_STAMP, "target": {**TARGET, "database": database},
                "counts": counts, "counts_sha256": backup_verify.canonical_sha256(counts),
                "schema_signature": {"schema_sha256": schema_sha or "a" * 64,
                                     "agenda_items": {"digest": "b" * 64}},
                "integrity": {"structural_orphans": 0}}
    baseline["digest"] = backup_verify.canonical_sha256(baseline)
    assert backup_verify.validate_baseline(baseline) == []
    path = tmp_path / "baseline.json"
    write_immutable(path, baseline)
    return path, baseline


def backup_fixture(tmp_path, *, database="poliscopic_dev", with_baseline=True, digest=None,
                   restore=True, created_at=BACKUP_AT, schema_sha=None):
    baseline_path, baseline = baseline_fixture(tmp_path, database=database,
                                              schema_sha=schema_sha)
    dump = b"dump-bytes"
    dump_path = Path(tmp_path) / "backup.dump"
    dump_path.write_bytes(dump)
    receipt = backup_verify.build_receipt(
        baseline=baseline, baseline_path=str(baseline_path) if with_baseline else "",
        dump_path=str(dump_path), dump_sha256=digest or hashlib.sha256(dump).hexdigest(),
        dump_started_at=DUMP_STARTED, comparisons={"counts": True, "schema": True},
        problems=[], created_at=created_at)
    if not restore:
        receipt["pg_restore"] = {"exit_code": 1, "evidence": ""}
    path = Path(tmp_path) / "backup-receipt.json"
    write_immutable(path, receipt)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return path, receipt


def now_after(created_at=BACKUP_AT, hours=1):
    return datetime.fromisoformat(created_at) + timedelta(hours=hours)
