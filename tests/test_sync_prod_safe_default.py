from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SYNC_PROD = ROOT / "scripts" / "sync" / "sync_prod.sh"
INTERLOCK = ROOT / "scripts" / "ops" / "production_interlock.py"


def test_scheduled_prod_sync_is_data_only_by_default():
    script = SYNC_PROD.read_text()
    assert "sync_prod.py --reconcile" in script
    assert 'if [ "${1:-}" = "--with-code"]' in script or 'if [ "${1:-}" = "--with-code" ]' in script
    assert "Mode: data only" in script


def test_stale_readiness_gate_is_absent():
    """The morning readiness receipt no longer authorizes anything.

    It validates LOCAL HISTORICAL artifacts and cannot establish current
    production identity or integrity, so it must not gate production. Historical
    receipts are diagnostic evidence only.
    """
    script = SYNC_PROD.read_text()
    assert "verify_morning_sync_readiness.py" not in script, (
        "the stale readiness path must not gate production sync"
    )
    assert "DIAGNOSTIC ONLY" in script or "diagnostic" in script.lower()


def test_production_sync_pins_the_new_safety_authority():
    """The interlock is the gate now, and it is invoked before the sync launches.

    Compared by LINE, not by raw character offset: the guard's own comment mentions
    "nohup", which defeats a naive `str.index` check.
    """
    script = SYNC_PROD.read_text()
    assert "production_interlock.py check" in script
    assert "--operation OP-RECON" in script

    lines = script.splitlines()
    interlock_line = next(
        i for i, line in enumerate(lines) if "production_interlock.py check" in line)
    nohup_line = next(
        i for i, line in enumerate(lines) if line.strip().startswith("nohup"))
    assert interlock_line < nohup_line, (
        "the interlock must run before the sync is launched"
    )
    # a refusal must stop the script, not fall through
    assert "exit 3" in script


def test_code_deployment_requires_explicit_switch():
    script = SYNC_PROD.read_text()
    assert "unsupported option" in script
    assert '[ "$#" -gt 1 ]' in script
    assert '[ "$1" != "--with-code" ]' in script
    assert "POLISCOPIC_DEPLOY_PATHS" in script
    assert "scripts/ops/deploy_release.sh" in script
    assert 'bash "$PROJECT_ROOT/sync.sh"' not in script


def test_interlock_authority_exists_and_refuses_production_kinds():
    src = INTERLOCK.read_text()
    assert "AUTHORIZATION_DISABLED" in src
    for kind in ("OP-CODE", "OP-SCHEMA", "OP-REPAIR", "OP-RECON", "OP-RESTORE"):
        assert kind in src, f"{kind} missing from the interlock classification"
    assert "no environment escape hatch" in src
