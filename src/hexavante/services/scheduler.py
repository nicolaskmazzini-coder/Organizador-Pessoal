"""Agendador em dry-run — Sprint 06.

Escopo ESTRITO Sprint 06 (cf. AGENTS.md + docs/SPRINTS.md):
- Persistência em SQLite (`schedules` + `operations_log` da migração 001),
  execução sem sobreposição e APENAS dry-run (scanner/classificação).
- Este módulo NUNCA realiza escrita em destino, NUNCA autoriza ou realiza
  movimentação real: só chama métodos de LEITURA (`scan` / `classify`) e
  registra auditoria em `operations_log` + `run_log` em memória.
- Sem `sudo`, sem instalar serviços, sem `gi`, sem I/O em diretórios
  pessoais. O chamador aplica `migrate()` e fornece a conexão.
- Relógio injetável (`clock: () -> datetime`) para testes determinísticos.
- `run_log` (lista ou dict com chave "events") permite observar execuções
  sem tocar no filesystem.

Política de execuções perdidas (documentada aqui e coberta por testes):
- `desabilitado nunca roda`: `next_run` retorna None e `run_once`/`run_due`
  recusam (`skipped_disabled`), sem catch-up futuro automático.
- `catch-up único`: se `last_run_at + intervalo <= agora`, a agenda está
  vencida (`is_due == True`), mas cada chamada de `run_due_all` executa cada
  agenda vencida NO MÁXIMO UMA VEZ; após a execução, `last_run_at` avança
  para "agora", descartando intervalos intermediários perdidos (sem fila,
  sem rajada de recuperação).
- `cron_expr` vazio/inválido nunca dispara (`next_run` None, `is_due`
  False): sem reprocessamento cego.
- Falhas de leitura (scanner/classifier) são persistidas como operação
  `falhou` + evento `error` em `run_log`; `run_once` retorna None sem
  propagar (sem perda silenciosa, sem crash). `last_run_at` avança mesmo
  em falha para evitar loop apertado de repetição imediata; o erro fica
  auditado para inspeção humana.
- Sobreposição: conjunto em memória `_running` por instância + `Lock`.
  Segunda execução concorrente do MESMO `schedule_id` é recusada
  (`skipped_overlap`), sem duplicar trabalho. IDs diferentes podem rodar
  em paralelo. Como o estado é só memória, restart nunca deixa trava
  fantasma: nova instância com o mesmo DB roda normalmente
  (`recover_after_restart` limpa o conjunto local; sem flag em DB).

Módulo puro Python + SQLite autorizado; sem efeitos na importação.
"""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from hexavante.models.entities import (
    OperationRecord,
    OperationState,
    Schedule,
    compute_plan_hash,
)

__all__ = ["DryRunScheduler", "parse_interval_seconds"]

_HOURLY_S = 3600
_DAILY_S = 86400

_UPSERT_SCHEDULE_SQL = """
INSERT INTO schedules (schedule_id, name, cron_expr, enabled, dry_run, last_run_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(schedule_id) DO UPDATE SET
    name = excluded.name,
    cron_expr = excluded.cron_expr,
    enabled = excluded.enabled,
    dry_run = excluded.dry_run,
    last_run_at = excluded.last_run_at;
"""

_SELECT_SCHEDULE_SQL = (
    "SELECT schedule_id, name, cron_expr, enabled, dry_run, last_run_at "
    "FROM schedules WHERE schedule_id = ?;"
)

_LIST_SCHEDULES_SQL = (
    "SELECT schedule_id, name, cron_expr, enabled, dry_run, last_run_at "
    "FROM schedules ORDER BY schedule_id ASC;"
)


def parse_interval_seconds(cron_expr: str | None) -> int | None:
    """Interpreta `cron_expr` simples. Retorna segundos ou None se inválido.

    Aceitos: `"interval:<segundos>"` (int positivo), `"@hourly"` (3600),
    `"@daily"` (86400). Vazio/None/desconhecido/<=0/não-numérico -> None.
    Espaços extras são tolerados; caixa de `@HOURLY` é normalizada.
    """
    if cron_expr is None:
        return None
    text = cron_expr.strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered == "@hourly":
        return _HOURLY_S
    if lowered == "@daily":
        return _DAILY_S
    if lowered.startswith("interval:"):
        raw = text[len("interval:"):].strip()
        if not raw:
            return None
        try:
            # Aceita "60" e "60.0"; rejeita texto, zero e negativos.
            value = int(float(raw)) if ("." in raw or "e" in raw.lower()) else int(raw)
        except (ValueError, OverflowError):
            return None
        if value <= 0:
            return None
        return value
    return None


def _parse_iso_or_none(value: str | None) -> datetime | None:
    """Converte ISO-8601 em datetime ou None se ausente/inválido."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        # `fromisoformat` não aceita "Z"; normaliza para +00:00.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def _as_aware_utc(value: datetime) -> datetime:
    """Normaliza datetime para UTC aware (ingênuo assume UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _row_to_schedule(row: sqlite3.Row) -> Schedule:
    return Schedule(
        schedule_id=row["schedule_id"],
        name=row["name"],
        cron_expr=row["cron_expr"] if row["cron_expr"] is not None else "",
        enabled=bool(row["enabled"]),
        dry_run=bool(row["dry_run"]),
        last_run_at=row["last_run_at"],
    )


def _discover_scan_root(scanner: Any) -> Any | None:
    """Tenta descobrir raiz para scanners que exigem `root` posicional.

    Procura atributos comuns (`_authorized_roots`, `authorized_roots`,
    `_root`, `root`) e retorna o primeiro item. Retorna None se nada
    encontrado (chamador trata como falha auditada, sem crash).
    """
    for attr in ("_authorized_roots", "authorized_roots", "_root", "root", "scan_root"):
        try:
            candidate = getattr(scanner, attr, None)
        except Exception:
            continue
        if candidate is None:
            continue
        if isinstance(candidate, (list, tuple)) and candidate:
            return candidate[0]
        if isinstance(candidate, (str, bytes)):
            return candidate
        # Path ou outro escalar: assume raiz única.
        try:
            return candidate
        except Exception:
            continue
    return None


class DryRunScheduler:
    """Agendamento persistido com execução SOMENTE em dry-run.

    Args:
        conn: conexão `sqlite3.Connection` com `migrate()` já aplicado pelo
            chamador (não migra aqui; erro de schema propaga, sem silenciar).
        scanner: opcional, objeto com `scan(...)` de LEITURA. Pode ser stub
            de teste com `scan(*, dry_run=True)` ou adaptador sobre a raiz
            real. Chamado sempre com `dry_run=True`.
        classifier: opcional, objeto com `classify(file)` de LEITURA.
        clock: opcional, `() -> datetime` injetável para testes. Padrão
            `datetime.now(timezone.utc)`.
        run_log: opcional, lista (ou dict com chave "events") onde eventos
            de execução são anexados para observação sem FS. Se None, uma
            lista interna é criada e exposta em `self.run_log`.

    Garantia estrutural: esta classe não recebe, importa ou aciona a camada
    de movimentação; só leitura + auditoria em `operations_log`.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        scanner: Any | None = None,
        classifier: Any | None = None,
        clock: Any | None = None,
        run_log: Any | None = None,
    ) -> None:
        self._conn = conn
        self._scanner = scanner
        self._classifier = classifier
        if clock is None:
            self._clock = lambda: datetime.now(timezone.utc)
        else:
            self._clock = clock
        if run_log is None:
            self.run_log: Any = []
        else:
            self.run_log = run_log
        self._running: set[str] = set()
        self._lock = threading.Lock()

    # -- observabilidade -------------------------------------------------

    def _append_log(self, event: dict[str, Any]) -> None:
        """Anexa evento a `run_log` (lista ou dict com "events")."""
        target = self.run_log
        try:
            if isinstance(target, list):
                target.append(event)
            elif isinstance(target, dict):
                target.setdefault("events", []).append(event)
            else:
                append = getattr(target, "append", None)
                if callable(append):
                    append(event)
        except Exception:
            pass  # observabilidade nunca derruba a execução

    def _now(self) -> datetime:
        return self._clock()  # type: ignore[no-any-return]

    # -- persistência de agendas -----------------------------------------

    def save_schedule(self, schedule: Schedule) -> Schedule:
        """Insere ou atualiza (upsert) por `schedule_id`. Retorna a agenda."""
        if not isinstance(schedule, Schedule):
            raise ValueError("esperado Schedule")
        with self._conn:
            self._conn.execute(
                _UPSERT_SCHEDULE_SQL,
                (
                    schedule.schedule_id,
                    schedule.name,
                    schedule.cron_expr,
                    1 if schedule.enabled else 0,
                    1 if schedule.dry_run else 0,
                    schedule.last_run_at,
                ),
            )
        return schedule

    def upsert(self, schedule: Schedule) -> Schedule:
        """Alias de `save_schedule`."""
        return self.save_schedule(schedule)

    def save(self, schedule: Schedule) -> Schedule:
        """Alias de `save_schedule`."""
        return self.save_schedule(schedule)

    def get_schedule(self, schedule_id: str) -> Schedule | None:
        """Retorna a agenda por id ou None se ausente."""
        row = self._conn.execute(_SELECT_SCHEDULE_SQL, (schedule_id,)).fetchone()
        if row is None:
            return None
        return _row_to_schedule(row)

    def get(self, schedule_id: str) -> Schedule | None:
        """Alias de `get_schedule`."""
        return self.get_schedule(schedule_id)

    def list_schedules(self) -> list[Schedule]:
        """Lista todas ordenadas por `schedule_id`."""
        rows = self._conn.execute(_LIST_SCHEDULES_SQL).fetchall()
        return [_row_to_schedule(r) for r in rows]

    def list(self) -> list[Schedule]:
        """Alias de `list_schedules`."""
        return self.list_schedules()

    def enable(self, schedule_id: str) -> Schedule | None:
        """Habilita (`enabled=1`) e retorna a agenda atualizada ou None."""
        with self._conn:
            self._conn.execute(
                "UPDATE schedules SET enabled = 1 WHERE schedule_id = ?;",
                (schedule_id,),
            )
        return self.get_schedule(schedule_id)

    def disable(self, schedule_id: str) -> Schedule | None:
        """Desabilita (`enabled=0`) e retorna a agenda atualizada ou None."""
        with self._conn:
            self._conn.execute(
                "UPDATE schedules SET enabled = 0 WHERE schedule_id = ?;",
                (schedule_id,),
            )
        return self.get_schedule(schedule_id)

    # -- cálculo de próxima execução -------------------------------------

    def next_run(self, schedule: Schedule) -> str | None:
        """Retorna ISO-8601 da próxima execução ou None se desabilitado.

        - Desabilitado -> None (nunca agenda).
        - `cron_expr` vazio/inválido -> None (sem reprocessamento cego).
        - Sem `last_run_at` (nunca rodou) -> agora (vencido, roda já).
        - Com `last_run_at` inválido -> agora (tratado como vencido).
        - Senão `last_run_at + intervalo` (pode estar no passado = vencido).
        """
        if not schedule.enabled:
            return None
        interval = parse_interval_seconds(schedule.cron_expr)
        if interval is None:
            return None
        now = self._now()
        if schedule.last_run_at is None:
            return now.isoformat()
        last = _parse_iso_or_none(schedule.last_run_at)
        if last is None:
            return now.isoformat()
        nxt = last + timedelta(seconds=interval)
        return nxt.isoformat()

    def is_due(self, schedule: Schedule, now: datetime | None = None) -> bool:
        """Diz se a agenda está vencida (deve rodar agora, catch-up único)."""
        if not schedule.enabled:
            return False
        interval = parse_interval_seconds(schedule.cron_expr)
        if interval is None:
            return False
        cur = now if now is not None else self._now()
        if schedule.last_run_at is None:
            return True
        last = _parse_iso_or_none(schedule.last_run_at)
        if last is None:
            return True
        nxt = _as_aware_utc(last + timedelta(seconds=interval))
        return nxt <= _as_aware_utc(cur)

    def due_schedules(self, now: datetime | None = None) -> list[Schedule]:
        """Lista agendas habilitadas e vencidas (snapshot, sem executar)."""
        cur = now if now is not None else self._now()
        return [s for s in self.list_schedules() if self.is_due(s, cur)]

    # -- leitura (scanner/classifier) ------------------------------------

    def _call_scan(self) -> list[Any]:
        """Chama `scanner.scan` com `dry_run=True`. Retorna lista (pode ser vazia)."""
        scanner = self._scanner
        if scanner is None:
            return []
        scan = getattr(scanner, "scan", None)
        if scan is None:
            if callable(scanner):
                result = scanner(dry_run=True)
                return list(result) if result is not None else []
            return []
        # Inspção de assinatura: stub de teste aceita zero posicionais;
        # scanner real exige `root` -> descobre raiz exposta ou falha auditada.
        try:
            sig = inspect.signature(scan)
            params = list(sig.parameters.values())
            # Conta posicionais obrigatórios (exclui *args/kw-only).
            required_positional = [
                p
                for p in params
                if p.default is inspect.Parameter.empty
                and p.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
        except (TypeError, ValueError):
            required_positional = []
        if not required_positional:
            result = scan(dry_run=True)
            return list(result) if result is not None else []
        root = _discover_scan_root(scanner)
        if root is None:
            raise TypeError(
                "scanner exige `root` posicional e nenhuma raiz foi exposta; "
                "forneça adaptador com `scan(*, dry_run=True)`"
            )
        result = scan(root, dry_run=True)
        return list(result) if result is not None else []

    def _call_classify(self, files: Sequence[Any]) -> list[Any]:
        """Classifica cada arquivo via `classifier.classify`. Lista (pode ser vazia)."""
        classifier = self._classifier
        if classifier is None:
            return []
        classify = getattr(classifier, "classify", None)
        if classify is None:
            return []
        out: list[Any] = []
        for item in files:
            out.append(classify(item))
        return out

    def _update_last_run(self, schedule: Schedule, now_iso: str) -> None:
        """Avança `last_run_at` para `now_iso`; insere a linha se ausente."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE schedules SET last_run_at = ? WHERE schedule_id = ?;",
                (now_iso, schedule.schedule_id),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    _UPSERT_SCHEDULE_SQL,
                    (
                        schedule.schedule_id,
                        schedule.name,
                        schedule.cron_expr,
                        1 if schedule.enabled else 0,
                        1 if schedule.dry_run else 0,
                        now_iso,
                    ),
                )

    # -- execução dry-run --------------------------------------------------

    def run_once(
        self, schedule: Schedule, *, dry_run: bool = True
    ) -> OperationRecord | None:
        """Executa UMA vez em dry-run (só leitura + auditoria).

        - `dry_run=False` (parâmetro ou `schedule.dry_run=False`) -> ValueError.
        - Desabilitado (objeto ou linha fresca no DB) -> None + log
          `skipped_disabled` (desabilitado nunca roda, sem catch-up).
        - Mesmo `schedule_id` já em execução nesta instância -> None + log
          `skipped_overlap` (sem duplicar trabalho).
        - Sucesso: leitura + auditoria `verificada` + `last_run_at=agora` +
          log `ok`; retorna `OperationRecord` verificada.
        - Falha de leitura: persiste `falhou` + `last_run_at=agora` + log
          `error`; retorna None sem propagar (sem perda silenciosa).
        Libera a trava em `finally` (sem trava fantasma).
        """
        if dry_run is not True:
            raise ValueError(
                "DryRunScheduler opera apenas em dry-run: "
                "dry_run=False recusado (nunca movimentação real)"
            )
        if getattr(schedule, "dry_run", True) is not True:
            raise ValueError(
                "agenda com dry_run=False recusada: "
                "agendador nunca realiza movimentação real"
            )
        if not schedule.enabled:
            self._append_log(
                {
                    "schedule_id": schedule.schedule_id,
                    "status": "skipped_disabled",
                    "at": self._now().isoformat(),
                    "reason": "agenda desabilitada (objeto)",
                }
            )
            return None
        # Trava em memória ANTES de qualquer I/O (concorrência por id).
        with self._lock:
            if schedule.schedule_id in self._running:
                overlapped = True
            else:
                self._running.add(schedule.schedule_id)
                overlapped = False
        if overlapped:
            self._append_log(
                {
                    "schedule_id": schedule.schedule_id,
                    "status": "skipped_overlap",
                    "at": self._now().isoformat(),
                    "reason": "execução concorrente do mesmo schedule_id recusada",
                }
            )
            return None
        try:
            # Guarda contra objeto obsoleto: DB fresco manda em `enabled`.
            try:
                fresh = self._conn.execute(
                    "SELECT enabled FROM schedules WHERE schedule_id = ?;",
                    (schedule.schedule_id,),
                ).fetchone()
            except Exception:
                fresh = None
            if fresh is not None:
                try:
                    fresh_enabled = int(fresh["enabled"] if "enabled" in fresh.keys() else fresh[0])
                except Exception:
                    fresh_enabled = int(fresh[0])
                if fresh_enabled == 0:
                    self._append_log(
                        {
                            "schedule_id": schedule.schedule_id,
                            "status": "skipped_disabled",
                            "at": self._now().isoformat(),
                            "reason": "agenda desabilitada (DB)",
                        }
                    )
                    return None
            try:
                files = self._call_scan()
                results = self._call_classify(files)
                scanned = len(files)
                classified = len(results)
                try:
                    suggested = sum(1 for r in results if getattr(r, "destination", None))
                except Exception:
                    suggested = 0
                now_iso = self._now().isoformat()
                operation_id = f"op-{schedule.schedule_id}-{uuid.uuid4().hex[:12]}"
                plan_id = f"plan-{schedule.schedule_id}-{uuid.uuid4().hex[:8]}"
                # Hash vinculado ao conteúdo sugerido quando disponível.
                try:
                    from hexavante.models.entities import ClassificationResult as _CR

                    if results and all(isinstance(r, _CR) for r in results):
                        plan_hash = compute_plan_hash(plan_id, tuple(results))  # type: ignore[arg-type]
                    else:
                        plan_hash = hashlib.sha256(
                            f"{schedule.schedule_id}:{now_iso}:{scanned}:{classified}".encode(
                                "utf-8"
                            )
                        ).hexdigest()
                except Exception:
                    plan_hash = hashlib.sha256(
                        f"{schedule.schedule_id}:{now_iso}:{scanned}:{classified}".encode(
                            "utf-8"
                        )
                    ).hexdigest()
                message = (
                    f"dry-run agenda '{schedule.schedule_id}' ({schedule.name}): "
                    f"{scanned} arquivo(s) varrido(s), {classified} classificado(s), "
                    f"{suggested} com destino sugerido; nenhuma movimentação realizada"
                )
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO operations_log "
                        "(operation_id, plan_id, plan_hash, state, approval_token, "
                        " message, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                        (
                            operation_id,
                            plan_id,
                            plan_hash,
                            OperationState.VERIFICADA.value,
                            None,
                            message,
                            now_iso,
                            now_iso,
                        ),
                    )
                    cur = self._conn.execute(
                        "UPDATE schedules SET last_run_at = ? WHERE schedule_id = ?;",
                        (now_iso, schedule.schedule_id),
                    )
                    if cur.rowcount == 0:
                        self._conn.execute(
                            _UPSERT_SCHEDULE_SQL,
                            (
                                schedule.schedule_id,
                                schedule.name,
                                schedule.cron_expr,
                                1 if schedule.enabled else 0,
                                1 if schedule.dry_run else 0,
                                now_iso,
                            ),
                        )
                self._append_log(
                    {
                        "schedule_id": schedule.schedule_id,
                        "status": "ok",
                        "at": now_iso,
                        "scanned": scanned,
                        "classified": classified,
                        "suggested": suggested,
                        "operation_id": operation_id,
                    }
                )
                return OperationRecord(
                    operation_id=operation_id,
                    plan_id=plan_id,
                    plan_hash=plan_hash,
                    state=OperationState.VERIFICADA,
                    approval_token=None,
                    message=message,
                    created_at=now_iso,
                    updated_at=now_iso,
                )
            except Exception as exc:
                now_iso = self._now().isoformat()
                operation_id = f"op-{schedule.schedule_id}-{uuid.uuid4().hex[:12]}"
                plan_id = f"plan-{schedule.schedule_id}-{uuid.uuid4().hex[:8]}"
                plan_hash = hashlib.sha256(
                    f"{schedule.schedule_id}:{now_iso}:erro".encode("utf-8")
                ).hexdigest()
                message = (
                    f"dry-run agenda '{schedule.schedule_id}' falhou: {exc!r}; "
                    f"nenhuma movimentação realizada"
                )
                try:
                    with self._conn:
                        self._conn.execute(
                            "INSERT INTO operations_log "
                            "(operation_id, plan_id, plan_hash, state, approval_token, "
                            " message, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                            (
                                operation_id,
                                plan_id,
                                plan_hash,
                                OperationState.FALHOU.value,
                                None,
                                message,
                                now_iso,
                                now_iso,
                            ),
                        )
                        cur = self._conn.execute(
                            "UPDATE schedules SET last_run_at = ? WHERE schedule_id = ?;",
                            (now_iso, schedule.schedule_id),
                        )
                        if cur.rowcount == 0:
                            self._conn.execute(
                                _UPSERT_SCHEDULE_SQL,
                                (
                                    schedule.schedule_id,
                                    schedule.name,
                                    schedule.cron_expr,
                                    1 if schedule.enabled else 0,
                                    1 if schedule.dry_run else 0,
                                    now_iso,
                                ),
                            )
                except Exception as persist_exc:
                    self._append_log(
                        {
                            "schedule_id": schedule.schedule_id,
                            "status": "error",
                            "at": now_iso,
                            "error": repr(exc),
                            "persist_error": repr(persist_exc),
                        }
                    )
                    return None
                self._append_log(
                    {
                        "schedule_id": schedule.schedule_id,
                        "status": "error",
                        "at": now_iso,
                        "error": repr(exc),
                        "operation_id": operation_id,
                    }
                )
                return None
        finally:
            with self._lock:
                self._running.discard(schedule.schedule_id)

    def run_due_all(
        self, *, dry_run: bool = True, now: datetime | None = None
    ) -> list[OperationRecord | None]:
        """Executa cada agenda vencida UMA vez (catch-up único).

        Snapshot de `due_schedules(now)`; cada id vencido roda no máximo
        uma vez por chamada, mesmo com múltiplos intervalos perdidos.
        Desabilitadas e com cron inválido nunca rodam.
        """
        if dry_run is not True:
            raise ValueError(
                "DryRunScheduler opera apenas em dry-run: "
                "dry_run=False recusado (nunca movimentação real)"
            )
        cur = now if now is not None else self._now()
        due = [s for s in self.list_schedules() if self.is_due(s, cur)]
        out: list[OperationRecord | None] = []
        for sched in due:
            out.append(self.run_once(sched, dry_run=dry_run))
        return out

    def recover_after_restart(self) -> list[str]:
        """Recuperação pós-restart: limpa travas em memória (sem fantasma).

        Como `_running` é só memória por instância, após restart nada fica
        preso: nova instância nasce com conjunto vazio e roda normalmente.
        Este método apenas esvazia o conjunto local e retorna [] (nenhuma
        agenda fica bloqueada). Não toca no DB (sem flag persistida).
        """
        with self._lock:
            self._running.clear()
        return []
