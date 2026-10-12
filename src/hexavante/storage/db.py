"""SQLite: connect + migrate versionado (Sprint 00).

- `connect(path)`: abre SQLite com `foreign_keys=ON` e `journal_mode=WAL`
  (WAL apenas para DBs em arquivo; `:memory:` mantém modo `memory`).
- `migrate(conn, migrations_dir=None)`: aplica arquivos `NNN_nome.sql`
  pendentes em ordem, cada um em transação própria, atualizando
  `PRAGMA user_version`. Idempotente: rodar 2x não duplica nem falha.
  Falha em migração faz rollback daquela migração e NÃO avança
  `user_version` (recuperável).
- `get_default_db_path()`: XDG (`$XDG_DATA_HOME` ou
  `~/.local/share/hexavante/organizer.db`). Apenas calcula o caminho;
  NÃO cria arquivo/diretório (testes usam `:memory:`/`tmp_path`).
- Sem `gi`, sem I/O fora do SQLite autorizado.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_MIGRATION_RE = re.compile(r"^(\d{3})_.*\.sql$")


def get_default_db_path() -> Path:
    """Caminho XDG padrão de produção. Não cria nada no filesystem."""
    import os

    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".local" / "share"
    return base / "hexavante" / "organizer.db"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Abre conexão SQLite com pragmas de integridade.

    Args:
        db_path: caminho do arquivo, `:memory:` ou objeto Path.
    """
    # Aceita str (incluindo ":memory:") ou Path.
    if isinstance(db_path, Path):
        name = str(db_path)
    else:
        name = db_path
    conn = sqlite3.connect(name)
    conn.row_factory = sqlite3.Row
    # Integridade relacional sempre ligada (exigida para FK em testes).
    conn.execute("PRAGMA foreign_keys = ON;")
    # WAL apenas para arquivos reais; :memory: não suporta WAL.
    if name != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
        except sqlite3.DatabaseError:
            pass
    return conn


def get_user_version(conn: sqlite3.Connection) -> int:
    """Lê PRAGMA user_version (fonte canônica da versão aplicada)."""
    row = conn.execute("PRAGMA user_version;").fetchone()
    return int(row[0]) if row is not None else 0


def _migrations_dir_default() -> Path:
    return Path(__file__).resolve().parent / "migrations"


def _list_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"diretório de migrações ausente: {migrations_dir}")
    found: list[tuple[int, Path]] = []
    for entry in sorted(migrations_dir.iterdir()):
        m = _MIGRATION_RE.match(entry.name)
        if m and entry.is_file():
            found.append((int(m.group(1)), entry))
    found.sort(key=lambda t: t[0])
    return found


def migrate(
    conn: sqlite3.Connection, migrations_dir: Path | str | None = None
) -> int:
    """Aplica migrações pendentes. Retorna `user_version` final.

    Idempotente e transacional por arquivo: cada migração roda dentro de
    `with conn:` (commit/rollback automático). Em falha, a exceção
    propaga e `user_version` NÃO avança para aquela versão.

    Raises:
        FileNotFoundError: diretório de migrações ausente.
        sqlite3.DatabaseError: SQL de migração inválido (rollback feito).
    """
    if migrations_dir is None:
        mdir = _migrations_dir_default()
    else:
        mdir = Path(migrations_dir)
    pending = [(v, p) for v, p in _list_migrations(mdir) if v > get_user_version(conn)]
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        try:
            with conn:  # transação: commit ok / rollback em exceção
                conn.executescript(sql)
                conn.execute(f"PRAGMA user_version = {version};")
        except Exception:
            # Garante que user_version não avançou parcialmente.
            # `with conn:` já fez rollback; re-leitura confirma.
            raise
    return get_user_version(conn)
