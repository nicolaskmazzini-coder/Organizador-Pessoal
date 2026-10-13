"""Dashboard GTK (Sprint 04) — views consomem controllers, nunca escrevem direto.

Segurança (inegociável):
- NENHUM clique de navegação/seleção dispara escrita. Alternar checkbox,
  trocar de aba ou filtrar apenas atualiza estado local / UI.
- Seleção de lista NUNCA é autorização implícita. `selected=True` é só
  intenção; a execução exige `Approval` explícita vinculada ao `plan_hash`
  (obtida via `controller.request_approval`) e chamada separada a
  `controller.execute` → `MoveService`.
- Nenhuma escrita direta em destino neste módulo; todo write passa por
  `controller.execute(plan, approval)` (que delega ao `MoveService`).

Headless:
- Este módulo NÃO contém `import gi` no topo (isolamento verificado por
  `tests/test_headless.py`). O acesso a Gtk/Adw/Gio é tardio, dentro de
  `build_ui()`, via `hexavante.views.app` (único módulo autorizado a
  importar `gi`) + `importlib`. Sem GTK/display, `build_ui()` levanta
  `RuntimeError` explicativo; toda a lógica (`get_suggestion_rows`,
  `approve_selected`, ...) funciona sem display e é testada em
  `tests/test_views_logic.py`.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Mapping, Sequence

from hexavante.models.entities import MovePlan
from hexavante.services.interfaces import Approval
from hexavante.views.models import (
    HistoryRow,
    PlanSummary,
    SuggestionRow,
    format_plan_summary,
    validate_approval_request,
)

if TYPE_CHECKING:  # apenas tipagem; sem import em runtime (evita ciclo)
    from hexavante.controllers import AppController

__all__ = ["Dashboard", "is_gtk_available"]


def is_gtk_available() -> bool:
    """True se GTK4/Libadwaita importáveis (delega a `views.app`).

    Função-módulo para permitir `monkeypatch` nos testes:
    `monkeypatch.setattr(dashboard_mod, "is_gtk_available", lambda: False)`.
    """
    try:
        from hexavante.views import app as _app

        return bool(_app.is_gtk_available())
    except Exception:
        return False


class Dashboard:
    """Dashboard nativo: Sugestões, Regras, Histórico, Agendamentos.

    Args:
        controller: `AppController` (orquestrador fino; nunca escreve direto).
        plan: `MovePlan` atual (opcional; use `set_plan` para trocar).
    """

    def __init__(self, controller: object, plan: MovePlan | None = None) -> None:
        self._controller = controller
        self._plan = plan

    # -- estado / lógica (sem display, sem escrita) ---------------------

    def set_plan(self, plan: MovePlan | None) -> None:
        """Troca o plano atual. Sem efeitos colaterais, sem escrita."""
        self._plan = plan

    @property
    def plan(self) -> MovePlan | None:
        """Plano atual (somente leitura)."""
        return self._plan

    def get_suggestion_rows(self) -> list[SuggestionRow]:
        """Linhas de sugestão do plano atual (puro, sem display, sem escrita).

        Retorna `[]` quando nenhum plano está carregado (estado vazio).
        Cada linha nasce com `selected=False`; marcar checkbox não autoriza.
        """
        if self._plan is None:
            return []
        rows: list[SuggestionRow] = []
        for item in self._plan.items:
            rows.append(
                SuggestionRow(
                    file_path=item.file_path,
                    destination=item.destination,
                    confidence=item.confidence,
                    reason=item.reason,
                    selected=False,
                )
            )
        return rows

    def get_history_rows(self, conn: object | None = None) -> list[HistoryRow]:
        """Histórico via `controller.history(conn)` (somente leitura).

        Sem `conn`, retorna `[]` (estado vazio) em vez de falhar — a UI
        exibe `EMPTY_HISTORY`. Com `conn`, delega ao controller (SELECT).
        Nunca escreve.
        """
        if conn is None:
            return []
        history_fn = getattr(self._controller, "history", None)
        if history_fn is None:
            return []
        return history_fn(conn)  # type: ignore[no-any-return]

    def describe_current_plan(
        self,
        selections: Sequence[bool] | Sequence[int] | Mapping[int, bool] | None = None,
    ) -> PlanSummary:
        """Resumo do lote (origem/destino/justificativa/riscos + plan_hash).

        Se `selections` for None, descreve o plano integral (todos
        selecionados) para exibição inicial.
        """
        if self._plan is None:
            raise ValueError("nenhum plano carregado — execute uma varredura primeiro")
        if selections is None:
            selections = [True] * len(self._plan.items)
        return format_plan_summary(self._plan, selections)

    def approve_selected(
        self,
        selections: Sequence[bool] | Sequence[int] | Mapping[int, bool] | None,
        approver: str = "",
    ) -> Approval:
        """Cria `Approval` explícita para o lote (botão "Aprovar lote").

        - Exige plano carregado e ao menos um item selecionado.
        - Delega a `controller.request_approval` (que recusa se vazio ou
          sem `plan_hash`) e revalida o vínculo via
          `validate_approval_request`.
        - NÃO executa movimentação; seleção + approval não movem nada
          sozinhas. A execução exige chamada separada a
          `execute_approved(plan, approval)`.

        Raises:
            ValueError: plano ausente ou seleções vazias/inválidas.
            PermissionError/ValueError: vínculo de aprovação inválido.
        """
        if self._plan is None:
            raise ValueError("nenhum plano carregado — nada a aprovar")
        request_fn = getattr(self._controller, "request_approval", None)
        if request_fn is None:
            raise RuntimeError("controller sem request_approval (Sprint 04)")
        approval: Approval = request_fn(self._plan, selections, approver)
        ok, motivo = validate_approval_request(self._plan, selections, approval)
        if not ok:
            raise PermissionError(motivo)
        return approval

    def execute_approved(self, plan: MovePlan | None, approval: Approval) -> object:
        """Executa lote APENAS com `Approval` válida (via controller→MoveService).

        Revalida o vínculo antes de delegar; em divergência, recusa sem
        chamar `execute` (sem escrita). `plan=None` usa o plano atual.
        """
        current = plan if plan is not None else self._plan
        if current is None:
            raise ValueError("nenhum plano para executar")
        if approval is None:
            raise ValueError(
                "aprovação explícita ausente — seleção sozinha não autoriza execução"
            )
        # Revalidação defensiva: deriva seleções integrais? Não — a aprovação
        # vincula o plano inteiro via hash; a granularidade por item foi
        # fixada em request_approval. Aqui basta checar o vínculo do plano.
        if (
            getattr(approval, "plan_id", None) != getattr(current, "plan_id", None)
            or getattr(approval, "plan_hash", None) != getattr(current, "plan_hash", None)
        ):
            raise PermissionError(
                "aprovação desvinculada do plano — gere nova aprovação em 'Aprovar lote'"
            )
        execute_fn = getattr(self._controller, "execute", None)
        if execute_fn is None:
            raise RuntimeError("controller sem execute (Sprint 04)")
        return execute_fn(current, approval)

    # -- UI (só com display; nunca chamada nos testes) -------------------

    def build_ui(self) -> object:
        """Constrói a UI GTK4/Libadwaita (só com display).

        Estrutura abas: Sugestões, Regras, Histórico, Agendamentos
        (placeholder). Sem GTK, levanta `RuntimeError` explicativo.

        Responsividade: varredura/hash NUNCA rodam na thread UI — aqui
        apenas se estrutura o stub async via `Gio.Task` (implementação
        real nas Sprints 01/05; este módulo não implementa monitor).
        """
        if not is_gtk_available():
            from hexavante.views import app as _app

            err = _app.gtk_import_error()
            raise RuntimeError(
                "GTK4/Libadwaita indisponível neste ambiente (headless/CI): "
                f"{err!r}. A lógica do dashboard permanece testável sem display; "
                "abra o app em sessão GNOME/Fedora para a UI."
            )
        # Import tardio (sem `import gi` literal — isolamento testado):
        # obtém Gtk/Adw/Gio reexportados por views.app.
        app_mod = importlib.import_module("hexavante.views.app")
        Gtk = getattr(app_mod, "Gtk")
        Adw = getattr(app_mod, "Adw")
        Gio = getattr(app_mod, "Gio")
        assert Gtk is not None and Adw is not None

        # Janela + abas (nomes acessíveis, mnemônicos com `_`).
        window = Adw.ApplicationWindow()
        window.set_title("Hexavante — Organizador de Arquivos")
        window.set_default_size(900, 600)

        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)
        window.set_content(notebook)

        # --- Aba 1: Sugestões ---------------------------------------
        sug_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sug_box.set_margin_top(12)
        sug_box.set_margin_bottom(12)
        sug_box.set_margin_start(12)
        sug_box.set_margin_end(12)

        search = Gtk.SearchEntry()
        search.set_placeholder_text("Filtrar sugestões (origem, destino, motivo)…")
        search.set_accessible_name("Filtrar sugestões")
        sug_box.append(search)

        empty_label = Gtk.Label(label="Nenhuma sugestão para exibir.")
        empty_label.set_accessible_name("Estado vazio das sugestões")
        sug_box.append(empty_label)

        # Resumo do lote (plan_hash sempre visível p/ conferência).
        plan_hash_label = Gtk.Label(label="plano: — | hash: —")
        plan_hash_label.set_selectable(True)
        plan_hash_label.set_accessible_name("Resumo do plano e hash")
        sug_box.append(plan_hash_label)

        approve_btn = Gtk.Button(label="Aprovar lote")
        approve_btn.set_tooltip_text(
            "Cria aprovação explícita para os itens marcados. "
            "Marcar checkbox sozinho NÃO autoriza movimentação."
        )
        approve_btn.set_accessible_name("Aprovar lote selecionado")
        # Handler explícito: SÓ o clique neste botão cria Approval.
        # Trocar de aba / marcar checkbox / filtrar NUNCA chama execute.
        def _on_approve_clicked(_btn: object) -> None:
            # Placeholder: a janela real coleta checkboxes + chama
            # self.approve_selected(seleções, aprovador) e exibe o resumo.
            # Nenhuma escrita aqui; execução exige passo separado.
            plan_hash_label.set_label("aprovação solicitada — confira origem, destino e hash")

        approve_btn.connect("clicked", _on_approve_clicked)
        sug_box.append(approve_btn)
        notebook.append_page(sug_box, Gtk.Label(label="_Sugestões"))

        # --- Aba 2: Regras ------------------------------------------
        rules_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        rules_label = Gtk.Label(label="Nenhuma regra cadastrada.")
        rules_label.set_accessible_name("Estado vazio das regras")
        rules_box.append(rules_label)
        notebook.append_page(rules_box, Gtk.Label(label="_Regras"))

        # --- Aba 3: Histórico ---------------------------------------
        hist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hist_label = Gtk.Label(label="Nenhuma operação registrada ainda.")
        hist_label.set_accessible_name("Estado vazio do histórico")
        hist_box.append(hist_label)
        notebook.append_page(hist_box, Gtk.Label(label="_Histórico"))

        # --- Aba 4: Agendamentos (placeholder Sprint 06) -------------
        sched_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sched_label = Gtk.Label(
            label="Agendamentos (Sprint 06 — somente dry-run, sem execução real)."
        )
        sched_label.set_accessible_name("Agendamentos em breve")
        sched_box.append(sched_label)
        notebook.append_page(sched_box, Gtk.Label(label="_Agendamentos"))

        # --- Stub async (não implementa monitor; só estrutura) -------
        def _run_background_stub(task_fn: object) -> None:
            """Estrutura p/ trabalho fora da thread UI (Sprints 01/05).

            Quando `Gio` disponível, o chamador real usaria
            `Gio.Task.run_in_thread`. Aqui apenas documenta o padrão;
            nenhum I/O é disparado pela navegação.
            """
            _ = (Gio, task_fn)
            return None

        _ = _run_background_stub
        return window
