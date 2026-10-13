"""Testes do JournaledMoveService — Sprint 03.

TODOS usam `tmp_path` + `:memory:` + fixtures sintéticas. Nenhum toca em `~`
real, nenhum `sudo`, nenhum serviço, nenhuma escrita fora de `tmp_path`.
Operações "reais" (link/unlink/cópia) ocorrem SOMENTE dentro de `tmp_path`.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from hexavante.models.entities import (
    ClassificationResult,
    MovePlan,
    OperationRecord,
    OperationState,
    compute_plan_hash,
)
from hexavante.services.interfaces import Approval, MoveService
from hexavante.services.move_service import JournaledMoveService, recover_pending
from hexavante.storage.db import connect, migrate


# -- helpers ---------------------------------------------------------------

def _memdb() -> sqlite3.Connection:
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _mk_dirs(tmp_path: Path) -> tuple[Path, Path]:
    src_root = tmp_path / "origem"
    dst_root = tmp_path / "destino"
    src_root.mkdir(parents=True, exist_ok=True)
    dst_root.mkdir(parents=True, exist_ok=True)
    return src_root, dst_root


def _svc_for(
    tmp_path: Path,
    conn: sqlite3.Connection,
    *,
    roots: list[Path] | None = None,
    allowed: list[str] | None = None,
    use_defaults: bool = True,
) -> JournaledMoveService:
    if use_defaults:
        return JournaledMoveService(conn, authorized_roots=[tmp_path])
    assert roots is not None
    if allowed is None:
        return JournaledMoveService(conn, authorized_roots=roots)
    return JournaledMoveService(conn, authorized_roots=roots, allowed_destinations=allowed)


def _item(src: str, dst: str) -> ClassificationResult:
    return ClassificationResult(
        file_path=src, rule_id="r1", destination=dst,
        confidence=0.9, reason="teste sprint03",
    )


def _plan(plan_id: str, pairs: list[tuple[str, str]], *, dry_run: bool) -> MovePlan:
    items = tuple(_item(s, d) for s, d in pairs)
    return MovePlan(
        plan_id=plan_id, items=items,
        plan_hash=compute_plan_hash(plan_id, items), dry_run=dry_run,
    )


def _approval(plan: MovePlan) -> Approval:
    return Approval(plan_id=plan.plan_id, plan_hash=plan.plan_hash, approver="tester")


def _db_state(conn: sqlite3.Connection, op_id: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM operations_log WHERE operation_id = ?;", (op_id,)
    ).fetchone()
    return row["state"] if row else None


def _db_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM operations_log;").fetchone()[0]  # type: ignore[no-any-return]


# -- preview: casos válidos --------------------------------------------------

def test_preview_ok_retorna_plano_inalterado(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"ola")
        dst = dst_root / "a.txt"
        plan = _plan("p1", [(str(src), str(dst))], dry_run=False)
        svc = _svc_for(tmp_path, conn)
        out = svc.preview(plan)
        assert out == plan
        assert out.plan_hash == compute_plan_hash(out.plan_id, out.items)
    finally:
        conn.close()


def test_preview_nao_escreve_fs_nem_db(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"conteudo")
        dst = dst_root / "a.txt"
        plan = _plan("p1", [(str(src), str(dst))], dry_run=False)
        svc = _svc_for(tmp_path, conn)
        before_fs = sorted(p.name for p in dst_root.iterdir())
        svc.preview(plan)
        after_fs = sorted(p.name for p in dst_root.iterdir())
        assert before_fs == after_fs == []
        assert not dst.exists()
        assert src.read_bytes() == b"conteudo"
        assert _db_count(conn) == 0
    finally:
        conn.close()


def test_conforma_ao_protocol_move_service(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        svc = _svc_for(tmp_path, conn)
        assert isinstance(svc, MoveService)
    finally:
        conn.close()


# -- preview: recusas --------------------------------------------------------

def test_preview_recusa_traversal_dotdot(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, _ = _mk_dirs(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        evil_src = outside / "segredo.txt"
        evil_src.write_bytes(b"x")
        # Raiz autorizada = só src_root; `..` escapa para outside.
        svc = JournaledMoveService(conn, authorized_roots=[src_root])
        traversal = str(src_root / ".." / "outside" / "segredo.txt")
        dst = str(src_root / "dest.txt")
        # Garante parent do destino existe para isolar a falha na origem.
        plan = _plan("p-trav", [(traversal, dst)], dry_run=False)
        with pytest.raises(PermissionError, match="escopo"):
            svc.preview(plan)
    finally:
        conn.close()


def test_preview_recusa_symlink_origem(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        real = src_root / "real.txt"
        real.write_bytes(b"conteudo")
        link = src_root / "atalho.txt"
        link.symlink_to(real)
        svc = _svc_for(tmp_path, conn)
        plan = _plan("p-link", [(str(link), str(dst_root / "x.txt"))], dry_run=False)
        with pytest.raises(PermissionError, match="symlink"):
            svc.preview(plan)
    finally:
        conn.close()


def test_preview_recusa_destino_existente_colisao(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"novo")
        dst = dst_root / "a.txt"
        dst.write_bytes(b"existente")
        svc = _svc_for(tmp_path, conn)
        plan = _plan("p-col", [(str(src), str(dst))], dry_run=False)
        with pytest.raises(ValueError, match="colis"):
            svc.preview(plan)
        # Nada foi sobrescrito pelo preview.
        assert dst.read_bytes() == b"existente"
        assert src.read_bytes() == b"novo"
    finally:
        conn.close()


def test_preview_recusa_destino_fora_de_allowed(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        fora = tmp_path / "fora"
        fora.mkdir()
        src = src_root / "a.txt"
        src.write_bytes(b"x")
        dst = fora / "x.txt"
        svc = JournaledMoveService(
            conn, authorized_roots=[src_root], allowed_destinations=[str(dst_root)]
        )
        plan = _plan("p-allow", [(str(src), str(dst))], dry_run=False)
        with pytest.raises(PermissionError, match="allowed"):
            svc.preview(plan)
    finally:
        conn.close()


def test_preview_recusa_temporario_part_e_crdownload(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        svc = _svc_for(tmp_path, conn)
        for name in ("video.mp4.part", "arquivo.crdownload"):
            src = src_root / name
            src.write_bytes(b"parcial")
            plan = _plan(f"p-tmp-{name}", [(str(src), str(dst_root / "x.txt"))],
                         dry_run=False)
            with pytest.raises(ValueError, match="tempor"):
                svc.preview(plan)
            src.unlink()
    finally:
        conn.close()


def test_preview_recusa_plano_vazio(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        svc = _svc_for(tmp_path, conn)
        plan = MovePlan(plan_id="vazio", items=(),
                        plan_hash=compute_plan_hash("vazio", ()), dry_run=True)
        with pytest.raises(ValueError, match="vazio"):
            svc.preview(plan)
    finally:
        conn.close()


def test_preview_recusa_parent_inexistente_sem_criar(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, _ = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"x")
        ghost_parent = src_root / "nao-existe"
        dst = ghost_parent / "x.txt"
        svc = JournaledMoveService(conn, authorized_roots=[src_root])
        plan = _plan("p-nodir", [(str(src), str(dst))], dry_run=False)
        with pytest.raises(ValueError, match="inexistente|autorizada"):
            svc.preview(plan)
        # NUNCA cria diretório.
        assert not ghost_parent.exists()
    finally:
        conn.close()


# -- execute: approval -------------------------------------------------------

def test_execute_recusa_hash_errado_e_journal_falhou(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"x")
        plan = _plan("p1", [(str(src), str(dst_root / "a.txt"))], dry_run=False)
        svc = _svc_for(tmp_path, conn)
        bad = Approval(plan_id="p1", plan_hash="hash-antigo-divergente")
        with pytest.raises(PermissionError):
            svc.execute(plan, bad)
        row = conn.execute(
            "SELECT state FROM operations_log WHERE plan_id = ?;", ("p1",)
        ).fetchone()
        assert row is not None and row["state"] == "falhou"
        # Nada movido.
        assert src.exists() and not (dst_root / "a.txt").exists()
    finally:
        conn.close()


def test_execute_recusa_plan_id_errado(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"x")
        plan = _plan("p1", [(str(src), str(dst_root / "a.txt"))], dry_run=False)
        svc = _svc_for(tmp_path, conn)
        bad = Approval(plan_id="outro-plano", plan_hash=plan.plan_hash)
        with pytest.raises(PermissionError):
            svc.execute(plan, bad)
    finally:
        conn.close()


def test_execute_recusa_plano_mutado_pos_approval(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"x")
        src2 = src_root / "b.txt"
        src2.write_bytes(b"y")
        plan = _plan("p1", [(str(src), str(dst_root / "a.txt"))], dry_run=False)
        good = _approval(plan)
        svc = _svc_for(tmp_path, conn)
        # Mutação pós-approval: mesmos plan_id/hash antigo, itens diferentes.
        mutated_items = (_item(str(src2), str(dst_root / "b.txt")),)
        mutated = MovePlan(plan_id="p1", items=mutated_items, plan_hash=plan.plan_hash,
                           dry_run=False)
        with pytest.raises(PermissionError):
            svc.execute(mutated, good)
        # Arquivos intactos.
        assert src.exists() and src2.exists()
    finally:
        conn.close()


def test_execute_dry_run_nao_move_journal_verificada(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"conteudo-dry")
        dst = dst_root / "a.txt"
        plan = _plan("p-dry", [(str(src), str(dst))], dry_run=True)
        svc = _svc_for(tmp_path, conn)
        op = svc.execute(plan, _approval(plan))
        assert op.state is OperationState.VERIFICADA
        assert src.exists() and not dst.exists()
        assert src.read_bytes() == b"conteudo-dry"
        assert _db_state(conn, op.operation_id) == "verificada"
        items = conn.execute(
            "SELECT source_path, dest_path FROM move_items WHERE operation_id = ?;",
            (op.operation_id,),
        ).fetchall()
        assert len(items) == 1
        assert items[0]["source_path"] == str(src)
        assert items[0]["dest_path"] == str(dst)
    finally:
        conn.close()


# -- execute real ------------------------------------------------------------

def test_execute_real_mesmo_fs_move_com_journal_concluida(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "doc.txt"
        src.write_bytes(b"conteudo-123")
        dst = dst_root / "doc.txt"
        plan = _plan("p-real", [(str(src), str(dst))], dry_run=False)
        svc = _svc_for(tmp_path, conn)
        op = svc.execute(plan, _approval(plan))
        assert op.state is OperationState.CONCLUIDA
        assert not src.exists()
        assert dst.read_bytes() == b"conteudo-123"
        assert _db_state(conn, op.operation_id) == "concluida"
        assert len(op.operation_id) == 32  # uuid4 hex
    finally:
        conn.close()


def test_execute_cross_fs_simulado_via_monkeypatch_st_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "foto.bin"
        payload = b"B" * 200_000  # > 64KB, exercita streaming multi-chunk
        src.write_bytes(payload)
        dst = dst_root / "foto.bin"
        plan = _plan("p-x", [(str(src), str(dst))], dry_run=False)
        svc = _svc_for(tmp_path, conn)

        real_stat = os.stat
        abs_parent = os.path.abspath(os.fspath(dst_root))

        def fake_stat(p, *a, **k):  # type: ignore[no-untyped-def]
            st = real_stat(p, *a, **k)
            if os.path.abspath(os.fspath(p)) == abs_parent:
                class _Fake:
                    pass
                f = _Fake()
                f.st_dev = st.st_dev + 0xABCD  # st_dev forçado diferente
                f.st_mode = st.st_mode
                f.st_size = st.st_size
                return f  # type: ignore[return-value]
            return st

        monkeypatch.setattr(os, "stat", fake_stat)
        op = svc.execute(plan, _approval(plan))
        assert op.state is OperationState.CONCLUIDA
        assert not src.exists()
        assert dst.read_bytes() == payload
        # Temporário exclusivo limpo (nenhum .hexavante-tmp-* restante).
        leftovers = [p for p in dst_root.iterdir() if p.name.startswith(".hexavante-tmp-")]
        assert leftovers == []
    finally:
        conn.close()


def test_execute_colisao_entre_preview_e_execute_sem_overwrite(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"NOVO")
        dst = dst_root / "a.txt"
        plan = _plan("p-race", [(str(src), str(dst))], dry_run=False)
        svc = _svc_for(tmp_path, conn)
        # Preview passa (destino ainda livre).
        svc.preview(plan)
        # Corrida: destino aparece entre preview e execute.
        dst.write_bytes(b"ORIGINAL-INTACTO")
        with pytest.raises(FileExistsError):
            svc.execute(plan, _approval(plan))
        # Sem overwrite: destino intacto, origem preservada.
        assert dst.read_bytes() == b"ORIGINAL-INTACTO"
        assert src.read_bytes() == b"NOVO"
        row = conn.execute(
            "SELECT state FROM operations_log WHERE plan_id = ?;", ("p-race",)
        ).fetchone()
        assert row is not None and row["state"] == "falhou"
    finally:
        conn.close()


def test_execute_nunca_chama_overwrite_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"seguro")
        dst = dst_root / "a.txt"
        plan = _plan("p-safe", [(str(src), str(dst))], dry_run=False)
        svc = _svc_for(tmp_path, conn)

        def _forbidden(*a, **k):  # type: ignore[no-untyped-def]
            raise AssertionError("primitiva com overwrite chamada!")

        monkeypatch.setattr(os, "replace", _forbidden)
        monkeypatch.setattr(os, "rename", _forbidden)
        monkeypatch.setattr(shutil, "move", _forbidden)
        op = svc.execute(plan, _approval(plan))
        assert op.state is OperationState.CONCLUIDA
        assert dst.read_bytes() == b"seguro"
    finally:
        conn.close()


def test_execute_crash_simulado_e_recover_pending(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"crash-me")
        dst = dst_root / "a.txt"
        plan = _plan("p-crash", [(str(src), str(dst))], dry_run=False)
        svc = _svc_for(tmp_path, conn)
        with pytest.raises(RuntimeError, match="crash simulado"):
            svc.execute(plan, _approval(plan), _crash_after="link")
        # Estado permanece em_andamento (NÃO falhou) para recovery.
        row = conn.execute(
            "SELECT operation_id, state FROM operations_log WHERE plan_id = ?;",
            ("p-crash",),
        ).fetchone()
        assert row is not None and row["state"] == "em_andamento"
        # Crash após link: ambos os nomes existem (hardlink) antes do unlink.
        assert src.exists() and dst.exists()
        recovered = recover_pending(conn)
        assert len(recovered) == 1
        assert recovered[0].state is OperationState.RECUPERACAO_PENDENTE
        assert _db_state(conn, row["operation_id"]) == "recuperacao_pendente"
    finally:
        conn.close()


def test_recover_pending_ignora_concluidas_e_falhadas(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"x")
        dst = dst_root / "a.txt"
        plan_ok = _plan("p-ok", [(str(src), str(dst))], dry_run=False)
        svc = _svc_for(tmp_path, conn)
        op_ok = svc.execute(plan_ok, _approval(plan_ok))
        assert op_ok.state is OperationState.CONCLUIDA
        # Nenhum pendente: recover retorna vazio e não toca na concluída.
        assert recover_pending(conn) == []
        assert _db_state(conn, op_ok.operation_id) == "concluida"
    finally:
        conn.close()


# -- undo --------------------------------------------------------------------

def test_undo_ok_reverte_criando_nova_operacao(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "doc.txt"
        src.write_bytes(b"original")
        dst = dst_root / "doc.txt"
        plan = _plan("p-undo", [(str(src), str(dst))], dry_run=False)
        svc = _svc_for(tmp_path, conn)
        op = svc.execute(plan, _approval(plan))
        assert op.state is OperationState.CONCLUIDA
        undone = svc.undo(op)
        assert undone.state is OperationState.CONCLUIDA
        assert undone.plan_id == "p-undo-undo"
        assert undone.operation_id != op.operation_id
        assert undone.message == "undo ok"
        # FS revertido: origem de volta, destino sumiu.
        assert src.read_bytes() == b"original"
        assert not dst.exists()
        # Original preservada como concluída (auditoria).
        assert _db_state(conn, op.operation_id) == "concluida"
        assert _db_state(conn, undone.operation_id) == "concluida"
    finally:
        conn.close()


def test_undo_recusa_se_origem_recriada_conflito(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"original")
        dst = dst_root / "a.txt"
        plan = _plan("p-conf", [(str(src), str(dst))], dry_run=False)
        svc = _svc_for(tmp_path, conn)
        op = svc.execute(plan, _approval(plan))
        # Conflito: origem recriada antes do undo.
        src.write_bytes(b"RECRIADA")
        with pytest.raises(FileExistsError, match="recriada|conflita"):
            svc.undo(op)
        # Sem overwrite: ambos preservados.
        assert src.read_bytes() == b"RECRIADA"
        assert dst.read_bytes() == b"original"
    finally:
        conn.close()


def test_undo_recusa_se_nao_concluida(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        src_root, dst_root = _mk_dirs(tmp_path)
        src = src_root / "a.txt"
        src.write_bytes(b"x")
        dst = dst_root / "a.txt"
        plan = _plan("p-nc", [(str(src), str(dst))], dry_run=True)
        svc = _svc_for(tmp_path, conn)
        op_dry = svc.execute(plan, _approval(plan))
        assert op_dry.state is OperationState.VERIFICADA
        with pytest.raises(ValueError, match="só desfaz.*concluída"):
            svc.undo(op_dry)
    finally:
        conn.close()


def test_undo_recusa_operacao_desconhecida(tmp_path: Path) -> None:
    conn = _memdb()
    try:
        svc = _svc_for(tmp_path, conn)
        fantasma = OperationRecord(
            operation_id="op-fantasma-123", plan_id="p-x",
            plan_hash="h" * 64, state=OperationState.CONCLUIDA,
        )
        with pytest.raises(ValueError, match="desconhecida"):
            svc.undo(fantasma)
    finally:
        conn.close()
