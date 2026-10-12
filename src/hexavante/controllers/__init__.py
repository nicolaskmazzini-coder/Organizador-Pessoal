"""Controller fino — delega a serviços, nunca escreve em destino.

Invariante: controllers NÃO escrevem no filesystem de destino; apenas
orquestram chamadas a serviços. `MoveService` é a única camada que pode
escrever (Sprints 03+). Dry-run por padrão.

Sprint 04: métodos de apresentação (`list_suggestions`, `describe_plan`,
`request_approval`, `history`) apenas montam dados ou leem `operations_log`
(SELECT); nunca escrevem em destino. Views consomem estes métodos; seleção
de lista nunca é autorização implícita — execução exige `Approval` válida
vinculada ao `plan_hash` via `execute`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from hexavante.models.entities import (
    ClassificationResult,
    FileRecord,
    MovePlan,
    OperationRecord,
)
from hexavante.services.interfaces import Approval


@dataclass
class AppController:
    """Orquestrador delgado (Sprint 00: sem lógica de negócio real)."""

    dry_run: bool = True
    scanner: object | None = None
    classifier: object | None = None
    move_service: object | None = None

    def health_check(self) -> dict[str, object]:
        """Retorna status sem efeitos colaterais (usado por --headless)."""
        return {
            "ok": True,
            "dry_run": self.dry_run,
            "has_scanner": self.scanner is not None,
            "has_classifier": self.classifier is not None,
            "has_move_service": self.move_service is not None,
        }

    def preview(self, plan: MovePlan) -> MovePlan:
        """Delega preview ao MoveService; sem MoveService, retorna o plano."""
        if self.move_service is None:
            return plan
        preview_fn = getattr(self.move_service, "preview")
        return preview_fn(plan)  # type: ignore[no-any-return]

    def execute(self, plan: MovePlan, approval: Approval) -> OperationRecord:
        """Delega execução; exige MoveService configurado."""
        if self.move_service is None:
            raise RuntimeError("MoveService não configurado (Sprint 00: sem execução real)")
        execute_fn = getattr(self.move_service, "execute")
        return execute_fn(plan, approval)  # type: ignore[no-any-return]

    def scan_preview(self, root: Path) -> Sequence[FileRecord]:
        """Delega varredura; sem scanner, retorna lista vazia (sem I/O)."""
        if self.scanner is None:
            return []
        scan_fn = getattr(self.scanner, "scan")
        return scan_fn(root, dry_run=True)  # type: ignore[no-any-return]

    # -- Sprint 04: apresentação (sem escrita em destino) ---------------

    def list_suggestions(
        self,
        scan_results: Sequence[FileRecord],
        classifications: Sequence[ClassificationResult],
    ) -> list[object]:
        """Monta linhas de sugestão pareando arquivos e classificações.

        Puro (sem I/O, sem escrita): indexa `classifications` por
        `file_path`; arquivo sem classificação gera linha com destino
        ausente e motivo de revisão. Retorna `SuggestionRow`s.
        """
        from hexavante.views.models import SuggestionRow

        by_path: dict[str, ClassificationResult] = {}
        for cls in classifications:
            by_path[cls.file_path] = cls
        rows: list[object] = []
        for f in scan_results:
            cls = by_path.get(f.path)
            if cls is None:
                rows.append(
                    SuggestionRow(
                        file_path=f.path,
                        destination=None,
                        confidence=0.0,
                        reason="sem classificação — requer revisão",
                        selected=False,
                    )
                )
            else:
                rows.append(
                    SuggestionRow(
                        file_path=cls.file_path,
                        destination=cls.destination,
                        confidence=cls.confidence,
                        reason=cls.reason,
                        selected=False,
                    )
                )
        return rows

    def describe_plan(
        self,
        plan: MovePlan,
        selections: Sequence[bool] | Sequence[int] | Mapping[int, bool] | None = None,
    ) -> object:
        """Resumo do lote (`PlanSummary`) para exibição de aprovação.

        Sem `selections`, descreve o plano integral (todos selecionados).
        Puro, sem escrita.
        """
        from hexavante.views.models import format_plan_summary

        if selections is None:
            selections = [True] * len(plan.items)
        return format_plan_summary(plan, selections)

    def request_approval(
        self,
        plan: MovePlan,
        selections: Sequence[bool] | Sequence[int] | Mapping[int, bool] | None,
        approver: str = "",
    ) -> Approval:
        """Cria `Approval` explícita vinculada ao `plan_hash` (sem escrita).

        Recusa (`ValueError`) se: plano ausente, `plan_id`/`plan_hash`
        ausentes, seleções vazias/malformadas/fora do intervalo, ou item
        selecionado sem destino (bloqueio). Nunca reutiliza aprovação:
        sempre cria objeto novo vinculado ao hash vigente.

        `approver` identifica o humano que clicou "Aprovar lote".
        """
        from hexavante.views.models import _selected_indices

        if plan is None:
            raise ValueError("plano ausente — nada a aprovar")
        plan_id = getattr(plan, "plan_id", "")
        plan_hash = getattr(plan, "plan_hash", "")
        if not plan_id:
            raise ValueError("plano sem plan_id — aprovação bloqueada")
        if not plan_hash:
            raise ValueError("plano sem plan_hash — aprovação bloqueada; gere o plano novamente")
        try:
            n_items = len(plan.items)
        except TypeError:
            raise ValueError("plano inválido — itens ausentes")
        indices, err = _selected_indices(n_items, selections)
        if err is not None:
            raise ValueError(err)
        if not indices:
            raise ValueError(
                "nenhum item selecionado — marque ao menos um checkbox e clique em 'Aprovar lote'"
            )
        origins: list[str] = []
        destinations: list[str] = []
        for i in indices:
            try:
                item = plan.items[i]
            except (IndexError, KeyError, TypeError):
                raise ValueError(f"seleção fora do intervalo — índice {i} inválido")
            dest = getattr(item, "destination", None)
            if dest is None or not str(dest).strip():
                fp = getattr(item, "file_path", f"índice {i}")
                raise ValueError(
                    f"lote bloqueado — '{fp}' sem destino; remova-o da seleção ou revise"
                )
            origins.append(str(getattr(item, "file_path", "")))
            destinations.append(str(dest))
        return Approval(
            plan_id=plan_id,
            plan_hash=plan_hash,
            approver=(approver or ""),
            scope_origin=";".join(origins),
            scope_destination=";".join(destinations),
        )

    def history(self, conn: sqlite3.Connection) -> list[object]:
        """Lê `operations_log` (somente SELECT, sem escrita).

        Retorna `HistoryRow`s ordenados por `created_at`. Tabela vazia →
        lista vazia (estado vazio na UI).
        """
        from hexavante.views.models import HistoryRow

        if conn is None:
            raise ValueError("conn ausente — histórico requer conexão SQLite")
        rows = conn.execute(
            "SELECT operation_id, state, message FROM operations_log "
            "ORDER BY created_at ASC, operation_id ASC;"
        ).fetchall()
        return [
            HistoryRow(
                operation_id=str(r["operation_id"]),
                state=str(r["state"]),
                message=str(r["message"] or ""),
            )
            for r in rows
        ]
