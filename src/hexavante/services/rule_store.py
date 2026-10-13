"""Persistência de regras em SQLite — Sprint 02.

Escopo ESTRITO Sprint 02:
- Upsert/listagem de `Rule` na tabela `rules`
  (rule_id PK, name, pattern, destination, priority, enabled 0/1).
- Apenas SQLite no path/:memory: passado pelo chamador.
- Valida destination/pattern não-vazios; NUNCA cria pastas no FS.
- Converte Rule <-> row; listagem ordenada por priority desc, rule_id asc.
- SEM movimentação, SEM scanner, SEM UI. Puro + SQLite autorizado.

Módulo sem `gi`, sem criar diretórios, sem efeitos na importação.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from hexavante.models.entities import Rule

__all__ = ["SqliteRuleStore"]


_RULES_DDL = """
CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    destination TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);
"""

_UPSERT_SQL = """
INSERT INTO rules (rule_id, name, pattern, destination, priority, enabled)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(rule_id) DO UPDATE SET
    name = excluded.name,
    pattern = excluded.pattern,
    destination = excluded.destination,
    priority = excluded.priority,
    enabled = excluded.enabled;
"""


def _row_to_rule(row: sqlite3.Row) -> Rule:
    return Rule(
        rule_id=row["rule_id"],
        name=row["name"],
        pattern=row["pattern"],
        destination=row["destination"],
        priority=int(row["priority"]),
        enabled=bool(row["enabled"]),
    )


def _validate_rule(rule: Rule) -> None:
    """Valida campos persistidos sem tocar no filesystem.

    Levanta ValueError se pattern/destination vazios (ou só espaços).
    Nunca cria pastas: apenas valida strings.
    """
    if not isinstance(rule, Rule):
        raise ValueError("esperado Rule")
    if not rule.rule_id or not rule.rule_id.strip():
        raise ValueError("Rule.rule_id não pode ser vazio")
    if not rule.name or not rule.name.strip():
        raise ValueError("Rule.name não pode ser vazio")
    if not rule.pattern or not rule.pattern.strip():
        raise ValueError("Rule.pattern não pode ser vazio")
    if not rule.destination or not rule.destination.strip():
        raise ValueError("Rule.destination não pode ser vazio")


class SqliteRuleStore:
    """Store SQLite de regras. Não cria pastas; só lê/escreve no DB.

    Args:
        db_path: ":memory:", caminho de arquivo (str|Path) ou
            conexão `sqlite3.Connection` já aberta pelo chamador.
            Quando path é passado, a conexão é aberta aqui (sem mkdir
            do parent; falha do SQLite propaga sem criar pastas).
            Quando conexão é passada, ela é reutilizada (não fechada aqui).

    Garante `CREATE TABLE IF NOT EXISTS rules` no init, compatível com
    `migrations/001_init.sql` (idempotente, não apaga dados).
    """

    def __init__(self, db_path: str | Path | sqlite3.Connection) -> None:
        if isinstance(db_path, sqlite3.Connection):
            self._conn = db_path
            self._owns = False
        else:
            name = str(db_path) if isinstance(db_path, Path) else db_path
            # Não cria diretórios: apenas abre o SQLite (falha se parent ausente).
            conn = sqlite3.connect(name)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys = ON;")
            except sqlite3.DatabaseError:
                pass
            self._conn = conn
            self._owns = True
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_RULES_DDL)
        self._conn.commit()

    # -- escrita ------------------------------------------------------

    def save(self, rule: Rule) -> Rule:
        """Insere ou atualiza (upsert) por `rule_id`. Retorna a regra.

        Valida pattern/destination não-vazios; nunca cria pastas no FS.
        """
        _validate_rule(rule)
        self._conn.execute(
            _UPSERT_SQL,
            (
                rule.rule_id,
                rule.name,
                rule.pattern,
                rule.destination,
                int(rule.priority),
                1 if rule.enabled else 0,
            ),
        )
        self._conn.commit()
        return rule

    # Alias explícito para clareza de chamada.
    def save_rule(self, rule: Rule) -> Rule:
        """Alias de `save`."""
        return self.save(rule)

    def upsert(self, rule: Rule) -> Rule:
        """Alias de `save` (upsert por PK)."""
        return self.save(rule)

    # -- leitura ------------------------------------------------------

    def get(self, rule_id: str) -> Rule | None:
        """Retorna a regra por id ou None se ausente."""
        row = self._conn.execute(
            "SELECT rule_id, name, pattern, destination, priority, enabled "
            "FROM rules WHERE rule_id = ?;",
            (rule_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_rule(row)

    def get_rule(self, rule_id: str) -> Rule | None:
        """Alias de `get`."""
        return self.get(rule_id)

    def load(self, rule_id: str | None = None) -> Rule | list[Rule] | None:
        """Compatibilidade `save/load`.

        - `load()` sem args -> `list_rules()` (todas, ordenadas).
        - `load(rule_id)` -> `get(rule_id)` (uma ou None).
        """
        if rule_id is None:
            return self.list_rules()
        return self.get(rule_id)

    def list_rules(self) -> list[Rule]:
        """Lista todas ordenadas por priority DESC, rule_id ASC."""
        rows = self._conn.execute(
            "SELECT rule_id, name, pattern, destination, priority, enabled "
            "FROM rules ORDER BY priority DESC, rule_id ASC;"
        ).fetchall()
        return [_row_to_rule(r) for r in rows]

    def count(self) -> int:
        """Número de regras persistidas (auxiliar de testes)."""
        row = self._conn.execute("SELECT COUNT(*) FROM rules;").fetchone()
        return int(row[0]) if row is not None else 0

    # -- ciclo de vida -------------------------------------------------

    def close(self) -> None:
        """Fecha a conexão apenas se foi aberta por este store."""
        if self._owns:
            try:
                self._conn.close()
            finally:
                pass

    def __enter__(self) -> SqliteRuleStore:
        return self

    def __exit__(self, *args: object) -> bool:
        self.close()
        return False
