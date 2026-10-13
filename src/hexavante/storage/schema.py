"""Schema versionado — Sprint 00.

- `CURRENT_SCHEMA_VERSION` é a versãocanônica esperada após `migrate()`.
- `EXPECTED_TABLES` lista tabelas criadas por `migrations/001_init.sql`.
- O DDL vive nos arquivos SQL versionados; este módulo expõe apenas
  constantes para validação em testes e diagnóstico, sem I/O.
"""

from __future__ import annotations

CURRENT_SCHEMA_VERSION: int = 1

EXPECTED_TABLES: tuple[str, ...] = (
    "schema_version",
    "folders",
    "rules",
    "operations_log",
    "move_items",
    "schedules",
)

# Estados válidos persistidos (CHECK constraint em 001_init.sql).
# Mantidos em ASCII snake_case; exibição com acentos fica na view.
VALID_OPERATION_STATES: tuple[str, ...] = (
    "planejada",
    "aprovada",
    "em_andamento",
    "verificada",
    "concluida",
    "falhou",
    "recuperacao_pendente",
)
