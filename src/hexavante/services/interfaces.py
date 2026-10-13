"""Contratos (Protocols) dos serviços — Sprint 00.

INVARIANTE DE SEGURANÇA (inegociável, cf. AGENTS.md):
- `MoveService` é a ÚNICA camada autorizada a escrever nos destinos.
  Views, controllers, scanners, classifiers, rule engines, monitores e
  schedulers NÃO escrevem diretamente em destinos; retornam dados/planos.
- A API de movimentação separa `preview`, `execute(approval)` e `undo`:
  `preview` nunca escreve; `execute` exige `approval` válida vinculada ao
  hash do plano; `undo` verifica origem/destino e conflitos antes de
  reverter e pode recusar com explicação.
- Dry-run é o padrão (`dry_run=True`). Nenhum watcher/agendador executa
  movimentação real sem confirmação explícita de origem e destino.
- A aprovação é vinculada ao hash do plano (`Approval.plan_hash` deve ser
  igual a `MovePlan.plan_hash`). NUNCA reutilizar uma aprovação após
  qualquer mudança no arquivo ou no plano — nova mudança exige nova
  aprovação explícita.
- Operações atômicas sem overwrite e com proteção a colisão; entre
  filesystems, remoção da origem somente após integridade verificada e
  estado persistido. Se a segurança não puder ser demonstrada, bloquear.

Este módulo é puro Python: sem `gi`, sem I/O, sem efeitos na importação.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from hexavante.models.entities import (
    ClassificationResult,
    FileRecord,
    MovePlan,
    OperationRecord,
    OperationState,
    Rule,
    Schedule,
)


@dataclass(frozen=True)
class Approval:
    """Aprovação explícita vinculada a um plano específico.

    `plan_hash` DEVE ser igual ao `MovePlan.plan_hash` vigente no momento
    da execução. Qualquer divergência (plano alterado, arquivo alterado)
    invalida a aprovação e `MoveService.execute` deve recusar com
    `PermissionError` (ou `ValueError` para hash malformado).
    """

    plan_id: str
    plan_hash: str
    approver: str = ""
    scope_origin: str = ""
    scope_destination: str = ""

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("Approval.plan_id não pode ser vazio")
        if not self.plan_hash:
            raise ValueError("Approval.plan_hash não pode ser vazio")


@runtime_checkable
class Scanner(Protocol):
    """Inventaria raízes autorizadas. Nunca escreve em destino."""

    def scan(self, root: Path, *, dry_run: bool = True) -> Sequence[FileRecord]:
        """Retorna registros observados. Não segue symlinks (Sprint 01)."""
        ...


@runtime_checkable
class Classifier(Protocol):
    """Sugere destino para um arquivo. Não executa I/O de escrita."""

    def classify(self, file: FileRecord) -> ClassificationResult:
        """Retorna sugestão determinística com justificativa."""
        ...


@runtime_checkable
class RuleEngine(Protocol):
    """Avalia regras persistentes. Apenas retorna dados/planos."""

    def suggest(self, file: FileRecord, rules: Sequence[Rule]) -> ClassificationResult:
        """Sugere destino aplicando regras por prioridade."""
        ...

    def list_rules(self) -> Sequence[Rule]:
        """Lista regras persistidas."""
        ...


@runtime_checkable
class MoveService(Protocol):
    """ÚNICA camada autorizada a escrever em destinos.

    - `preview` valida e retorna o plano sem nenhum write no filesystem.
    - `execute` exige `approval` vinculada ao hash do plano; recusa
      (`PermissionError`/`ValueError`) se `approval.plan_hash != plan.plan_hash`
      ou `approval.plan_id != plan.plan_id`.
    - `undo` verifica origem/destino e conflitos antes de reverter e pode
      recusar com explicação (raise com mensagem).
    """

    def preview(self, plan: MovePlan) -> MovePlan:
        """Valida o plano sem efeitos colaterais."""
        ...

    def execute(self, plan: MovePlan, approval: Approval) -> OperationRecord:
        """Executa o plano APENAS com aprovação válida vinculada ao hash."""
        ...

    def undo(self, operation: OperationRecord) -> OperationRecord:
        """Reverte operação após verificar origem/destino; pode recusar."""
        ...


@runtime_checkable
class DuplicateDetector(Protocol):
    """Detecta duplicados via SHA-256. Apenas sugere; nunca apaga/limpa."""

    def find_duplicates(
        self, files: Sequence[FileRecord]
    ) -> Sequence[Sequence[FileRecord]]:
        """Agrupa arquivos com mesmo hash. Sem efeitos destrutivos."""
        ...


@runtime_checkable
class Monitor(Protocol):
    """Observa filesystem (Gio.FileMonitor na Sprint 05). Só sugere."""

    def start(self, root: Path, *, dry_run: bool = True) -> None:
        """Inicia observação em dry-run por padrão; não move nada."""
        ...

    def stop(self) -> None:
        """Encerra observação."""
        ...


@runtime_checkable
class Scheduler(Protocol):
    """Agendamento persistido. Execução real apenas em dry-run na v0.1."""

    def next_run(self, schedule: Schedule) -> str | None:
        """Retorna ISO-8601 da próxima execução ou None se desabilitado."""
        ...

    def run_once(
        self, schedule: Schedule, *, dry_run: bool = True
    ) -> OperationRecord | None:
        """Executa uma vez em dry-run; nunca movimentação real sem approval."""
        ...


__all__ = [
    "Approval",
    "Classifier",
    "DuplicateDetector",
    "Monitor",
    "MoveService",
    "RuleEngine",
    "Scanner",
    "Scheduler",
]
