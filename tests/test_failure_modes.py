"""Simulações de falha ponta a ponta — Sprint 07 (QA + pacote Fedora).

TODOS os testes usam `tmp_path` + `:memory:` + fixtures sintéticas.
Nenhum toca em `~` real, nenhum `sudo`, nenhum serviço, nenhuma rede.
Falhas físicas (ENOSPC, EACCES, crash, concorrência) são SIMULADAS via
monkeypatch/injeção — nenhum disco real é enchido, nenhuma permissão
real é alterada fora de `tmp_path`.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import threading
from pathlib import Path

from hexavante.models.entities import (
    ClassificationResult,
    MovePlan,
    compute_plan_hash,
)
from hexavante.services.interfaces import Approval
from hexavante.services.move_service import JournaledMoveService, recover_pending
from hexavante.storage.db import connect, migrate


def _memdb() -> sqlite3.Connection:
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _item(src: str, dst: str) -> ClassificationResult:
    return ClassificationResult(
        file_path=src, rule_id="r1", destination=dst,
        confidence=0.9, reason="falha simulada sprint07",
    )


def _plan(plan_id: str, pairs: list[tuple[str, str]]) -> MovePlan:
    items = tuple(_item(s, d) for s, d in pairs)
    return MovePlan(
        plan_id=plan_id, items=items,
        plan_hash=compute_plan_hash(plan_id, items), dry_run=False,
    )


def _approval(plan: MovePlan) -> Approval:
    return Approval(plan_id=plan.plan_id, plan_hash=plan.plan_hash, approver="qa07")


def _state(conn: sqlite3.Connection, op_id: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM operations_log WHERE operation_id = ?;", (op_id,)
    ).fetchone()
    return row["state"] if row else None


def test_enospc_simulado_preserva_origem_e_journal_falhou(
    tmp_path: Path, monkeypatch
) -> None:
    """Disco cheio durante a cópia: origem intacta, journal `falhou`, sem tmp vazado."""
    conn = _memdb()
    try:
        src_root, dst_root = tmp_path / "o", tmp_path / "d"
        src_root.mkdir(parents=True)
        dst_root.mkdir(parents=True)
        src = src_root / "big.bin"
        payload = b"x" * 200_000
        src.write_bytes(payload)
        dst = dst_root / "big.bin"
        plan = _plan("enospc", [(str(src), str(dst))])
        svc = JournaledMoveService(conn, authorized_roots=[tmp_path])
        # Força o ramo cross-fs (tmp O_EXCL + streaming), onde ENOSPC é realista.
        monkeypatch.setattr(svc, "_same_filesystem", lambda *a: False)

        real_open = os.open

        def boom_open(path, flags, *args, **kwargs):
            if flags & os.O_CREAT and flags & os.O_EXCL:
                raise OSError(errno.ENOSPC, "No space left (simulado)")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", boom_open)
        op_id: str | None = None
        try:
            svc.execute(plan, _approval(plan))
            raise AssertionError("deveria falhar com ENOSPC")
        except OSError as exc:
            assert exc.errno == errno.ENOSPC
            row = conn.execute(
                "SELECT operation_id FROM operations_log WHERE state='falhou';"
            ).fetchone()
            assert row is not None
            op_id = row["operation_id"]
        assert _state(conn, op_id) == "falhou"  # type: ignore[arg-type]
        assert src.read_bytes() == payload  # origem intacta
        assert not dst.exists()  # nada publicado
        # Nenhum temporário vazado no destino.
        leftovers = [p for p in dst_root.iterdir() if p.name.startswith(".hexavante-tmp-")]
        assert leftovers == []
    finally:
        conn.close()


def test_eacces_no_destino_falha_sem_perder_origem(
    tmp_path: Path, monkeypatch
) -> None:
    """Sem permissão de escrita no destino: falha auditada, origem intacta."""
    conn = _memdb()
    try:
        src_root, dst_root = tmp_path / "o", tmp_path / "d"
        src_root.mkdir(parents=True)
        dst_root.mkdir(parents=True)
        src = src_root / "a.txt"
        src.write_bytes(b"conteudo")
        dst = dst_root / "a.txt"
        plan = _plan("eacces", [(str(src), str(dst))])
        svc = JournaledMoveService(conn, authorized_roots=[tmp_path])

        real_link = os.link

        def boom_link(a, b, *args, **kwargs):
            raise PermissionError(errno.EACCES, "Permission denied (simulado)")

        monkeypatch.setattr(os, "link", boom_link)
        try:
            svc.execute(plan, _approval(plan))
            raise AssertionError("deveria falhar com EACCES")
        except OSError:
            row = conn.execute(
                "SELECT operation_id FROM operations_log WHERE state='falhou';"
            ).fetchone()
            assert row is not None
        assert src.read_bytes() == b"conteudo"
        assert not dst.exists()
    finally:
        conn.close()


def test_execucoes_concorrentes_mesmo_plano_sem_overwrite(tmp_path: Path) -> None:
    """Corrida pelo mesmo destino: no máximo uma vence; sem overwrite.

    Cada thread usa sua própria conexão `:memory:` (sqlite3 não cruza
    threads); o árbitro é o FS compartilhado via `link` atômico (EEXIST).
    Trava cross-process/instância documentada como limitação (Sprint 06).
    """
    dst_root = tmp_path / "d"
    src_root = tmp_path / "o"
    src_root.mkdir(parents=True)
    dst_root.mkdir(parents=True)
    # Dois arquivos-fonte distintos para o MESMO destino (corrida real).
    s1 = src_root / "um.txt"
    s2 = src_root / "dois.txt"
    s1.write_bytes(b"UM")
    s2.write_bytes(b"DOIS")
    dst = dst_root / "alvo.txt"
    results: dict[str, str] = {}

    def run(tag: str, src: Path) -> None:
        conn = _memdb()
        try:
            svc = JournaledMoveService(conn, authorized_roots=[tmp_path])
            plan = _plan(f"race-{tag}", [(str(src), str(dst))])
            try:
                op = svc.execute(plan, _approval(plan))
                results[tag] = op.state.value
            except Exception as exc:  # noqa: BLE001 — corrida esperada
                results[tag] = f"erro:{type(exc).__name__}"
        finally:
            conn.close()

    t1 = threading.Thread(target=run, args=("t1", s1))
    t2 = threading.Thread(target=run, args=("t2", s2))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert sorted(results) == ["t1", "t2"]
    assert "concluida" in set(results.values())
    # O perdedor falhou sem sobrescrever o vencedor.
    assert dst.read_bytes() in (b"UM", b"DOIS")
    # No máximo uma vencedora: a outra origem segue intacta.
    intactas = [p for p in (s1, s2) if p.exists()]
    assert len(intactas) == 1


def test_approval_antiga_rejeitada_apos_restart_com_db_em_disco(
    tmp_path: Path,
) -> None:
    """Journal sobrevive ao restart (DB em arquivo tmp); approval antiga não é reutilizável."""
    db = tmp_path / "restart.db"
    conn = connect(db)
    migrate(conn)
    try:
        src_root, dst_root = tmp_path / "o", tmp_path / "d"
        src_root.mkdir(parents=True)
        dst_root.mkdir(parents=True)
        src = src_root / "a.txt"
        src.write_bytes(b"v1")
        dst = dst_root / "a.txt"
        plan = _plan("restart", [(str(src), str(dst))])
        old_approval = _approval(plan)
        svc = JournaledMoveService(conn, authorized_roots=[tmp_path])
        op = svc.execute(plan, old_approval)
        assert op.state.value == "concluida"
    finally:
        conn.close()
    # "Restart": nova conexão sobre o mesmo arquivo.
    conn2 = connect(db)
    try:
        assert recover_pending(conn2) == []  # nada pendente
        # Plano mutado (outro destino) com approval ANTIGA → recusa.
        dst2 = tmp_path / "d" / "b.txt"
        items = (_item(str(tmp_path / "o" / "a.txt"), str(dst2)),)
        from dataclasses import replace

        mutated = replace(
            _plan("restart", [(str(tmp_path / "o" / "a.txt"), str(dst2))]),
            plan_hash="0" * 64,
        )
        assert mutated.plan_hash != old_approval.plan_hash
        svc2 = JournaledMoveService(conn2, authorized_roots=[tmp_path])
        try:
            svc2.execute(mutated, old_approval)
            raise AssertionError("approval antiga deveria ser recusada")
        except PermissionError:
            pass
        # Journal original intacto após restart.
        row = conn2.execute(
            "SELECT state FROM operations_log WHERE plan_id='restart' AND state='concluida';"
        ).fetchone()
        assert row is not None
    finally:
        conn2.close()


def test_crash_recovery_permite_reexecucao_limpa(tmp_path: Path) -> None:
    """Crash simulado após o link: sem perda (par hardlink) + reexecução recusada sem overwrite.

    O `link` já havia publicado o destino antes do crash; a guarda de
    colisão impede a reexecução cega de sobrescrever — o operador decide
    via journal (concluir removendo a origem ou desfazer). Nenhum dado some.
    """
    conn = _memdb()
    try:
        src_root, dst_root = tmp_path / "o", tmp_path / "d"
        src_root.mkdir(parents=True)
        dst_root.mkdir(parents=True)
        src = src_root / "a.txt"
        src.write_bytes(b"dados")
        dst = dst_root / "a.txt"
        plan = _plan("crash-retry", [(str(src), str(dst))])
        svc = JournaledMoveService(conn, authorized_roots=[tmp_path])
        try:
            svc.execute(plan, _approval(plan), _crash_after="link")
            raise AssertionError("crash simulado deveria interromper")
        except RuntimeError:
            pass
        pending = recover_pending(conn)
        assert len(pending) == 1
        assert pending[0].state.value == "recuperacao_pendente"
        # Sem perda: origem intacta E destino publicado (par hardlink).
        assert src.read_bytes() == b"dados"
        assert dst.read_bytes() == b"dados"
        # Reexecução cega é RECUSADA (colisão) em vez de sobrescrever.
        try:
            svc.execute(plan, _approval(plan))
            raise AssertionError("reexecução deveria ser recusada por colisão")
        except FileExistsError:
            pass
        assert src.read_bytes() == b"dados"
        assert dst.read_bytes() == b"dados"
    finally:
        conn.close()


def test_scheduler_dry_run_com_scanner_real_nao_move(tmp_path: Path) -> None:
    """Integração: scheduler + SafeScanner real em tmp → sugere, nunca move."""
    from hexavante.services.scheduler import DryRunScheduler
    from hexavante.services.scanner import SafeScanner
    from hexavante.models.entities import Schedule

    conn = _memdb()
    try:
        root = tmp_path / "dl"
        root.mkdir()
        (root / "a.pdf").write_bytes(b"%PDF-1.4 fake")
        (root / "b.jpg").write_bytes(b"\xff\xd8 fake")
        scanner = SafeScanner(authorized_roots=[root])
        sched = DryRunScheduler(conn, scanner=scanner)
        sch = Schedule(
            schedule_id="integ1", name="integracao", cron_expr="interval:60",
            enabled=True, dry_run=True,
        )
        sched.save_schedule(sch)
        before = sorted(p.name for p in root.iterdir())
        op = sched.run_once(sch, dry_run=True)
        assert op is not None
        assert op.state.value == "verificada"
        after = sorted(p.name for p in root.iterdir())
        assert before == after  # nada movido/criado/apagado
    finally:
        conn.close()


def test_spec_fedora_tem_campos_obrigatorios() -> None:
    """Empacotamento: spec existe e declara Name/Version/Source/Files sem sudo."""
    repo = Path(__file__).resolve().parent.parent
    spec = repo / "packaging" / "hexavante-file-organizer.spec"
    assert spec.is_file(), "packaging/hexavante-file-organizer.spec ausente"
    text = spec.read_text(encoding="utf-8")
    for campo in ("Name:", "Version:", "Release:", "Summary:", "License:",
                  "Source0:", "%description", "%files", "%changelog"):
        assert campo in text, f"spec sem {campo}"
    assert "hexavante-file-organizer" in text
