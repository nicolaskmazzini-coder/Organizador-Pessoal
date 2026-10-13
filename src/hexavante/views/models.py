"""Modelos de apresentação puros (Sprint 04).

Módulo PURO: sem `gi`, sem I/O, sem efeitos na importação. Toda lógica
de montagem de linhas, formatação de plano, validação de aprovação e
filtro é testável sem display (headless).

Invariantes de segurança (cf. AGENTS.md):
- Seleção de lista NUNCA é autorização implícita. `selected=True` apenas
  marca intenção de revisão; só uma `Approval` explícita vinculada ao
  `plan_hash` autoriza execução (via `MoveService`, fora desta camada).
- Nenhuma função aqui escreve em destino; apenas constrói dados.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from hexavante.models.entities import ClassificationResult, FileRecord, MovePlan

__all__ = [
    "SuggestionRow",
    "PlanSummary",
    "HistoryRow",
    "ErrorState",
    "EMPTY_SUGGESTIONS",
    "EMPTY_HISTORY",
    "EMPTY_RULES",
    "DISPLAY_STATE",
    "build_suggestion_rows",
    "format_plan_summary",
    "validate_approval_request",
    "filter_suggestions",
    "display_state",
]


@dataclass(frozen=True)
class SuggestionRow:
    """Linha de sugestão para exibição (checkbox = intenção, não autorização)."""

    file_path: str
    destination: str | None
    confidence: float
    reason: str
    selected: bool = False

    def __post_init__(self) -> None:
        if not self.file_path:
            raise ValueError("SuggestionRow.file_path não pode ser vazio")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("SuggestionRow.confidence deve estar em [0.0, 1.0]")
        if not self.reason:
            raise ValueError("SuggestionRow.reason não pode ser vazio")


@dataclass(frozen=True)
class PlanSummary:
    """Resumo de lote para o fluxo de aprovação explícita."""

    plan_id: str
    plan_hash: str
    n_items: int
    n_selected: int
    riscos: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.plan_id:
            raise ValueError("PlanSummary.plan_id não pode ser vazio")
        if not self.plan_hash:
            raise ValueError("PlanSummary.plan_hash não pode ser vazio")
        if self.n_items < 0 or self.n_selected < 0:
            raise ValueError("PlanSummary contadores não podem ser negativos")
        if self.n_selected > self.n_items:
            raise ValueError("PlanSummary.n_selected não pode exceder n_items")
        if isinstance(self.riscos, list):  # type: ignore[unreachable]
            object.__setattr__(self, "riscos", tuple(self.riscos))


@dataclass(frozen=True)
class HistoryRow:
    """Linha de histórico lida de `operations_log` (somente leitura)."""

    operation_id: str
    state: str
    message: str = ""

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("HistoryRow.operation_id não pode ser vazio")
        if not self.state:
            raise ValueError("HistoryRow.state não pode ser vazio")


@dataclass(frozen=True)
class ErrorState:
    """Estado vazio/erro compreensível para a UI (acessível, sem jargão)."""

    code: str
    message: str
    hint: str = ""

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("ErrorState.code não pode ser vazio")
        if not self.message:
            raise ValueError("ErrorState.message não pode ser vazio")


EMPTY_SUGGESTIONS = ErrorState(
    code="empty_suggestions",
    message="Nenhuma sugestão para exibir.",
    hint="Execute uma varredura em uma raiz autorizada para gerar sugestões.",
)

EMPTY_HISTORY = ErrorState(
    code="empty_history",
    message="Nenhuma operação registrada ainda.",
    hint="Aprove um lote para ver o histórico aqui.",
)

EMPTY_RULES = ErrorState(
    code="empty_rules",
    message="Nenhuma regra cadastrada.",
    hint="Cadastre uma regra para automatizar sugestões de destino.",
)

DISPLAY_STATE: dict[str, str] = {
    "planejada": "planejada",
    "aprovada": "aprovada",
    "em_andamento": "em andamento",
    "verificada": "verificada",
    "concluida": "concluída",
    "falhou": "falhou",
    "recuperacao_pendente": "recuperação pendente",
}


def display_state(state: str) -> str:
    """Forma de exibição com acentos a partir do valor persistido ASCII."""
    return DISPLAY_STATE.get(state, state)


def build_suggestion_rows(
    files: Sequence[FileRecord],
    classifications: Sequence[ClassificationResult],
) -> list[SuggestionRow]:
    """Monta linhas de sugestão (puro, sem escrita).

    Pareia por `file_path`. Arquivo sem classificação gera linha com
    `destination=None`, `confidence=0.0` e motivo de revisão — nunca
    autoriza movimentação.
    """
    by_path: dict[str, ClassificationResult] = {}
    for cls in classifications:
        # Última classificação por caminho prevalece (determinístico).
        by_path[cls.file_path] = cls
    rows: list[SuggestionRow] = []
    for f in files:
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


def _selected_indices(
    plan_len: int, selections: Sequence[bool] | Sequence[int] | Mapping[int, bool] | None
) -> tuple[list[int], str | None]:
    """Normaliza `selections` para índices ordenados.

    Aceita:
    - `None` → vazio;
    - `Mapping[int, bool]` (ex.: `{0: True, 2: True}`);
    - `Sequence[bool]` alinhada ao plano (checkbox por item);
    - `Sequence[int]` com índices selecionados.

    Retorna `(indices, erro)`. `erro` é None em caso de formato válido
    (mesmo que vazio — vazio é tratado pelo chamador como recusa).
    """
    if selections is None:
        return [], None
    if isinstance(selections, Mapping):
        indices: list[int] = []
        for k, v in selections.items():
            if type(k) is not int or type(v) is not bool:
                return [], "seleções inválidas — use {índice: True/False}"
            if v:
                indices.append(k)
        for i in indices:
            if not 0 <= i < plan_len:
                return [], f"seleção fora do intervalo — índice {i} inválido"
        return sorted(set(indices)), None
    if isinstance(selections, (list, tuple)):
        if len(selections) == 0:
            return [], None
        if all(type(x) is bool for x in selections):
            if len(selections) != plan_len:
                return (
                    [],
                    f"seleções inválidas — esperado {plan_len} marcadores, recebido {len(selections)}",
                )
            return [i for i, v in enumerate(selections) if v], None  # type: ignore[union-attr]
        if all(type(x) is int for x in selections):
            for i in selections:  # type: ignore[union-attr]
                if not 0 <= i < plan_len:  # type: ignore[operator]
                    return [], f"seleção fora do intervalo — índice {i} inválido"
            return sorted(set(selections)), None  # type: ignore[arg-type]
        return [], "seleções inválidas — use lista de booleanos ou de índices"
    return [], "seleções inválidas — tipo não suportado"


def format_plan_summary(
    plan: MovePlan,
    selections: Sequence[bool] | Sequence[int] | Mapping[int, bool] | None,
) -> PlanSummary:
    """Formata resumo do lote (puro). Nunca autoriza nem executa.

    `riscos` contém bloqueios/riscos por item selecionado:
    - destino ausente → bloqueio;
    - confiança < 0.5 → risco de revisão.
    """
    n_items = len(plan.items)
    indices, err = _selected_indices(n_items, selections)
    if err is not None:
        # Formato inválido: resumo com zero selecionados + motivo como risco.
        return PlanSummary(
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            n_items=n_items,
            n_selected=0,
            riscos=(err,),
        )
    riscos: list[str] = []
    if not indices:
        riscos.append("nenhum item selecionado — aprovação bloqueada")
    for i in indices:
        item = plan.items[i]
        dest = (item.destination or "").strip()
        if not dest:
            riscos.append(f"bloqueio: '{item.file_path}' sem destino — requer revisão")
        elif item.confidence < 0.5:
            riscos.append(
                f"risco: '{item.file_path}' com confiança baixa "
                f"({item.confidence:.2f}) — requer revisão"
            )
    return PlanSummary(
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        n_items=n_items,
        n_selected=len(indices),
        riscos=tuple(riscos),
    )


def validate_approval_request(
    plan: MovePlan | None,
    selections: Sequence[bool] | Sequence[int] | Mapping[int, bool] | None,
    approval: object | None,
) -> tuple[bool, str]:
    """Valida pedido de aprovação do lote (puro, sem escrita).

    Retorna `(ok, motivo)` em pt-BR. Recusa (ok=False) quando:
    - plano ausente ou sem `plan_id`/`plan_hash`;
    - seleções vazias, malformadas ou fora do intervalo;
    - item selecionado sem destino (bloqueio);
    - `approval` ausente (seleção sozinha nunca autoriza);
    - `approval` desvinculada (`plan_id`/`plan_hash` divergentes).

    `approval` é tipado como `object` para evitar import circular;
    acessa `plan_id`/`plan_hash` por atributo.
    """
    if plan is None:
        return False, "plano ausente — carregue um plano antes de aprovar"
    plan_id = getattr(plan, "plan_id", "")
    plan_hash = getattr(plan, "plan_hash", "")
    items = getattr(plan, "items", ())
    if not plan_id:
        return False, "plano sem identificador — aprovação bloqueada"
    if not plan_hash:
        return False, "plano sem plan_hash — aprovação bloqueada; gere o plano novamente"
    try:
        n_items = len(items)
    except TypeError:
        return False, "plano inválido — itens ausentes"
    indices, err = _selected_indices(n_items, selections)
    if err is not None:
        return False, err
    if not indices:
        return (
            False,
            "nenhum item selecionado — marque ao menos um checkbox e clique em 'Aprovar lote'",
        )
    # Bloqueio: item selecionado sem destino nunca pode ser aprovado.
    for i in indices:
        try:
            item = items[i]
        except (IndexError, KeyError, TypeError):
            return False, f"seleção fora do intervalo — índice {i} inválido"
        dest = getattr(item, "destination", None)
        if dest is None or not str(dest).strip():
            fp = getattr(item, "file_path", f"índice {i}")
            return (
                False,
                f"lote bloqueado — '{fp}' sem destino; remova-o da seleção ou revise",
            )
    if approval is None:
        return (
            False,
            "aprovação explícita ausente — seleção sozinha não autoriza; clique em 'Aprovar lote'",
        )
    ap_plan_id = getattr(approval, "plan_id", None)
    ap_plan_hash = getattr(approval, "plan_hash", None)
    if not ap_plan_id or not ap_plan_hash:
        return False, "aprovação inválida — plan_id/plan_hash ausentes"
    if ap_plan_id != plan_id:
        return False, "aprovação vinculada a outro plano (plan_id divergente) — gere nova aprovação"
    if ap_plan_hash != plan_hash:
        return (
            False,
            "aprovação com hash divergente — plano alterado após aprovação; gere nova aprovação",
        )
    return True, f"aprovação válida para {len(indices)} item(ns) do plano '{plan_id}'"


def filter_suggestions(rows: Sequence[SuggestionRow], query: str | None) -> list[SuggestionRow]:
    """Filtra linhas por substring case-insensitive (puro, sem mutação).

    Campos pesquisados: `file_path`, `destination`, `reason`.
    Query vazia/None retorna cópia integral.
    """
    if query is None or not query.strip():
        return list(rows)
    q = query.strip().lower()
    out: list[SuggestionRow] = []
    for r in rows:
        dest = (r.destination or "").lower()
        if q in r.file_path.lower() or q in dest or q in r.reason.lower():
            out.append(r)
    return out


# Garantia estática de pureza: este módulo nunca deve importar gi/Gtk.
assert "gi" not in globals(), "views/models.py não deve importar gi"
