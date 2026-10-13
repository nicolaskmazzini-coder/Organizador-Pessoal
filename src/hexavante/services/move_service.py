"""Movimentação segura com journal — Sprint 03.

Escopo ESTRITO Sprint 03 (cf. AGENTS.md + docs/SPRINTS.md):
- `preview` valida sem NENHUMA escrita em FS ou DB; nunca cria diretório.
- `execute` exige `Approval` vinculada ao hash; journal em SQLite
  (`operations_log` + `move_items`); sem overwrite; atômico mesmo-fs via
  `os.link + unlink`; cross-fs via cópia p/ temporário exclusivo + verificação
  + publicação sem overwrite + remoção da origem só após integridade e journal.
- `undo` cria NOVA operação `<plan_id>-undo` (decisão documentada aqui);
  recusa se estado != concluída ou se houver conflito.
- `recover_pending` marca `em_andamento`/`verificada`/`aprovada` como
  `recuperacao_pendente` para decisão humana (retomar/abortar).

Decisões (mentalidade @seguranca-qa):
- Undo cria NOVA operação em vez de mutar a original: preserva trilha de
  auditoria (a operação original permanece `concluida`; o undo é um novo
  registro rastreável com `plan_id + '-undo'` e message 'undo ok').
- Mesmo-fs: `os.link(origem, destino)` falha com `FileExistsError` (EEXIST)
  se houver colisão — sem `os.replace`/`os.rename` sobre existente, sem
  `O_TRUNC`, sem `shutil.move`.
- Cross-fs: temporário exclusivo `O_CREAT|O_EXCL|O_WRONLY` no mesmo dir do
  destino, cópia streaming 64KB, `fsync`, checagem de tamanho, publicação via
  `os.link(tmp, destino)` (falha EEXIST em corrida), `fsync` arquivo+dir,
  journal `verificada` ANTES de remover a origem, remoção só após verificação.
- `_crash_after='link'` (teste): simula crash após o `link` sem journalizar
  `falhou`, deixando `em_andamento` para `recover_pending` exercitar.
- `preview` só usa `lstat` (nunca `stat` que segue link) e `realpath` para
  contencao; `execute` revalida via `preview` + checagem de colisão
  imediatamente antes de publicar (TOCTOU mitigado, não eliminado — corrida
  residual documentada como risco).
- Limitação conhecida: plano multi-item não é atômico — falha no item N deixa
  0..N-1 já movidos com estado `falhou`; chamador deve inspecionar journal e
  decidir (undo parcial manual). Sem rollback automático por segurança.

Módulo puro Python: sem `gi`, sem criação de pastas fora do teste, sem sudo.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Sequence

from hexavante.models.entities import (
    ClassificationResult,
    MovePlan,
    OperationRecord,
    OperationState,
    compute_plan_hash,
)
from hexavante.services.interfaces import Approval, MoveService

__all__ = ["JournaledMoveService", "recover_pending"]


_TEMP_SUFFIXES = (".part", ".crdownload", ".tmp")
_TEMP_PREFIXES = ("~", ".~lock.")

_CHUNK_SIZE = 64 * 1024


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _is_temp_marker(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith(_TEMP_SUFFIXES):
        return True
    return name.startswith(_TEMP_PREFIXES)


def _within(child_abs: str, parent_abs: str) -> bool:
    return child_abs == parent_abs or child_abs.startswith(parent_abs + os.sep)


class JournaledMoveService(MoveService):
    """ÚNICA camada autorizada a escrever em destinos (Sprint 03)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        authorized_roots: Sequence[Path],
        allowed_destinations: Sequence[str] | None = None,
    ) -> None:
        if not authorized_roots:
            raise ValueError("authorized_roots não pode ser vazio")
        self._conn = conn
        # Normalização lexical (abspath): sem tocar FS, sem seguir symlinks.
        self._authorized_roots = tuple(
            os.path.abspath(os.fspath(p)) for p in authorized_roots
        )
        if allowed_destinations is None:
            self._allowed: tuple[str, ...] | None = None
        else:
            self._allowed = tuple(
                os.path.abspath(os.fspath(p)) for p in allowed_destinations
            )

    # -- helpers de escopo -------------------------------------------------

    def _lexical_roots(self) -> tuple[str, ...]:
        return self._authorized_roots

    def _allowed_set(self) -> tuple[str, ...]:
        if self._allowed is None:
            return self._authorized_roots
        return self._allowed + self._authorized_roots

    def _check_src_in_scope(self, abs_src: str) -> None:
        lexical_ok = any(_within(abs_src, r) for r in self._lexical_roots())
        if not lexical_ok:
            raise PermissionError(f"origem fora do escopo autorizado: {abs_src!r}")
        real_src = os.path.realpath(abs_src)
        real_ok = any(
            real_src == os.path.realpath(r)
            or real_src.startswith(os.path.realpath(r) + os.sep)
            for r in self._lexical_roots()
        )
        if not real_ok:
            raise PermissionError(
                f"origem escapa do escopo autorizado via symlink: {abs_src!r}"
            )

    def _check_parent_allowed(self, abs_parent: str) -> None:
        allowed = self._allowed_set()
        lexical_ok = any(_within(abs_parent, a) for a in allowed)
        if not lexical_ok:
            raise PermissionError(
                f"destino fora de allowed_destinations/autorizadas: {abs_parent!r}"
            )
        # Escape via symlink no parent: o realpath também precisa estar contido
        # no realpath de algum destino permitido/autorizado.
        try:
            real_parent = os.path.realpath(abs_parent)
        except OSError as exc:
            raise PermissionError(f"parent de destino ilegível: {exc}") from exc
        real_ok = any(
            real_parent == os.path.realpath(a)
            or real_parent.startswith(os.path.realpath(a) + os.sep)
            for a in allowed
        )
        if not real_ok:
            raise PermissionError(
                f"parent de destino escapa via symlink: {abs_parent!r}"
            )

    # -- journal -----------------------------------------------------------

    def _journal(
        self, operation: OperationRecord, items: Sequence[ClassificationResult]
    ) -> OperationRecord:
        """Upsert `operations_log` + substitui `move_items` em transação."""
        now = _now_iso()
        created_at = operation.created_at or now
        with self._conn:
            self._conn.execute(
                "INSERT INTO operations_log"
                " (operation_id, plan_id, plan_hash, state,"
                " approval_token, message, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(operation_id) DO UPDATE SET"
                " plan_id=excluded.plan_id,"
                " plan_hash=excluded.plan_hash,"
                " state=excluded.state,"
                " approval_token=excluded.approval_token,"
                " message=excluded.message,"
                " updated_at=excluded.updated_at;",
                (
                    operation.operation_id,
                    operation.plan_id,
                    operation.plan_hash,
                    operation.state.value,
                    operation.approval_token,
                    operation.message,
                    created_at,
                    now,
                ),
            )
            self._conn.execute(
                "DELETE FROM move_items WHERE operation_id = ?;",
                (operation.operation_id,),
            )
            for item in items:
                src = item.file_path
                dst = item.destination or ""
                self._conn.execute(
                    "INSERT INTO move_items (operation_id, source_path, dest_path)"
                    " VALUES (?, ?, ?);",
                    (operation.operation_id, src, dst),
                )
        import dataclasses

        return dataclasses.replace(operation, updated_at=now, created_at=created_at)

    def _journal_state(
        self, operation: OperationRecord, state: OperationState, message: str,
        items: Sequence[ClassificationResult],
    ) -> OperationRecord:
        import dataclasses

        updated = dataclasses.replace(operation, state=state, message=message)
        return self._journal(updated, items)

    # -- preview -----------------------------------------------------------

    def preview(self, plan: MovePlan) -> MovePlan:
        """Valida o plano SEM nenhuma escrita em FS ou DB."""
        if not plan.items:
            raise ValueError("plano vazio: nenhum item para validar")
        for item in plan.items:
            src_raw = item.file_path
            dst_raw = item.destination
            if not src_raw:
                raise ValueError("item com file_path vazio")
            if not dst_raw:
                raise ValueError(f"item sem destination (origem={src_raw!r})")
            abs_src = os.path.abspath(src_raw)
            abs_dst = os.path.abspath(dst_raw)
            # Escopo da origem (traversal `..` barrado aqui).
            self._check_src_in_scope(abs_src)
            # Marcadores temporários recusados pelo basename da origem.
            if _is_temp_marker(os.path.basename(abs_src)):
                raise ValueError(f"origem é download parcial/temporário: {src_raw!r}")
            if _is_temp_marker(os.path.basename(abs_dst)):
                raise ValueError(f"destino com marcador temporário: {dst_raw!r}")
            # Origem: lstat, sem seguir symlink; só regular.
            try:
                st_src = os.lstat(abs_src)
            except FileNotFoundError as exc:
                raise ValueError(f"origem inexistente: {src_raw!r}") from exc
            except PermissionError as exc:
                raise PermissionError(f"origem sem permissão: {src_raw!r}: {exc}") from exc
            except OSError as exc:
                raise ValueError(f"origem ilegível: {src_raw!r}: {exc}") from exc
            if stat.S_ISLNK(st_src.st_mode):
                raise PermissionError(
                    f"origem é symlink; nunca seguir links: {src_raw!r}"
                )
            if not stat.S_ISREG(st_src.st_mode):
                raise ValueError(
                    f"origem não é arquivo regular (especial?): {src_raw!r}"
                )
            # Destino: parent dentro do permitido; parent existe e é dir real.
            abs_parent = os.path.abspath(os.path.dirname(abs_dst))
            self._check_parent_allowed(abs_parent)
            try:
                st_parent = os.lstat(abs_parent)
            except FileNotFoundError as exc:
                raise ValueError(
                    f"diretório de destino inexistente; criação não autorizada: "
                    f"{abs_parent!r}"
                ) from exc
            except OSError as exc:
                raise ValueError(
                    f"parent de destino ilegível: {abs_parent!r}: {exc}"
                ) from exc
            if stat.S_ISLNK(st_parent.st_mode):
                raise PermissionError(
                    f"parent de destino é symlink: {abs_parent!r}"
                )
            if not stat.S_ISDIR(st_parent.st_mode):
                raise ValueError(
                    f"parent de destino não é diretório: {abs_parent!r}"
                )
            # Destino não pode existir (colisão) — lstat, sem seguir.
            try:
                os.lstat(abs_dst)
            except FileNotFoundError:
                pass  # esperado: livre para publicar sem overwrite.
            except OSError as exc:
                raise ValueError(
                    f"destino ilegível: {dst_raw!r}: {exc}"
                ) from exc
            else:
                raise ValueError(f"colisão: destino já existe: {dst_raw!r}")
        return plan

    # -- primitivas sem overwrite ------------------------------------------

    @staticmethod
    def _fsync_dir(dirpath: str) -> None:
        try:
            fd = os.open(dirpath, os.O_DIRECTORY)
        except OSError:
            return
        try:
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)

    def _same_filesystem(self, abs_src: str, abs_parent: str) -> bool:
        st_src = os.lstat(abs_src)
        st_parent = os.stat(abs_parent)
        return st_src.st_dev == st_parent.st_dev

    def _move_same_fs(self, abs_src: str, abs_dst: str) -> None:
        # `os.link` é atômico e falha com EEXIST (FileExistsError) — sem overwrite.
        os.link(abs_src, abs_dst)
        self._fsync_dir(os.path.dirname(abs_dst))
        # Remoção da origem SOMENTE após link ok.
        os.unlink(abs_src)

    def _move_cross_fs(self, abs_src: str, abs_dst: str) -> None:
        parent = os.path.dirname(abs_dst)
        tmp_name = f".hexavante-tmp-{uuid.uuid4().hex}.tmp"
        tmp_path = os.path.join(parent, tmp_name)
        # Revalidação TOCTOU: destino pode ter aparecido após preview.
        try:
            os.lstat(abs_dst)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(f"colisão na publicação: {abs_dst!r}")
        fd_tmp = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            # Streaming 64KB origem -> temporário.
            with open(abs_src, "rb") as fsrc:
                while True:
                    chunk = fsrc.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    # `os.write` pode escrever parcial; loop garante tudo.
                    view = memoryview(chunk)
                    while view:
                        n = os.write(fd_tmp, view)
                        view = view[n:]
            os.fsync(fd_tmp)
        except BaseException:
            try:
                os.close(fd_tmp)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        os.close(fd_tmp)
        try:
            # Verificação de integridade mínima: tamanho igual.
            st_src = os.lstat(abs_src)
            st_tmp = os.lstat(tmp_path)
            if st_tmp.st_size != st_src.st_size:
                raise IOError(
                    f"integridade: tamanho divergente após cópia "
                    f"({st_tmp.st_size} != {st_src.st_size})"
                )
            # Publicação SEM overwrite: `link` falha EEXIST em corrida.
            try:
                os.lstat(abs_dst)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(
                    f"colisão entre preview e publicação: {abs_dst!r}"
                )
            os.link(tmp_path, abs_dst)
            self._fsync_dir(parent)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        # `tmp` e `destino` agora são o mesmo inode; `tmp` é só nome extra.
        # A remoção da origem acontece no chamador APÓS journal `verificada`
        # (ver `execute`); aqui removemos apenas o nome temporário após
        # confirmação? Não — caller decide. Remove tmp-name agora, mantém dest.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # -- execute -----------------------------------------------------------

    def execute(
        self,
        plan: MovePlan,
        approval: Approval,
        *,
        _crash_after: str | None = None,
    ) -> OperationRecord:
        """Executa com approval vinculada; journaliza todos os estados."""
        operation_id = uuid.uuid4().hex
        expected = compute_plan_hash(plan.plan_id, plan.items)
        items = list(plan.items)

        def _fail_state(msg: str) -> OperationRecord:
            op = OperationRecord(
                operation_id=operation_id,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                state=OperationState.FALHOU,
                approval_token=approval.plan_hash,
                message=msg,
            )
            return self._journal(op, items)

        # 1) Approval vinculada (plan_id + plan_hash vigente + recomputado).
        if (
            approval.plan_id != plan.plan_id
            or approval.plan_hash != plan.plan_hash
            or approval.plan_hash != expected
        ):
            _fail_state(
                "approval divergente do plano (plan_id/hash); nova aprovação exigida"
            )
            raise PermissionError(
                "approval inválida ou desvinculada do plano; "
                "qualquer mudança exige nova aprovação explícita"
            )

        # Revalidação pré-execução (TOCTOU): preview sem escrever.
        # Colisão tardia (destino apareceu após preview externo) é mapeada
        # para FileExistsError para que o chamador distinga colisão de erro
        # de validação — sem overwrite em nenhum caso.
        try:
            self.preview(plan)
        except (ValueError, PermissionError, OSError) as exc:
            _fail_state(f"preview pré-execução recusou: {exc}")
            if isinstance(exc, ValueError) and "colis" in str(exc).lower():
                raise FileExistsError(str(exc)) from exc
            raise

        # 2) Dry-run: simulação journalizada, SEM mover.
        if plan.dry_run:
            op = OperationRecord(
                operation_id=operation_id,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                state=OperationState.VERIFICADA,
                approval_token=approval.plan_hash,
                message="simulação dry-run; nenhum arquivo movido",
            )
            return self._journal(op, items)

        # 3) Execução real: aprovada -> em_andamento ANTES de tocar FS.
        import dataclasses

        op = OperationRecord(
            operation_id=operation_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            state=OperationState.APROVADA,
            approval_token=approval.plan_hash,
            message="aprovada; início da execução",
        )
        op = self._journal(op, items)
        op = self._journal_state(op, OperationState.EM_ANDAMENTO,
                                 "em_andamento; FS ainda intacto", items)
        try:
            for item in items:
                abs_src = os.path.abspath(item.file_path)
                abs_dst = os.path.abspath(item.destination or "")
                abs_parent = os.path.dirname(abs_dst)
                # Colisão tardia (TOCTOU entre preview e item): aborta sem overwrite.
                try:
                    os.lstat(abs_dst)
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError(
                        f"colisão tardia: destino já existe: {abs_dst!r}"
                    )
                same_fs = self._same_filesystem(abs_src, abs_parent)
                if same_fs:
                    os.link(abs_src, abs_dst)
                    self._fsync_dir(abs_parent)
                    if _crash_after == "link":
                        # Crash simulado: deixa em_andamento (link ok, unlink pendente).
                        raise RuntimeError("crash simulado após link (teste)")
                    os.unlink(abs_src)
                    # Verificação pós-item: destino existe regular, origem sumiu.
                    st_dst = os.lstat(abs_dst)
                    if stat.S_ISLNK(st_dst.st_mode) or not stat.S_ISREG(
                        st_dst.st_mode
                    ):
                        raise IOError(
                            f"verificação pós-move falhou: {abs_dst!r}"
                        )
                    try:
                        os.lstat(abs_src)
                    except FileNotFoundError:
                        pass
                    else:
                        raise IOError(
                            f"origem ainda existe após move: {abs_src!r}"
                        )
                else:
                    # Cross-fs: cópia p/ tmp exclusivo + publicação sem overwrite.
                    parent = abs_parent
                    tmp_name = f".hexavante-tmp-{uuid.uuid4().hex}.tmp"
                    tmp_path = os.path.join(parent, tmp_name)
                    fd_tmp = os.open(
                        tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                    try:
                        with open(abs_src, "rb") as fsrc:
                            while True:
                                chunk = fsrc.read(_CHUNK_SIZE)
                                if not chunk:
                                    break
                                view = memoryview(chunk)
                                while view:
                                    n = os.write(fd_tmp, view)
                                    view = view[n:]
                        os.fsync(fd_tmp)
                    except BaseException:
                        try:
                            os.close(fd_tmp)
                        except OSError:
                            pass
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        raise
                    os.close(fd_tmp)
                    st_src = os.lstat(abs_src)
                    st_tmp = os.lstat(tmp_path)
                    if st_tmp.st_size != st_src.st_size:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        raise IOError("tamanho divergente após cópia cross-fs")
                    try:
                        os.lstat(abs_dst)
                    except FileNotFoundError:
                        pass
                    else:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        raise FileExistsError(
                            f"colisão entre preview e publicação: {abs_dst!r}"
                        )
                    os.link(tmp_path, abs_dst)
                    self._fsync_dir(parent)
                    if _crash_after == "link":
                        raise RuntimeError("crash simulado após link (teste)")
                    # Journal `verificada` ANTES de remover a origem (cross-fs).
                    op = self._journal_state(
                        op, OperationState.VERIFICADA,
                        "cross-fs publicado; origem será removida após verificação",
                        items,
                    )
                    # Reconfere destino antes de remover origem.
                    st_dst = os.lstat(abs_dst)
                    if stat.S_ISLNK(st_dst.st_mode) or not stat.S_ISREG(
                        st_dst.st_mode
                    ):
                        raise IOError("destino cross-fs inválido na verificação")
                    if st_dst.st_size != st_src.st_size:
                        raise IOError("tamanho do destino diverge na verificação")
                    os.unlink(abs_src)
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        except RuntimeError as exc:
            # Crash simulado: MANTÉM `em_andamento` (ou `verificada` cross-fs
            # se já journalizada) para `recover_pending` exercitar. Sem `falhou`.
            if "crash simulado" in str(exc):
                raise
            op = self._journal_state(op, OperationState.FALHOU, f"falhou: {exc}", items)
            raise
        except (FileExistsError, ValueError, PermissionError, OSError, IOError) as exc:
            # FileExistsError (colisão) preservado para o chamador distinguir de
            # falha genérica; demais viram a exceção original após journal.
            op = self._journal_state(op, OperationState.FALHOU, f"falhou: {exc}", items)
            raise
        except BaseException as exc:  # noqa: BLE001 — garantir journal em falha rara
            try:
                self._journal_state(op, OperationState.FALHOU, f"falhou: {exc}", items)
            except BaseException:
                pass
            raise

        # Verificação final de todos os itens + conclusão.
        try:
            for item in items:
                abs_src = os.path.abspath(item.file_path)
                abs_dst = os.path.abspath(item.destination or "")
                st_dst = os.lstat(abs_dst)
                if stat.S_ISLNK(st_dst.st_mode) or not stat.S_ISREG(st_dst.st_mode):
                    raise IOError(f"verificação final falhou: {abs_dst!r}")
                try:
                    os.lstat(abs_src)
                except FileNotFoundError:
                    pass
                else:
                    raise IOError(f"origem ainda existe: {abs_src!r}")
        except (OSError, IOError) as exc:
            op = self._journal_state(op, OperationState.FALHOU, f"falhou: {exc}", items)
            raise
        op = self._journal_state(op, OperationState.VERIFICADA,
                                 "verificada; integridade confirmada", items)
        op = self._journal_state(op, OperationState.CONCLUIDA,
                                 "concluida", items)
        return op

    # -- undo --------------------------------------------------------------

    def undo(self, operation: OperationRecord) -> OperationRecord:
        """Reverte operação `concluida` criando NOVA operação `<id>-undo`.

        Recusa com `ValueError` se o journal não estiver `concluida`, e com
        `FileExistsError` se a origem recriada conflitar (sem overwrite).
        """
        row = self._conn.execute(
            "SELECT operation_id, plan_id, plan_hash, state, approval_token, message,"
            " created_at, updated_at FROM operations_log WHERE operation_id = ?;",
            (operation.operation_id,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"operação desconhecida no journal: {operation.operation_id!r}"
            )
        state_in_db = row["state"]
        if state_in_db != OperationState.CONCLUIDA.value:
            raise ValueError(
                "só desfaz operação concluída; "
                f"estado atual={state_in_db!r} (op={operation.operation_id!r})"
            )
        item_rows = self._conn.execute(
            "SELECT source_path, dest_path FROM move_items"
            " WHERE operation_id = ? ORDER BY item_id;",
            (operation.operation_id,),
        ).fetchall()
        if not item_rows:
            raise ValueError(
                f"operação sem itens no journal: {operation.operation_id!r}"
            )
        pairs = [(r["source_path"], r["dest_path"]) for r in item_rows]

        new_op_id = uuid.uuid4().hex
        new_plan_id = f"{row['plan_id']}-undo"
        # Itens invertidos para journal da nova operação (dest->origem).
        reversed_items: list[ClassificationResult] = []
        for src, dst in pairs:
            reversed_items.append(
                ClassificationResult(
                    file_path=dst,
                    rule_id=None,
                    destination=src,
                    confidence=0.0,
                    reason="undo: reversão de operação concluída",
                )
            )
        import dataclasses

        new_op = OperationRecord(
            operation_id=new_op_id,
            plan_id=new_plan_id,
            plan_hash=row["plan_hash"],
            state=OperationState.APROVADA,
            approval_token=row["approval_token"],
            message="undo aprovado; início da reversão",
        )
        new_op = self._journal(new_op, reversed_items)
        new_op = self._journal_state(
            new_op, OperationState.EM_ANDAMENTO,
            "undo em_andamento; FS ainda intacto", reversed_items,
        )
        try:
            # Ordem invertida (último movido volta primeiro).
            for src, dst in reversed(pairs):
                # Agora: origem do undo = dst anterior; destino do undo = src anterior.
                abs_cur = os.path.abspath(dst)
                abs_back = os.path.abspath(src)
                # Escopo: ambos precisam estar contidos (origem volta p/ raiz).
                self._check_src_in_scope(abs_cur)
                abs_back_parent = os.path.abspath(os.path.dirname(abs_back))
                self._check_parent_allowed(abs_back_parent)
                # Verifica destino-atual existe regular não-symlink.
                try:
                    st_cur = os.lstat(abs_cur)
                except FileNotFoundError as exc:
                    raise ValueError(
                        f"undo: arquivo já sumiu em {abs_cur!r}"
                    ) from exc
                if stat.S_ISLNK(st_cur.st_mode):
                    raise PermissionError(
                        f"undo: origem do undo é symlink: {abs_cur!r}"
                    )
                if not stat.S_ISREG(st_cur.st_mode):
                    raise ValueError(
                        f"undo: origem do undo não é regular: {abs_cur!r}"
                    )
                # Conflito: origem recriada — recusa sem overwrite.
                try:
                    os.lstat(abs_back)
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError(
                        f"undo recusado: origem recriada conflita: {abs_back!r}"
                    )
                # Parent do destino-revertido precisa existir (sem criar).
                try:
                    st_parent = os.lstat(abs_back_parent)
                except FileNotFoundError as exc:
                    raise ValueError(
                        f"undo: diretório de retorno inexistente "
                        f"(criação não autorizada): {abs_back_parent!r}"
                    ) from exc
                if stat.S_ISLNK(st_parent.st_mode):
                    raise PermissionError(
                        f"undo: parent de retorno é symlink: {abs_back_parent!r}"
                    )
                if not stat.S_ISDIR(st_parent.st_mode):
                    raise ValueError(
                        f"undo: parent de retorno não é diretório: {abs_back_parent!r}"
                    )
                same_fs = self._same_filesystem(abs_cur, abs_back_parent)
                if same_fs:
                    os.link(abs_cur, abs_back)
                    self._fsync_dir(abs_back_parent)
                    os.unlink(abs_cur)
                else:
                    # Cross-fs reverso: mesma primitiva sem overwrite.
                    tmp_name = f".hexavante-undo-{uuid.uuid4().hex}.tmp"
                    tmp_path = os.path.join(abs_back_parent, tmp_name)
                    fd_tmp = os.open(
                        tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                    try:
                        with open(abs_cur, "rb") as fsrc:
                            while True:
                                chunk = fsrc.read(_CHUNK_SIZE)
                                if not chunk:
                                    break
                                view = memoryview(chunk)
                                while view:
                                    n = os.write(fd_tmp, view)
                                    view = view[n:]
                        os.fsync(fd_tmp)
                    except BaseException:
                        try:
                            os.close(fd_tmp)
                        except OSError:
                            pass
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        raise
                    os.close(fd_tmp)
                    st_c = os.lstat(abs_cur)
                    st_t = os.lstat(tmp_path)
                    if st_t.st_size != st_c.st_size:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        raise IOError("undo cross-fs: tamanho divergente")
                    try:
                        os.lstat(abs_back)
                    except FileNotFoundError:
                        pass
                    else:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        raise FileExistsError(
                            f"undo recusado: conflito tardio em {abs_back!r}"
                        )
                    os.link(tmp_path, abs_back)
                    self._fsync_dir(abs_back_parent)
                    new_op = self._journal_state(
                        new_op, OperationState.VERIFICADA,
                        "undo cross-fs publicado; removendo origem do undo",
                        reversed_items,
                    )
                    os.unlink(abs_cur)
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        except (ValueError, PermissionError, FileExistsError, OSError, IOError) as exc:
            new_op = self._journal_state(
                new_op, OperationState.FALHOU, f"undo falhou: {exc}", reversed_items
            )
            raise
        # Verificação final do undo.
        try:
            for src, dst in pairs:
                abs_cur = os.path.abspath(dst)
                abs_back = os.path.abspath(src)
                st_back = os.lstat(abs_back)
                if stat.S_ISLNK(st_back.st_mode) or not stat.S_ISREG(
                    st_back.st_mode
                ):
                    raise IOError(f"undo: verificação falhou em {abs_back!r}")
                try:
                    os.lstat(abs_cur)
                except FileNotFoundError:
                    pass
                else:
                    raise IOError(f"undo: origem do undo ainda existe: {abs_cur!r}")
        except (OSError, IOError) as exc:
            new_op = self._journal_state(
                new_op, OperationState.FALHOU, f"undo falhou: {exc}", reversed_items
            )
            raise
        new_op = self._journal_state(
            new_op, OperationState.VERIFICADA, "undo verificado", reversed_items
        )
        new_op = self._journal_state(
            new_op, OperationState.CONCLUIDA, "undo ok", reversed_items
        )
        return new_op


def recover_pending(conn: sqlite3.Connection) -> list[OperationRecord]:
    """Marca `aprovada`/`em_andamento`/`verificada` como `recuperacao_pendente`.

    Retorna os registros atualizados. Chamadores decidem retomar/abortar
    (esta função nunca move arquivos nem apaga journal).
    """
    rows = conn.execute(
        "SELECT operation_id, plan_id, plan_hash, state, approval_token, message,"
        " created_at, updated_at FROM operations_log"
        " WHERE state IN ('aprovada', 'em_andamento', 'verificada')"
        " ORDER BY created_at;"
    ).fetchall()
    result: list[OperationRecord] = []
    now = _now_iso()
    with conn:
        for r in rows:
            conn.execute(
                "UPDATE operations_log SET state = 'recuperacao_pendente',"
                " message = ?, updated_at = ? WHERE operation_id = ?;",
                (
                    "marcada para recuperação após crash/interrupção;"
                    " inspecionar FS e journal antes de retomar/abortar",
                    now,
                    r["operation_id"],
                ),
            )
            result.append(
                OperationRecord(
                    operation_id=r["operation_id"],
                    plan_id=r["plan_id"],
                    plan_hash=r["plan_hash"],
                    state=OperationState.RECUPERACAO_PENDENTE,
                    approval_token=r["approval_token"],
                    message=(
                        "marcada para recuperação após crash/interrupção;"
                        " inspecionar FS e journal antes de retomar/abortar"
                    ),
                    created_at=r["created_at"],
                    updated_at=now,
                )
            )
    return result
