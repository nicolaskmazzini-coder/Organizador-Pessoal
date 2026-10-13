"""Entidades puras do domínio (Sprint 00).

Invariantes:
- Módulo puro Python: sem `import gi`, sem I/O, sem acesso a filesystem,
  sem acesso a rede, sem efeitos colaterais na importação.
- Todas as entidades são `dataclasses` frozen (imutáveis) e totalmente
  tipadas, para que planos e aprovações sejam rastreáveis e comparáveis.
- `OperationState` cobre os estados persistentes de operação:
  planejada, aprovada, em_andamento, verificada, concluída, falhou,
  recuperação pendente. Os valores persistidos são ASCII snake_case
  (ex.: "concluida", "recuperacao_pendente"); a forma de exibição com
  acentos deve ser resolvida na camada de view.
- Serialização: `dataclasses.asdict()` / reconstrução via `**dict`.
  Nenhum campo usa tipo que impeça `asdict` (tuplas, str, int, float,
  bool, None, Enum).
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from enum import Enum


class OperationState(str, Enum):
    """Estados persistentes de operação.

    Valores persistidos (ASCII, estáveis para CHECK constraint no SQLite):
    - PLANEJADA -> "planejada"
    - APROVADA -> "aprovada"
    - EM_ANDAMENTO -> "em_andamento" (exibição: "em andamento")
    - VERIFICADA -> "verificada"
    - CONCLUIDA -> "concluida" (exibição: "concluída")
    - FALHOU -> "falhou"
    - RECUPERACAO_PENDENTE -> "recuperacao_pendente" (exibição: "recuperação pendente")
    """

    PLANEJADA = "planejada"
    APROVADA = "aprovada"
    EM_ANDAMENTO = "em_andamento"
    VERIFICADA = "verificada"
    CONCLUIDA = "concluida"
    FALHOU = "falhou"
    RECUPERACAO_PENDENTE = "recuperacao_pendente"


@dataclass(frozen=True)
class FileRecord:
    """Inventário de um arquivo observado (sem I/O neste módulo)."""

    path: str
    size_bytes: int = 0
    mtime_ns: int = 0
    mime_type: str = ""
    sha256: str | None = None
    is_stable: bool = False

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("FileRecord.path não pode ser vazio")
        if self.size_bytes < 0:
            raise ValueError("FileRecord.size_bytes não pode ser negativo")
        if self.mtime_ns < 0:
            raise ValueError("FileRecord.mtime_ns não pode ser negativo")
        if self.sha256 is not None and len(self.sha256) != 64:
            raise ValueError("FileRecord.sha256 deve ter 64 hex chars ou None")


@dataclass(frozen=True)
class FolderRecord:
    """Pasta conhecida; varredura só em raízes autorizadas (regra de segurança)."""

    path: str
    is_authorized_root: bool = False
    last_scan_at: str | None = None

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("FolderRecord.path não pode ser vazio")


@dataclass(frozen=True)
class Rule:
    """Regra determinística de classificação -> destino sugerido."""

    rule_id: str
    name: str
    pattern: str
    destination: str
    priority: int = 0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise ValueError("Rule.rule_id não pode ser vazio")
        if not self.name:
            raise ValueError("Rule.name não pode ser vazio")
        if not self.pattern:
            raise ValueError("Rule.pattern não pode ser vazio")
        if not self.destination:
            raise ValueError("Rule.destination não pode ser vazio")


@dataclass(frozen=True)
class ClassificationResult:
    """Sugestão determinística para um arquivo (não move nada)."""

    file_path: str
    rule_id: str | None
    destination: str | None
    confidence: float
    reason: str

    def __post_init__(self) -> None:
        if not self.file_path:
            raise ValueError("ClassificationResult.file_path não pode ser vazio")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("ClassificationResult.confidence deve estar em [0.0, 1.0]")
        if not self.reason:
            raise ValueError("ClassificationResult.reason não pode ser vazio")


@dataclass(frozen=True)
class MovePlan:
    """Plano de movimentação (dry-run por padrão). Não executa I/O.

    `items` agrega `ClassificationResult` como itens do plano para evitar
    novo tipo na Sprint 00. `plan_hash` vincula a aprovação: qualquer
    mudança no plano exige nova aprovação (nunca reutilizar approval).
    """

    plan_id: str
    items: tuple[ClassificationResult, ...]
    plan_hash: str
    dry_run: bool = True
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("MovePlan.plan_id não pode ser vazio")
        if not self.plan_hash:
            raise ValueError("MovePlan.plan_hash não pode ser vazio")
        # Normaliza listas para tupla preservando frozen/hashable.
        if isinstance(self.items, list):  # type: ignore[unreachable]
            object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True)
class OperationRecord:
    """Registro auditável de uma operação (trilha persistida em operations_log)."""

    operation_id: str
    plan_id: str
    plan_hash: str
    state: OperationState
    approval_token: str | None = None
    message: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("OperationRecord.operation_id não pode ser vazio")
        if not self.plan_id:
            raise ValueError("OperationRecord.plan_id não pode ser vazio")
        if not self.plan_hash:
            raise ValueError("OperationRecord.plan_hash não pode ser vazio")
        if not isinstance(self.state, OperationState):
            raise ValueError("OperationRecord.state deve ser OperationState")


@dataclass(frozen=True)
class Schedule:
    """Agendamento persistido. Execução real apenas em dry-run na v0.1."""

    schedule_id: str
    name: str
    cron_expr: str = ""
    enabled: bool = True
    dry_run: bool = True
    last_run_at: str | None = None

    def __post_init__(self) -> None:
        if not self.schedule_id:
            raise ValueError("Schedule.schedule_id não pode ser vazio")
        if not self.name:
            raise ValueError("Schedule.name não pode ser vazio")


def compute_plan_hash(plan_id: str, items: tuple[ClassificationResult, ...]) -> str:
    """Hash determinístico SHA-256 do plano (puro, sem I/O).

    Usado para vincular approval ao plano: `MoveService.execute` deve
    recusar approval cujo `plan_hash` difira do hash atual do plano.
    """
    h = hashlib.sha256()
    h.update(plan_id.encode("utf-8"))
    h.update(b"\x00")
    for item in items:
        h.update(item.file_path.encode("utf-8"))
        h.update(b"\x00")
        h.update((item.rule_id or "").encode("utf-8"))
        h.update(b"\x00")
        h.update((item.destination or "").encode("utf-8"))
        h.update(b"\x00")
        h.update(repr(item.confidence).encode("utf-8"))
        h.update(b"\x00")
        h.update(item.reason.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


__all__ = [
    "ClassificationResult",
    "FileRecord",
    "FolderRecord",
    "MovePlan",
    "OperationRecord",
    "OperationState",
    "Rule",
    "Schedule",
    "compute_plan_hash",
]

# Garantia estática de pureza: este módulo nunca deve importar gi/Gtk.
assert "gi" not in globals(), "entities.py não deve importar gi"
