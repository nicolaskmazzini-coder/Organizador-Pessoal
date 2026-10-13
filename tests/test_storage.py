"""Storage SQLite: migrate do zero, idempotência, FK, rollback. Só tmp_path."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hexavante.storage import db as dbmod
from hexavante.storage.db import connect, get_user_version, migrate
from hexavante.storage.schema import CURRENT_SCHEMA_VERSION, EXPECTED_TABLES


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
    ).fetchall()
    return {r[0] for r in rows}


def test_migrate_do_zero_cria_tabelas(tmp_path: Path) -> None:
    target = tmp_path / "organizer.db"
    conn = connect(target)
    try:
        assert get_user_version(conn) == 0
        version = migrate(conn)
        assert version == CURRENT_SCHEMA_VERSION == 1
        created = _tables(conn)
        for expected in EXPECTED_TABLES:
            assert expected in created, f"tabela ausente: {expected}"
        # pragmas de integridade
        fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert fk == 1
    finally:
        conn.close()
    # Nunca criou DB real em ~/.local/share
    assert not (Path.home() / ".local" / "share" / "hexavante" / "organizer.db").exists()
    # Garante que o arquivo de teste existe apenas em tmp_path
    assert target.exists()


def test_migrate_idempotente(tmp_path: Path) -> None:
    target = tmp_path / "idem.db"
    conn = connect(target)
    try:
        v1 = migrate(conn)
        # Insere dado sintético e roda migrate de novo: dado sobrevive.
        conn.execute(
            "INSERT INTO folders (folder_id, path, is_authorized_root) VALUES (?, ?, ?);",
            ("f1", "/tmp/synth/root", 1),
        )
        conn.commit()
        v2 = migrate(conn)
        assert v1 == v2 == 1
        n = conn.execute("SELECT COUNT(*) FROM folders;").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    target = tmp_path / "fk.db"
    conn = connect(target)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO operations_log (operation_id, plan_id, plan_hash, state)"
            " VALUES (?, ?, ?, ?);",
            ("op1", "plan1", "hash1", "planejada"),
        )
        conn.commit()
        # FK válida passa.
        conn.execute(
            "INSERT INTO move_items (operation_id, source_path, dest_path)"
            " VALUES (?, ?, ?);",
            ("op1", "/tmp/synth/a", "/tmp/synth/b"),
        )
        conn.commit()
        # FK inválida deve falhar com IntegrityError (foreign_keys=ON).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO move_items (operation_id, source_path, dest_path)"
                " VALUES (?, ?, ?);",
                ("op-inexistente", "/tmp/synth/a", "/tmp/synth/b"),
            )
        # CHECK constraint de estados também é enforced.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO operations_log (operation_id, plan_id, plan_hash, state)"
                " VALUES (?, ?, ?, ?);",
                ("op-bad", "plan1", "hash1", "estado-invalido"),
            )
    finally:
        conn.close()


def test_rollback_em_migracao_quebrada(tmp_path: Path) -> None:
    # Diretório sintético com 001 válida (copiada) + 002 quebrada.
    synth_migrations = tmp_path / "migrations"
    synth_migrations.mkdir()
    real_001 = Path(dbmod.__file__).resolve().parent / "migrations" / "001_init.sql"
    (synth_migrations / "001_init.sql").write_text(real_001.read_text(encoding="utf-8"), encoding="utf-8")
    (synth_migrations / "002_broken.sql").write_text(
        "CREATE TABLE broken (id INTEGER PRIMARY KEY; -- SQL inválido de propósito\n",
        encoding="utf-8",
    )
    target = tmp_path / "rollback.db"
    conn = connect(target)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            migrate(conn, migrations_dir=synth_migrations)
        # Rollback: versão permanece em 1, tabelas de 001 intactas.
        assert get_user_version(conn) == 1
        assert "operations_log" in _tables(conn)
        assert "broken" not in _tables(conn)
    finally:
        conn.close()


def test_default_db_path_eh_xdg_e_nao_cria_nada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "fake-xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home))
    p = dbmod.get_default_db_path()
    assert p == fake_home / "hexavante" / "organizer.db"
    # Apenas calcula; não cria arquivo nem diretório.
    assert not p.exists()
    assert not (fake_home / "hexavante").exists()
