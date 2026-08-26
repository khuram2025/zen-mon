from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_cli_update_gates_schema_before_build_and_restart():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    update = script.split('    update)\n', 1)[1].split('    backup)\n', 1)[0]

    schema_gate = update.index("if ! run_migrations; then")
    build = update.index('echo "  building poller..."')
    restart = update.index("systemctl restart zenplus-api zenplus-poller nginx")

    assert schema_gate < build < restart
    assert 'git reset --hard "$OLD"' in update
    assert "existing services were not restarted" in update
    assert "MIGRATION_RC" not in update
