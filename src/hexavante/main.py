"""Entry-point `python -m hexavante.main` / `hexavante.main:main`.

- `--headless` + `--db :memory:` permite CI sem display (padrão dos testes).
- Delega ao `AppController`; NUNCA escreve em destino (sem MoveService
  real na Sprint 00; apenas `health_check` + `migrate` do SQLite).
- Sem `import gi` neste módulo (isolamento GTK verificado em testes).
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hexavante", description="Hexavante File Organizer (Sprint 00)")
    p.add_argument("--headless", action="store_true", help="executa sem display GTK (CI)")
    p.add_argument(
        "--db",
        default=None,
        help="caminho SQLite ou ':memory:' (testes/CI). Padrão: XDG produção.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.headless:
        # Caminho headless: núcleo puro + SQLite, sem gi, sem writes em destino.
        from hexavante.controllers import AppController
        from hexavante.storage.db import connect, get_default_db_path, migrate

        db_target = args.db if args.db else str(get_default_db_path())
        # NUNCA criar DB real em CI quando :memory: for pedido.
        conn = connect(db_target)
        try:
            version = migrate(conn)
        except Exception:
            conn.close()
            raise
        try:
            controller = AppController(dry_run=True)
            status = controller.health_check()
            print(f"hexavante headless ok (schema v{version}, dry_run={status['dry_run']})")
            return 0
        finally:
            conn.close()

    # Caminho gráfico: único lugar que toca na view GTK.
    from hexavante.views.app import run as run_gui

    return run_gui(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
