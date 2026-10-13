"""Testes do DownloadMonitor — Sprint 05.

TODOS os testes usam `tmp_path` + fixtures sintéticas, sem display real
(nenhum GTK; `Gio` só dentro de `DownloadMonitor.start()` com guard para
`polling-stub`). Nenhum toca em `~` real, nenhum usa `sudo`, nenhum
instala serviços. O monitor SÓ sugere (`pending`); nunca move/executa.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from hexavante.models.entities import FileRecord
from hexavante.services.interfaces import Monitor
from hexavante.services.monitor import DownloadMonitor


def _make_dl(tmp_path: Path, name: str = "dl") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return root


# -- autorização -----------------------------------------------------------------

def test_raiz_nao_autorizada_recusa(tmp_path: Path) -> None:
    allowed = _make_dl(tmp_path, "allowed")
    other = _make_dl(tmp_path, "other")
    monitor = DownloadMonitor([allowed])
    with pytest.raises(PermissionError):
        monitor.start(other)


def test_symlink_raiz_recusa(tmp_path: Path) -> None:
    real = _make_dl(tmp_path, "real")
    linkroot = tmp_path / "linkroot"
    linkroot.symlink_to(real, target_is_directory=True)
    monitor = DownloadMonitor([linkroot])
    with pytest.raises(PermissionError):
        monitor.start(linkroot)


def test_escape_via_dotdot_recusa(tmp_path: Path) -> None:
    allowed = _make_dl(tmp_path, "allowed")
    outside = _make_dl(tmp_path, "outside")
    monitor = DownloadMonitor([allowed])
    with pytest.raises(PermissionError):
        monitor.start(allowed / ".." / "outside")
    with pytest.raises(PermissionError):
        monitor.start(tmp_path)


def test_dry_run_false_recusa(tmp_path: Path) -> None:
    root = _make_dl(tmp_path)
    monitor = DownloadMonitor([tmp_path])
    with pytest.raises(ValueError):
        monitor.start(root, dry_run=False)  # monitor nunca executa movimentação
    assert monitor.started is False


# -- debounce puro (relógio injetado) ----------------------------------------------

def test_debounce_coalesce_com_relogio_injetado(tmp_path: Path) -> None:
    monitor = DownloadMonitor([tmp_path], debounce_s=1.0)
    target = os.fspath(tmp_path / "arq.zip")

    assert monitor.processable(target, now=100.0) is True
    assert monitor.processable(target, now=100.2) is False  # dentro da janela
    assert monitor.processable(target, now=100.9) is False
    assert monitor.debounce_count(target) == 3
    assert monitor.processable(target, now=101.5) is True  # janela passou
    assert monitor.debounce_count(target) == 4


def test_eventos_duplicados_coalescidos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_dl(tmp_path)
    target = root / "video.mp4"
    target.write_bytes(b"dados" * 100)
    monitor = DownloadMonitor([tmp_path], debounce_s=10.0)

    times = iter([500.0, 500.1, 500.2])
    monkeypatch.setattr("time.monotonic", lambda: next(times))

    first = monitor.handle_event(str(target))
    assert isinstance(first, FileRecord)
    assert len(monitor.pending) == 1
    # Rajada duplicada dentro da janela: coalescida, sem nova sugestão.
    assert monitor.handle_event(str(target)) is None
    assert monitor.handle_event(target) is None
    assert len(monitor.pending) == 1
    assert monitor.debounce_count(str(target)) == 3


def test_pending_atualiza_em_vez_de_duplicar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_dl(tmp_path)
    target = root / "doc.pdf"
    target.write_bytes(b"v1")
    monitor = DownloadMonitor([tmp_path], debounce_s=1.0)

    times = iter([10.0, 12.0])  # segundo evento após a janela
    monkeypatch.setattr("time.monotonic", lambda: next(times))
    monitor.handle_event(str(target))
    target.write_bytes(b"versao-2-maior")
    monitor.handle_event(str(target))

    assert len(monitor.pending) == 1
    assert monitor.pending[0].size_bytes == (root / "doc.pdf").stat().st_size
    assert monitor.debounce_count(str(target)) == 2


# -- temporários ignorados -----------------------------------------------------------

@pytest.mark.parametrize(
    "name", ["f.mp4.part", "a.crdownload", "r.tmp", "~backup.docx", ".~lock.rel.odt#"]
)
def test_temporarios_ignorados(tmp_path: Path, name: str) -> None:
    root = _make_dl(tmp_path)
    target = root / name
    target.write_bytes(b"parcial")
    monitor = DownloadMonitor([tmp_path])

    assert monitor.is_stable_candidate(str(target)) is False
    assert monitor.handle_event(str(target)) is None
    assert monitor.pending == []


# -- estabilidade por lstat duplo -----------------------------------------------------

def test_estavel_quando_size_igual_e_regular(tmp_path: Path) -> None:
    root = _make_dl(tmp_path)
    target = root / "ok.zip"
    target.write_bytes(b"final" * 50)
    monitor = DownloadMonitor([tmp_path])
    assert monitor.is_stable_candidate(str(target)) is True


def test_instavel_quando_cresce_entre_leituras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_dl(tmp_path)
    target = root / "crescendo.bin"
    target.write_bytes(b"0123456789")
    monitor = DownloadMonitor([tmp_path])
    real_lstat = os.lstat
    calls = {"n": 0}
    abs_target = os.path.abspath(os.fspath(target))

    def fake_lstat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        st = real_lstat(path, *args, **kwargs)
        if os.path.abspath(os.fspath(path)) != abs_target:
            return st
        calls["n"] += 1
        if calls["n"] >= 2:
            class Cresceu:
                pass
            fake = Cresceu()
            fake.st_mode = st.st_mode  # regular
            fake.st_size = st.st_size + 999  # cresceu: download parcial
            fake.st_mtime_ns = st.st_mtime_ns
            return fake  # type: ignore[return-value]
        return st

    monkeypatch.setattr(os, "lstat", fake_lstat)
    assert monitor.is_stable_candidate(str(target)) is False


def test_symlink_e_fifo_nao_sao_estaveis(tmp_path: Path) -> None:
    root = _make_dl(tmp_path)
    real = root / "real.txt"
    real.write_bytes(b"ok")
    link = root / "atalho.txt"
    link.symlink_to(real)
    fifo = root / "canal.fifo"
    os.mkfifo(fifo)
    monitor = DownloadMonitor([tmp_path])
    assert monitor.is_stable_candidate(str(link)) is False
    assert monitor.is_stable_candidate(str(fifo)) is False
    assert monitor.handle_event(str(link)) is None
    assert monitor.handle_event(str(fifo)) is None
    assert monitor.pending == []


# -- start/stop sem display; stub; só sugere --------------------------------------------

def test_start_stop_sem_display_nao_crash(tmp_path: Path) -> None:
    root = _make_dl(tmp_path)
    monitor = DownloadMonitor([tmp_path])
    monitor.start(root)  # Gio real ou polling-stub: ambos aceitos, sem crash
    assert monitor.started is True
    assert monitor.mode in ("gio", "polling-stub")
    monitor.stop()
    assert monitor.started is False
    assert monitor.mode == "disconnected"
    monitor.stop()  # parar parado não crasha


def test_stub_quando_gi_indisponivel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_dl(tmp_path)
    monitor = DownloadMonitor([tmp_path])
    # Simula headless sem PyGObject: `from gi.repository import Gio` falha.
    monkeypatch.setitem(sys.modules, "gi", None)
    monkeypatch.setitem(sys.modules, "gi.repository", None)
    monitor.start(root)
    assert monitor.started is True
    assert monitor.mode == "polling-stub"
    # API pura segue funcional no stub.
    assert monitor.processable("x", now=1.0) is True
    monitor.stop()
    assert monitor.started is False


def test_handle_event_so_sugere_nunca_move(tmp_path: Path) -> None:
    root = _make_dl(tmp_path)
    target = root / "novo.pdf"
    target.write_bytes(b"%PDF-1.4 conteudo")
    before_listing = sorted(os.fspath(p) for p in root.rglob("*"))
    before_bytes = target.read_bytes()
    seen: list[FileRecord] = []
    monitor = DownloadMonitor([tmp_path], callback=seen.append)

    record = monitor.handle_event(str(target))

    assert isinstance(record, FileRecord)
    assert record.sha256 is None  # hash sob demanda no detector
    assert record.path == os.path.abspath(os.fspath(target))
    assert len(monitor.pending) == 1
    assert seen == [record]
    # Filesystem intacto: nada criado/movido/apagado pelo monitor.
    assert sorted(os.fspath(p) for p in root.rglob("*")) == before_listing
    assert target.read_bytes() == before_bytes


def test_conforma_ao_protocol_monitor(tmp_path: Path) -> None:
    assert isinstance(DownloadMonitor([tmp_path]), Monitor)
