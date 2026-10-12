"""Lógica de views (Sprint 04) — testes headless, sem display, sem I/O real.

- Fixtures 100% sintéticas (`/tmp/synth/...`); nenhum arquivo pessoal.
- Nenhum teste abre janela GTK nem importa `gi`.
- Cobre: formatação de plano, validação (sem seleção/sem approval/hash
  divergente), filtro, seleção≠autorização, estados vazios/erro,
  controller history com `:memory:`, dashboard headless.
"""

from __future__ import annotations

import sqlite3

import pytest

from hexavante.controllers import AppController
from hexavante.models.entities import (
    ClassificationResult,
    FileRecord,
    MovePlan,
    OperationRecord,
    OperationState,
    compute_plan_hash,
)
from hexavante.services.interfaces import Approval
from hexavante.storage.db import connect, migrate
from hexavante.views import dashboard as dashboard_mod
from hexavante.views.dashboard import Dashboard
from hexavante.views.models import (
    EMPTY_HISTORY,
    EMPTY_SUGGESTIONS,
    ErrorState,
    HistoryRow,
    PlanSummary,
    SuggestionRow,
    display_state,
    filter_suggestions,
    format_plan_summary,
    validate_approval_request,
)


def _items_two() -> tuple[ClassificationResult, ...]:
    return (
        ClassificationResult(
            file_path="/tmp/synth/a.pdf",
            rule_id="r-pdf",
            destination="/tmp/synth/docs",
            confidence=0.9,
            reason="ext .pdf → docs",
        ),
        ClassificationResult(
            file_path="/tmp/synth/b.jpg",
            rule_id="r-img",
            destination="/tmp/synth/imgs",
            confidence=0.9,
            reason="ext .jpg → imgs",
        ),
    )


def _plan_two(plan_id: str = "plan-1") -> MovePlan:
    items = _items_two()
    return MovePlan(plan_id=plan_id, items=items, plan_hash=compute_plan_hash(plan_id, items))


def _approval_for(plan: MovePlan, approver: str = "humano") -> Approval:
    return Approval(plan_id=plan.plan_id, plan_hash=plan.plan_hash, approver=approver)


# -- formatação de plano -------------------------------------------------


def test_format_plan_summary_counts_and_hash() -> None:
    plan = _plan_two()
    summary = format_plan_summary(plan, [True, True])
    assert isinstance(summary, PlanSummary)
    assert summary.plan_id == "plan-1"
    assert summary.plan_hash == plan.plan_hash
    assert summary.n_items == 2
    assert summary.n_selected == 2
    assert summary.riscos == ()


def test_format_plan_summary_partial_selection() -> None:
    plan = _plan_two()
    summary = format_plan_summary(plan, [True, False])
    assert summary.n_items == 2
    assert summary.n_selected == 1


def test_format_plan_summary_indices_style() -> None:
    plan = _plan_two()
    summary = format_plan_summary(plan, [0])
    assert summary.n_selected == 1
    summary2 = format_plan_summary(plan, {0: True, 1: False})
    assert summary2.n_selected == 1


def test_format_plan_summary_bloqueio_sem_destino() -> None:
    items = (
        ClassificationResult(
            file_path="/tmp/synth/x.bin",
            rule_id=None,
            destination=None,
            confidence=0.0,
            reason="desconhecido — requer revisão",
        ),
    )
    plan = MovePlan(plan_id="p-bloq", items=items, plan_hash=compute_plan_hash("p-bloq", items))
    summary = format_plan_summary(plan, [True])
    assert summary.n_selected == 1
    assert any("bloqueio" in r for r in summary.riscos)


def test_format_plan_summary_risco_baixa_confianca() -> None:
    items = (
        ClassificationResult(
            file_path="/tmp/synth/y.pdf",
            rule_id="r1",
            destination="/tmp/synth/docs",
            confidence=0.2,
            reason="fallback categoria",
        ),
    )
    plan = MovePlan(plan_id="p-low", items=items, plan_hash=compute_plan_hash("p-low", items))
    summary = format_plan_summary(plan, [True])
    assert any("risco" in r for r in summary.riscos)


def test_format_plan_empty_selection_reports_block() -> None:
    plan = _plan_two()
    summary = format_plan_summary(plan, [False, False])
    assert summary.n_selected == 0
    assert any("nenhum item" in r for r in summary.riscos)


# -- validação de aprovação ----------------------------------------------


def test_validate_recusa_sem_selecao() -> None:
    plan = _plan_two()
    ok, motivo = validate_approval_request(plan, [], _approval_for(plan))
    assert ok is False
    assert "nenhum item" in motivo.lower()


def test_validate_recusa_sem_approval() -> None:
    plan = _plan_two()
    ok, motivo = validate_approval_request(plan, [True, False], None)
    assert ok is False
    assert "aprovação" in motivo.lower()
    assert "seleção sozinha" in motivo.lower()


def test_validate_recusa_hash_divergente() -> None:
    plan = _plan_two()
    bad = Approval(plan_id=plan.plan_id, plan_hash="hash-antigo-divergente")
    ok, motivo = validate_approval_request(plan, [True, True], bad)
    assert ok is False
    assert "divergente" in motivo.lower()


def test_validate_recusa_plan_id_divergente() -> None:
    plan = _plan_two()
    bad = Approval(plan_id="outro-plano", plan_hash=plan.plan_hash)
    ok, _ = validate_approval_request(plan, [True], bad)
    assert ok is False


def test_validate_ok_aprovacao_valida() -> None:
    plan = _plan_two()
    ok, motivo = validate_approval_request(plan, [True, True], _approval_for(plan))
    assert ok is True
    assert "válida" in motivo.lower()


def test_validate_recusa_bloqueio_sem_destino_mesmo_com_approval() -> None:
    items = (
        ClassificationResult(
            file_path="/tmp/synth/z.bin",
            rule_id=None,
            destination=None,
            confidence=0.0,
            reason="revisão",
        ),
    )
    plan = MovePlan(plan_id="p-b", items=items, plan_hash=compute_plan_hash("p-b", items))
    ok, motivo = validate_approval_request(plan, [True], _approval_for(plan))
    assert ok is False
    assert "bloqueado" in motivo.lower()


# -- filtro ---------------------------------------------------------------


def test_filter_case_insensitive() -> None:
    rows = [
        SuggestionRow("/tmp/synth/Relatorio.PDF", "/tmp/synth/docs", 0.9, "pdf → docs", False),
        SuggestionRow("/tmp/synth/foto.jpg", "/tmp/synth/imgs", 0.9, "img", False),
    ]
    assert len(filter_suggestions(rows, "relatorio")) == 1
    assert len(filter_suggestions(rows, "DOCS")) == 1
    assert len(filter_suggestions(rows, "img")) == 1


def test_filter_empty_query_returns_copy_without_mutation() -> None:
    rows = [SuggestionRow("/tmp/synth/a.pdf", "/tmp/synth/d", 0.9, "r", False)]
    out = filter_suggestions(rows, "   ")
    assert out == rows
    assert out is not rows  # cópia, sem mutação
    out_none = filter_suggestions(rows, None)
    assert out_none == rows


def test_filter_no_match_returns_empty() -> None:
    rows = [SuggestionRow("/tmp/synth/a.pdf", "/tmp/synth/d", 0.9, "r", False)]
    assert filter_suggestions(rows, "zzz-nao-existe") == []


# -- seleção não autoriza sozinha ------------------------------------------


class _FakeMoveService:
    """Fake que só executa com approval vinculada (nunca escreve)."""

    def __init__(self) -> None:
        self.calls = 0

    def preview(self, plan: MovePlan) -> MovePlan:
        return plan

    def execute(self, plan: MovePlan, approval: Approval) -> OperationRecord:
        self.calls += 1
        if approval.plan_id != plan.plan_id or approval.plan_hash != plan.plan_hash:
            raise PermissionError("approval inválida ou desvinculada do plano")
        return OperationRecord(
            operation_id="op-1",
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            state=OperationState.APROVADA,
        )


def test_selecao_sozinha_nao_executa_sem_request_approval() -> None:
    plan = _plan_two()
    fake = _FakeMoveService()
    ctl = AppController(dry_run=True, move_service=fake)
    # Tentativa de executar sem approval válida (hash divergente) recusa.
    bad = Approval(plan_id=plan.plan_id, plan_hash="divergente")
    with pytest.raises(PermissionError):
        ctl.execute(plan, bad)
    assert fake.calls == 1
    # Seleção (lista de bools) por si só nunca chamou execute de novo.
    _ = [True, False]  # marcar checkbox não tem efeito colateral
    assert fake.calls == 1
    # Só approval explícita via request_approval + execute passa.
    approval = ctl.request_approval(plan, [True, False], "humano")
    op = ctl.execute(plan, approval)
    assert op.state is OperationState.APROVADA


def test_dashboard_approve_selected_exige_selecao_nao_vazia() -> None:
    ctl = AppController(dry_run=True)
    dash = Dashboard(ctl, _plan_two())
    with pytest.raises(ValueError):
        dash.approve_selected([], "humano")
    with pytest.raises(ValueError):
        dash.approve_selected([False, False], "humano")


def test_dashboard_approve_selected_retorna_approval_vinculada_sem_executar() -> None:
    fake = _FakeMoveService()
    ctl = AppController(dry_run=True, move_service=fake)
    dash = Dashboard(ctl, _plan_two())
    approval = dash.approve_selected([True, False], "humano")
    assert approval.plan_hash == dash.plan.plan_hash  # type: ignore[union-attr]
    # approve NÃO executou nada sozinho.
    assert fake.calls == 0


# -- controller: history + list_suggestions + request_approval --------------


def test_controller_history_empty_and_with_rows() -> None:
    conn = connect(":memory:")
    try:
        migrate(conn)
        ctl = AppController()
        assert ctl.history(conn) == []  # estado vazio compreensível
        conn.execute(
            "INSERT INTO operations_log (operation_id, plan_id, plan_hash, state, message)"
            " VALUES (?, ?, ?, ?, ?);",
            ("op-1", "plan-1", "h1", "planejada", "lote planejado"),
        )
        conn.execute(
            "INSERT INTO operations_log (operation_id, plan_id, plan_hash, state, message)"
            " VALUES (?, ?, ?, ?, ?);",
            ("op-2", "plan-1", "h1", "concluida", "ok"),
        )
        conn.commit()
        rows = ctl.history(conn)
        assert len(rows) == 2
        assert isinstance(rows[0], HistoryRow)
        assert rows[0].operation_id == "op-1"
        assert rows[1].state == "concluida"
        assert display_state("concluida") == "concluída"
        assert display_state("em_andamento") == "em andamento"
    finally:
        conn.close()


def test_controller_request_approval_recusa_vazio_e_hash_ausente() -> None:
    ctl = AppController()
    plan = _plan_two()
    with pytest.raises(ValueError):
        ctl.request_approval(plan, [], "humano")
    with pytest.raises(ValueError):
        ctl.request_approval(plan, [False, False], "humano")
    # plan_hash ausente → recusa (plano adulterado).
    # MovePlan exige hash não-vazio, então simula plano corrompido
    # via object.__new__ (sem passar pelo __post_init__ válido).
    corrupted = object.__new__(MovePlan)
    object.__setattr__(corrupted, "plan_id", "plan-1")
    object.__setattr__(corrupted, "items", plan.items)
    object.__setattr__(corrupted, "plan_hash", "")
    object.__setattr__(corrupted, "dry_run", True)
    object.__setattr__(corrupted, "created_at", "")
    with pytest.raises(ValueError):
        ctl.request_approval(corrupted, [True, False], "humano")


def test_controller_request_approval_ok_vinculada() -> None:
    ctl = AppController()
    plan = _plan_two()
    approval = ctl.request_approval(plan, [True, True], "humano")
    assert approval.plan_id == plan.plan_id
    assert approval.plan_hash == plan.plan_hash
    assert "a.pdf" in approval.scope_origin
    ok, _ = validate_approval_request(plan, [True, True], approval)
    assert ok is True


def test_controller_list_suggestions_pairs_and_missing() -> None:
    ctl = AppController()
    files = [
        FileRecord(path="/tmp/synth/a.pdf"),
        FileRecord(path="/tmp/synth/sem-regra.txt"),
    ]
    clss = [
        ClassificationResult(
            file_path="/tmp/synth/a.pdf",
            rule_id="r1",
            destination="/tmp/synth/docs",
            confidence=0.9,
            reason="pdf",
        )
    ]
    rows = ctl.list_suggestions(files, clss)  # type: ignore[return-value]
    assert len(rows) == 2
    assert rows[0].destination == "/tmp/synth/docs"  # type: ignore[attr-defined]
    assert rows[1].destination is None  # type: ignore[attr-defined]
    assert "revisão" in rows[1].reason  # type: ignore[attr-defined]


def test_controller_describe_plan() -> None:
    ctl = AppController()
    plan = _plan_two()
    summary = ctl.describe_plan(plan)  # type: ignore[return-value]
    assert summary.n_items == 2  # type: ignore[attr-defined]
    assert summary.n_selected == 2  # type: ignore[attr-defined]


# -- dashboard headless + estados -------------------------------------------


def test_dashboard_headless_build_ui_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_mod, "is_gtk_available", lambda: False)
    dash = Dashboard(AppController(), _plan_two())
    with pytest.raises(RuntimeError) as exc:
        dash.build_ui()
    assert "GTK" in str(exc.value) or "headless" in str(exc.value).lower()


def test_dashboard_get_rows_sem_display_sem_escrita() -> None:
    dash = Dashboard(AppController(), _plan_two())
    rows = dash.get_suggestion_rows()
    assert len(rows) == 2
    assert all(r.selected is False for r in rows)
    # Navegação/seleção local não escreve: apenas filtra em memória.
    assert len(filter_suggestions(rows, "a.pdf")) == 1
    # Sem plano → estado vazio, sem exceção obscura.
    dash_empty = Dashboard(AppController(), None)
    assert dash_empty.get_suggestion_rows() == []
    assert dash_empty.get_history_rows(None) == []
    with pytest.raises(ValueError):
        dash_empty.describe_current_plan([True])
    with pytest.raises(ValueError):
        dash_empty.approve_selected([True], "humano")


def test_estados_vazios_e_erro_compreensiveis() -> None:
    assert isinstance(EMPTY_SUGGESTIONS, ErrorState)
    assert EMPTY_SUGGESTIONS.hint != ""
    assert isinstance(EMPTY_HISTORY, ErrorState)
    assert EMPTY_HISTORY.hint != ""
    with pytest.raises(ValueError):
        ErrorState(code="", message="x")
    with pytest.raises(ValueError):
        SuggestionRow(file_path="", destination=None, confidence=0.0, reason="r")
    with pytest.raises(ValueError):
        HistoryRow(operation_id="", state="planejada")


def test_views_models_e_dashboard_sem_gi_no_topo() -> None:
    import ast
    import pathlib

    for rel in ("src/hexavante/views/models.py", "src/hexavante/views/dashboard.py"):
        p = pathlib.Path(__file__).resolve().parent.parent / rel
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not (alias.name == "gi" or alias.name.startswith("gi.")), rel
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not (mod == "gi" or mod.startswith("gi.")), rel
