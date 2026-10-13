"""UI GTK4/Libadwaita — ÚNICO módulo autorizado a importar `gi`.

Isolamento:
- Todo `import gi` vive neste arquivo, dentro de try/except.
- Em headless/CI (sem PyGObject/display), o import falha de forma
  controlada: `_GTK_AVAILABLE=False` e `run()` levanta `RuntimeError`
  explicativo em vez de crashar a importação do núcleo.
- Nenhum outro módulo do pacote importa `gi` (verificado em testes).
"""

from __future__ import annotations

try:  # pragma: no cover — depende do host ter PyGObject + display
    import gi  # type: ignore[import-not-found]

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, Gtk  # type: ignore[import-not-found]

    _GTK_AVAILABLE = True
    _GTK_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # ImportError, ValueError (versão), etc.
    gi = None  # type: ignore[assignment]
    Gtk = None  # type: ignore[assignment]
    Adw = None  # type: ignore[assignment]
    Gio = None  # type: ignore[assignment]
    _GTK_AVAILABLE = False
    _GTK_IMPORT_ERROR = exc


def is_gtk_available() -> bool:
    """True se GTK4/Libadwaita importáveis neste host."""
    return _GTK_AVAILABLE


def gtk_import_error() -> Exception | None:
    """Retorna o erro de importação do GTK, se houver."""
    return _GTK_IMPORT_ERROR


def run(argv: list[str] | None = None) -> int:
    """Ponto de entrada gráfico (Sprint 04 implementará a UI completa).

    Sprint 00: apenas valida disponibilidade e retorna; nunca executa
    movimentação real.
    """
    if not _GTK_AVAILABLE:
        raise RuntimeError(
            "GTK4/Libadwaita indisponível neste ambiente "
            f"(headless/CI): {_GTK_IMPORT_ERROR!r}. "
            "Use `python -m hexavante.main --headless`."
        )
    # Placeholder Sprint 00: não abre janela ainda.
    # Sprint 04 construirá Adw.Application aqui.
    assert Gtk is not None and Adw is not None
    return 0
