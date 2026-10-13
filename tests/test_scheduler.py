"""Testes do DryRunScheduler — Sprint 06.

TODOS os testes usam `:memory:`/`tmp_path` + fixtures sintéticas e relógio
injetável; sem I/O pessoal, sem `sudo`, sem instalar serviços. O agendador
SÓ executa leitura (scan/classify em dry-run) + auditoria; nunca movimentação.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hexavante.models.entities import (
    ClassificationResult,
    FileRecord,
    OperationState,
    Schedule,
)
from hexavante.services.interfaces import Scheduler
from hexavante.services.scheduler import DryRunScheduler, parse_interval_seconds
from hexavante.storage.db import connect, migrate


# -- helpers sintéticos ------------------------------------------------------


def _mem_conn() -> sqlite3.Connection:
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _file_conn(tmp_path: Path, name: str = "sched.db") -> sqlite3.Connection:
    conn = connect(tmp_path / name)
    migrate(conn)
    return conn


class FakeClock:
    """Relógio injetável determinístico."""

    def __init__(self, start: datetime) -> None:
        self.now_dt = start

    def __call__(self) -> datetime:
        return self.now_dt

    def advance(self, seconds: float) -> None:
        self.now_dt = self.now_dt + timedelta(seconds=seconds)

    def set(self, value: datetime) -> None:
        self.now_dt = value


def _t0() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _files(n: int = 2) -> list[FileRecord]:
    return [
        FileRecord(path=f"/tmp/synth/a{i}.txt", size_bytes=10 + i, mtime_ns=1)
        for i in range(n)
    ]


class FakeScanner:
    """Stub de leitura: registra chamadas, exige dry_run True."""

    def __init__(self, files: list[FileRecord] | None = None) -> None:
        self.files = list(files) if files is not None else []
        self.calls: list[dict] = []

    def scan(self, root=None, *, dry_run: bool = True):  # type: ignore[no-untyped-def]
        self.calls.append({"root": root, "dry_run": dry_run})
        assert dry_run is True, "scheduler deve chamar scan em dry-run"
        return list(self.files)


class FakeClassifier:
    """Stub de leitura: sugere destino fixo por arquivo."""

    def __init__(self, destination: str | None = "Documentos") -> None:
        self.destination = destination
        self.calls: list[str] = []

    def classify(self, file: FileRecord) -> ClassificationResult:
        path = getattr(file, "path", str(file))
        self.calls.append(path)
        return ClassificationResult(
            file_path=path,
            rule_id="fake:t",
            destination=self.destination,
            confidence=0.9,
            reason="sugestão sintética de teste",
        )


class BoomScanner:
    """Scanner que sempre falha (erro sintético, sem FS)."""

    def __init__(self) -> None:
        self.calls = 0

    def scan(self, root=None, *, dry_run: bool = True):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise RuntimeError("falha sintética do scanner")


def _sched(
    sid: str = "s1",
    cron: str = "interval:3600",
    enabled: bool = True,
    last: str | None = None,
    dry: bool = True,
) -> Schedule:
    return Schedule(
        schedule_id=sid, name=f"Agenda {sid}", cron_expr=cron, enabled=enabled,
        dry_run=dry, last_run_at=last,
    )


# -- persistência ------------------------------------------------------------


def test_persistencia_save_get() -> None:
    conn = _mem_conn()
    try:
        sch = DryRunScheduler(conn, clock=FakeClock(_t0()))
        s = _sched("s1", "interval:60")
        sch.save_schedule(s)
        got = sch.get_schedule("s1")
        assert got is not None
        assert got.schedule_id == "s1"
        assert got.cron_expr == "interval:60"
        assert got.enabled is True
        assert sch.get("s1") == got
        assert sch.get_schedule("ausente") is None
    finally:
        conn.close()


def test_upsert_sobrescreve() -> None:
    conn = _mem_conn()
    try:
        sch = DryRunScheduler(conn, clock=FakeClock(_t0()))
        sch.save_schedule(_sched("u1", "interval:60"))
        sch.upsert(_sched("u1", "@daily"))
        got = sch.get_schedule("u1")
        assert got is not None
        assert got.cron_expr == "@daily"
        # upsert não duplica linha
        n = conn.execute("SELECT COUNT(*) FROM schedules WHERE schedule_id='u1';").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_enable_disable_persiste() -> None:
    conn = _mem_conn()
    try:
        sch = DryRunScheduler(conn, clock=FakeClock(_t0()))
        sch.save_schedule(_sched("e1", "interval:60", enabled=True))
        sch.disable("e1")
        assert sch.get_schedule("e1") is not None
        assert sch.get_schedule("e1").enabled is False  # type: ignore[union-attr]
        # desabilitado persiste após releitura
        row = conn.execute("SELECT enabled FROM schedules WHERE schedule_id='e1';").fetchone()
        assert int(row[0]) == 0
        sch.enable("e1")
        assert sch.get_schedule("e1").enabled is True  # type: ignore[union-attr]
        assert sch.enable("ausente") is None
        assert sch.disable("ausente") is None
    finally:
        conn.close()


def test_list_retorna_todas_ordenadas() -> None:
    conn = _mem_conn()
    try:
        sch = DryRunScheduler(conn, clock=FakeClock(_t0()))
        sch.save_schedule(_sched("b2"))
        sch.save_schedule(_sched("a1"))
        ids = [s.schedule_id for s in sch.list_schedules()]
        assert ids == ["a1", "b2"]
        assert [s.schedule_id for s in sch.list()] == ids
    finally:
        conn.close()


# -- next_run ----------------------------------------------------------------


def test_next_run_none_se_disabled() -> None:
    sch = DryRunScheduler(_mem_conn(), clock=FakeClock(_t0()))
    s = _sched("d1", "interval:60", enabled=False, last=_t0().isoformat())
    assert sch.next_run(s) is None
    assert sch.is_due(s) is False


@pytest.mark.parametrize(
    "cron", ["", "   ", "blah", "cron * * *", "interval:", "interval:abc", "interval:0", "interval:-5"]
)
def test_next_run_none_se_cron_vazio_ou_invalido(cron: str) -> None:
    sch = DryRunScheduler(_mem_conn(), clock=FakeClock(_t0()))
    s = _sched("i1", cron, last=None)
    assert parse_interval_seconds(cron) is None
    assert sch.next_run(s) is None
    assert sch.is_due(s) is False


def test_next_run_interval_soma_last() -> None:
    t0 = _t0()
    sch = DryRunScheduler(_mem_conn(), clock=FakeClock(t0))
    last = (t0 - timedelta(seconds=100)).isoformat()
    s = _sched("n1", "interval:3600", last=last)
    nxt = sch.next_run(s)
    assert nxt is not None
    expected = datetime.fromisoformat(last) + timedelta(seconds=3600)
    assert datetime.fromisoformat(nxt) == expected


def test_next_run_sem_last_retorna_agora() -> None:
    t0 = _t0()
    sch = DryRunScheduler(_mem_conn(), clock=FakeClock(t0))
    s = _sched("n2", "interval:60", last=None)
    assert sch.next_run(s) == t0.isoformat()
    assert sch.is_due(s) is True


@pytest.mark.parametrize(
    ("cron", "segs"), [("@hourly", 3600), ("@daily", 86400), ("@HOURLY", 3600)]
)
def test_next_run_hourly_daily(cron: str, segs: int) -> None:
    t0 = _t0()
    sch = DryRunScheduler(_mem_conn(), clock=FakeClock(t0))
    assert parse_interval_seconds(cron) == segs
    s = _sched("h1", cron, last=t0.isoformat())
    nxt = sch.next_run(s)
    assert nxt is not None
    assert datetime.fromisoformat(nxt) == t0 + timedelta(seconds=segs)


# -- run_once dry-run --------------------------------------------------------


def test_run_once_dryrun_ok_executa_scan_classify_e_atualiza_last() -> None:
    t0 = _t0()
    conn = _mem_conn()
    try:
        clock = FakeClock(t0)
        run_log: list = []
        scanner = FakeScanner(_files(2))
        classifier = FakeClassifier()
        sch = DryRunScheduler(conn, scanner=scanner, classifier=classifier, clock=clock, run_log=run_log)
        s = _sched("r1", "interval:3600", last=None)
        sch.save_schedule(s)
        rec = sch.run_once(s)
        assert rec is not None
        assert rec.state == OperationState.VERIFICADA
        # scan/classify chamados em leitura
        assert len(scanner.calls) == 1
        assert scanner.calls[0]["dry_run"] is True
        assert len(classifier.calls) == 2
        # last_run_at avançou para agora
        fresh = sch.get_schedule("r1")
        assert fresh is not None and fresh.last_run_at == t0.isoformat()
        # auditoria verificada com contagens, sem movimentação
        row = conn.execute(
            "SELECT state, message FROM operations_log WHERE operation_id=?;", (rec.operation_id,)
        ).fetchone()
        assert row is not None and row["state"] == "verificada"
        assert "2 arquivo(s)" in row["message"] and "nenhuma movimentação" in row["message"]
        # run_log observável sem FS
        assert any(e.get("status") == "ok" and e.get("scanned") == 2 for e in run_log)
    finally:
        conn.close()


def test_run_once_dryrun_false_recusa() -> None:
    conn = _mem_conn()
    try:
        scanner = FakeScanner(_files(1))
        sch = DryRunScheduler(conn, scanner=scanner, clock=FakeClock(_t0()), run_log=[])
        s = _sched("rf", "interval:60")
        sch.save_schedule(s)
        with pytest.raises(ValueError):
            sch.run_once(s, dry_run=False)
        assert scanner.calls == []
        n = conn.execute("SELECT COUNT(*) FROM operations_log;").fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_run_once_schedule_com_dryrun_false_recusa() -> None:
    conn = _mem_conn()
    try:
        scanner = FakeScanner(_files(1))
        sch = DryRunScheduler(conn, scanner=scanner, clock=FakeClock(_t0()), run_log=[])
        s = _sched("rdf", "interval:60", dry=False)
        sch.save_schedule(s)
        with pytest.raises(ValueError):
            sch.run_once(s, dry_run=True)
        assert scanner.calls == []
    finally:
        conn.close()


def test_run_once_disabled_nao_executa() -> None:
    conn = _mem_conn()
    try:
        run_log: list = []
        scanner = FakeScanner(_files(3))
        sch = DryRunScheduler(conn, scanner=scanner, clock=FakeClock(_t0()), run_log=run_log)
        s = _sched("dis", "interval:60", enabled=False)
        sch.save_schedule(s)
        assert sch.run_once(s) is None
        assert scanner.calls == []
        assert any(e.get("status") == "skipped_disabled" for e in run_log)
        # objeto obsoleto habilitado mas DB desabilitado também recusa
        run_log.clear()
        sch.disable("dis")
        stale_enabled = _sched("dis", "interval:60", enabled=True)
        assert sch.run_once(stale_enabled) is None
        assert any(e.get("status") == "skipped_disabled" for e in run_log)
    finally:
        conn.close()


def test_run_once_sem_scanner_nem_classifier_audita_zero() -> None:
    t0 = _t0()
    conn = _mem_conn()
    try:
        run_log: list = []
        sch = DryRunScheduler(conn, clock=FakeClock(t0), run_log=run_log)
        s = _sched("z1", "interval:60")
        sch.save_schedule(s)
        rec = sch.run_once(s)
        assert rec is not None and rec.state == OperationState.VERIFICADA
        assert "0 arquivo(s)" in rec.message
    finally:
        conn.close()


# -- sobreposição ------------------------------------------------------------


def test_overlap_memoria_recusado_sem_duplicar() -> None:
    conn = _mem_conn()
    try:
        run_log: list = []
        scanner = FakeScanner(_files(1))
        sch = DryRunScheduler(conn, scanner=scanner, clock=FakeClock(_t0()), run_log=run_log)
        s = _sched("o1", "interval:60")
        sch.save_schedule(s)
        sch._running.add("o1")  # simula execução em curso nesta instância
        assert sch.run_once(s) is None
        assert scanner.calls == []
        assert any(e.get("status") == "skipped_overlap" for e in run_log)
        n = conn.execute("SELECT COUNT(*) FROM operations_log;").fetchone()[0]
        assert n == 0
        # após liberar, roda normalmente
        sch._running.discard("o1")
        assert sch.run_once(s) is not None
    finally:
        conn.close()


def test_overlap_concorrente_thread_sem_duplicar(tmp_path: Path) -> None:
    db = tmp_path / "thr.db"
    raw = sqlite3.connect(str(db), check_same_thread=False)
    try:
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON;")
        migrate(raw)
        t0 = _t0()
        entered = threading.Event()
        release = threading.Event()

        class BlockingScanner:
            def __init__(self) -> None:
                self.calls = 0

            def scan(self, root=None, *, dry_run: bool = True):  # type: ignore[no-untyped-def]
                self.calls += 1
                entered.set()
                assert release.wait(timeout=5), "timeout no scanner bloqueante"
                return []

        scanner = BlockingScanner()
        run_log: list = []
        sch = DryRunScheduler(raw, scanner=scanner, clock=FakeClock(t0), run_log=run_log)
        s = _sched("ot", "interval:60")
        sch.save_schedule(s)
        results: dict = {}

        def _first() -> None:
            results["first"] = sch.run_once(s)

        th = threading.Thread(target=_first)
        th.start()
        assert entered.wait(timeout=5), "primeira execução não iniciou"
        second = sch.run_once(s)  # concorrente: deve recusar
        assert second is None
        assert any(e.get("status") == "skipped_overlap" for e in run_log)
        release.set()
        th.join(timeout=5)
        assert not th.is_alive()
        assert results.get("first") is not None
        assert scanner.calls == 1  # sem trabalho duplicado
        n = raw.execute(
            "SELECT COUNT(*) FROM operations_log WHERE state='verificada';"
        ).fetchone()[0]
        assert n == 1
    finally:
        try:
            raw.close()
        except Exception:
            pass


# -- erros sem perda silenciosa ----------------------------------------------


def test_erro_scanner_registrado_sem_crash() -> None:
    t0 = _t0()
    conn = _mem_conn()
    try:
        run_log: list = []
        scanner = BoomScanner()
        sch = DryRunScheduler(conn, scanner=scanner, clock=FakeClock(t0), run_log=run_log)
        s = _sched("err1", "interval:60")
        sch.save_schedule(s)
        rec = sch.run_once(s)  # não propaga
        assert rec is None
        assert scanner.calls == 1
        rows = conn.execute(
            "SELECT state, message FROM operations_log WHERE plan_id LIKE 'plan-err1-%';"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["state"] == "falhou"
        assert "falha sintética" in rows[0]["message"]
        errs = [e for e in run_log if e.get("status") == "error"]
        assert len(errs) == 1 and "falha sintética" in errs[0].get("error", "")
        # estado persistido: last_run_at avançou (evita loop apertado)
        assert sch.get_schedule("err1").last_run_at == t0.isoformat()  # type: ignore[union-attr]
    finally:
        conn.close()


# -- restart sem trava fantasma ----------------------------------------------


def test_restart_sem_lock_fantasma_mesmo_arquivo(tmp_path: Path) -> None:
    db = tmp_path / "restart.db"
    c1 = _file_conn(tmp_path, "restart.db")
    try:
        t0 = _t0()
        s = _sched("rs1", "interval:60")
        s1 = DryRunScheduler(c1, scanner=FakeScanner(_files(1)), clock=FakeClock(t0), run_log=[])
        s1.save_schedule(s)
        s1._running.add("rs1")  # simula instância antiga travada em memória
        c2 = connect(db)
        try:
            migrate(c2)
            run_log2: list = []
            s2 = DryRunScheduler(
                c2, scanner=FakeScanner(_files(1)), classifier=FakeClassifier(),
                clock=FakeClock(t0), run_log=run_log2,
            )
            rec = s2.run_once(s)  # nova instância não herda trava: roda
            assert rec is not None and rec.state == OperationState.VERIFICADA
            assert any(e.get("status") == "ok" for e in run_log2)
        finally:
            c2.close()
    finally:
        c1.close()


def test_recover_after_restart_limpa_travas() -> None:
    conn = _mem_conn()
    try:
        sch = DryRunScheduler(conn, scanner=FakeScanner([]), clock=FakeClock(_t0()), run_log=[])
        sch._running.add("x1")
        assert sch.recover_after_restart() == []
        assert sch._running == set()
        s = _sched("x1", "interval:60")
        sch.save_schedule(s)
        assert sch.run_once(s) is not None
    finally:
        conn.close()


# -- garantia: nunca movimentação --------------------------------------------


def test_nunca_importa_camada_de_movimentacao() -> None:
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parent.parent / "src" / "hexavante" / "services" / "scheduler.py").read_text(
        encoding="utf-8"
    )
    assert "move_service" not in src
    assert "MoveService" not in src
    import hexavante.services.scheduler as mod

    assert not hasattr(mod.DryRunScheduler, "execute")
    assert not hasattr(mod.DryRunScheduler, "move_service")


def test_nunca_chama_execute_e_nao_altera_fs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    class ExplosiveMove:
        def preview(self, plan):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            raise AssertionError("preview nunca deve ser chamado pelo agendador")

        def execute(self, plan, approval=None):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            raise AssertionError("execute nunca deve ser chamado pelo agendador")

        def undo(self, operation):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            raise AssertionError("undo nunca deve ser chamado pelo agendador")

    import sys
    import types

    fake_mod = types.ModuleType("hexavante.services.move_service")
    fake_mod.JournaledMoveService = ExplosiveMove  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hexavante.services.move_service", fake_mod)

    victim = tmp_path / "v keep.txt"
    victim.write_bytes(b"conteudo intacto")
    before = victim.read_bytes()

    conn = _mem_conn()
    try:
        sch = DryRunScheduler(
            conn, scanner=FakeScanner(_files(2)), classifier=FakeClassifier(),
            clock=FakeClock(_t0()), run_log=[],
        )
        s = _sched("nm1", "interval:60")
        sch.save_schedule(s)
        rec = sch.run_once(s)
        assert rec is not None
        assert calls["n"] == 0  # nada da camada de movimentação foi tocado
        assert victim.read_bytes() == before  # FS intacto
        # auditoria só em dry-run: nunca concluída/em_andamento por agenda
        states = {r[0] for r in conn.execute("SELECT DISTINCT state FROM operations_log;").fetchall()}
        assert states <= {"verificada", "falhou"}
        assert "concluida" not in states and "em_andamento" not in states
    finally:
        conn.close()


# -- perdidas: catch-up único -------------------------------------------------


def test_missed_catchup_unico_sem_fila() -> None:
    t0 = _t0()
    conn = _mem_conn()
    try:
        clock = FakeClock(t0)
        sch = DryRunScheduler(conn, scanner=FakeScanner([]), clock=clock, run_log=[])
        last = (t0 - timedelta(seconds=3 * 3600)).isoformat()  # 3 intervalos perdidos
        s = _sched("m1", "interval:3600", last=last)
        sch.save_schedule(s)
        nxt = sch.next_run(s)
        assert nxt is not None
        assert datetime.fromisoformat(nxt) < t0  # vencido
        assert sch.is_due(s) is True
        rec = sch.run_once(s)
        assert rec is not None
        fresh = sch.get_schedule("m1")
        assert fresh is not None
        assert fresh.last_run_at == t0.isoformat()  # pula intermediários: um catch-up
        assert sch.is_due(fresh) is False  # sem fila imediata
        assert sch.due_schedules() == []
        # após um intervalo, volta a vencer exatamente uma vez
        clock.advance(3600)
        assert sch.is_due(sch.get_schedule("m1")) is True  # type: ignore[arg-type]
        assert len(sch.due_schedules()) == 1
    finally:
        conn.close()


def test_run_due_all_so_vencidas_habilitadas() -> None:
    t0 = _t0()
    conn = _mem_conn()
    try:
        clock = FakeClock(t0)
        run_log: list = []
        sch = DryRunScheduler(conn, scanner=FakeScanner([]), clock=clock, run_log=run_log)
        sch.save_schedule(_sched("due1", "interval:60", last=(t0 - timedelta(seconds=120)).isoformat()))
        sch.save_schedule(_sched("dis1", "interval:60", enabled=False, last=(t0 - timedelta(seconds=9999)).isoformat()))
        sch.save_schedule(_sched("inv1", "blah", last=None))
        sch.save_schedule(_sched("fut1", "interval:3600", last=t0.isoformat()))
        out = sch.run_due_all()
        assert len(out) == 1  # só due1
        assert out[0] is not None and out[0].state == OperationState.VERIFICADA  # type: ignore[union-attr]
        # desabilitada nunca roda nem entra em catch-up
        assert sch.get_schedule("dis1").enabled is False  # type: ignore[union-attr]
        assert sch.run_once(_sched("dis1", "interval:60", enabled=False)) is None
    finally:
        conn.close()


def test_run_due_all_recusa_dryrun_false() -> None:
    conn = _mem_conn()
    try:
        sch = DryRunScheduler(conn, clock=FakeClock(_t0()))
        with pytest.raises(ValueError):
            sch.run_due_all(dry_run=False)  # type: ignore[arg-type]
    finally:
        conn.close()


def test_conforma_ao_protocol_scheduler() -> None:
    sch = DryRunScheduler(_mem_conn(), clock=FakeClock(_t0()))
    assert isinstance(sch, Scheduler)
    assert callable(getattr(sch, "next_run"))
    assert callable(getattr(sch, "run_once"))
