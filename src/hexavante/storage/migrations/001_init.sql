-- 001_init.sql — Fundação Sprint 00 (v1).
-- Tabelas: schema_version (auditoria), folders, rules, operations_log
-- (trilha de auditoria com estados), move_items (FK p/ teste de integridade),
-- schedules. Sem dados pessoais, sem seeds destrutivos.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS folders (
    folder_id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    is_authorized_root INTEGER NOT NULL DEFAULT 0 CHECK (is_authorized_root IN (0, 1)),
    last_scan_at TEXT
);

CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    destination TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);

-- Trilha de auditoria: um registro por operação, estados persistentes.
CREATE TABLE IF NOT EXISTS operations_log (
    operation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'planejada', 'aprovada', 'em_andamento', 'verificada',
        'concluida', 'falhou', 'recuperacao_pendente'
    )),
    approval_token TEXT,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Itens vinculados à operação (exercita FK ON DELETE CASCADE).
CREATE TABLE IF NOT EXISTS move_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES operations_log(operation_id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    dest_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cron_expr TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    dry_run INTEGER NOT NULL DEFAULT 1 CHECK (dry_run IN (0, 1)),
    last_run_at TEXT
);

-- Registra a versão aplicada na trilha (idempotente).
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
