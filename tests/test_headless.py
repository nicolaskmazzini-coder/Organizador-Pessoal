"""Isolamento GTK + main headless (Sprint 00).

- Núcleo (models/storage/services/controllers) deve importar sem carregar `gi`.
- `main --headless --db :memory:` funciona sem display e sem criar DB real.
- `views/app.py` é o ÚNICO lugar com `import gi`, com guard headless.
"""

from __future__ import annotations

import pathlib
import sys


def test_nucleo_nao_carrega_gi() -> None:
    for mod in list(sys.modules):
        if mod == "gi" or mod.startswith("gi."):
            del sys.modules[mod]
    import hexavante.controllers  # noqa: F401
    import hexavante.models.entities  # noqa: F401
    import hexavante.services.interfaces  # noqa: F401
    import hexavante.storage.db  # noqa: F401

    assert "gi" not in sys.modules, "núcleo não deve importar gi"


def test_apenas_app_py_importa_gi() -> None:
    """Invariante de isolamento (Sprints 00–05).

    - `Gtk`/`Adw` (toolkit UI): exclusivo de `views/app.py`.
    - `import gi` no TOPO do módulo: exclusivo de `views/app.py`
      (núcleo importa sem carregar `gi` — cf. test_nucleo_nao_carrega_gi).
    - Exceção Sprint 05: `services/monitor.py` pode importar `Gio`
      (I/O assíncrono, FileMonitor — não é UI) de forma TARDIA,
      dentro de função, com guard headless. `Gtk`/`Adw` seguem
      proibidos fora de `views/app.py` em qualquer nível.
    """
    import ast

    root = pathlib.Path(__file__).resolve().parent.parent / "src"
    offenders: list[str] = []
    for py in sorted(root.rglob("*.py")):
        is_app = py.name == "app.py" and py.parent.name == "views"
        is_monitor = (
            py.parent.name == "services" and py.name == "monitor.py"
        )
        text = py.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(py))
        top_level = set(tree.body)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
                modules = [n for n in names if n == "gi" or n.startswith("gi.")]
                imported = [n.split(".")[-1] for n in names]
            elif isinstance(node, ast.ImportFrom):
                if node.module == "gi" or (node.module or "").startswith("gi."):
                    modules = [node.module or "gi"]
                    imported = [a.name for a in node.names]
                else:
                    continue
            else:
                continue
            if not modules:
                continue
            rel = f"{py.relative_to(root)}:{node.lineno}"
            # Gtk/Adw: só views/app.py, em qualquer nível.
            if any(i in ("Gtk", "Adw") for i in imported):
                if not is_app:
                    offenders.append(f"{rel}: Gtk/Adw fora de views/app.py")
                continue
            # gi/Gio no topo: só views/app.py.
            if node in top_level and not is_app:
                offenders.append(f"{rel}: import gi no topo fora de views/app.py")
                continue
            # gi/Gio tardio (dentro de função): só monitor.py + app.py.
            if node not in top_level and not (is_app or is_monitor):
                offenders.append(f"{rel}: import gi tardio fora de monitor.py/app.py")
    assert offenders == [], f"gi fora do isolamento: {offenders}"


def test_main_headless_memory_ok(capsys) -> None:  # type: ignore[no-untyped-def]
    from hexavante.main import main

    rc = main(["--headless", "--db", ":memory:"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "headless ok" in out
    assert "gi" not in sys.modules


def test_views_app_guard_nao_crash_headless() -> None:
    from hexavante.views import app as gtk_app

    assert isinstance(gtk_app.is_gtk_available(), bool)
    if not gtk_app.is_gtk_available():
        import pytest

        with pytest.raises(RuntimeError):
            gtk_app.run([])
