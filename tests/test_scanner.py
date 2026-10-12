"""Testes do SafeScanner — Sprint 01.

TODOS os testes usam `tmp_path` + fixtures sintéticas. Nenhum toca em
`~` real, nenhum usa `sudo`, nenhum instala serviços, nenhuma escrita
fora de `tmp_path`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hexavante.services.interfaces import Scanner
from hexavante.services.scanner import SafeScanner


def _make_root(tmp_path: Path, name: str = "allowed") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return root


# -- inventário básico + MIME -----------------------------------------------

def test_scan_inventaria_arquivos_reais_com_mime_e_lstat(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root / "a.txt").write_bytes(b"ola")
    (root / "b.pdf").write_bytes(b"%PDF-1.4 fake")
    sub = root / "sub"
    sub.mkdir()
    (sub / "c.txt").write_bytes(b"xyz")

    scanner = SafeScanner([root])
    records, stats = scanner.scan_with_stats(root)

    by_name = {Path(r.path).name: r for r in records}
    assert set(by_name) == {"a.txt", "b.pdf", "c.txt"}
    assert stats.scanned == 3
    assert stats.truncated is False

    txt = by_name["a.txt"]
    assert txt.mime_type == "text/plain"
    assert txt.sha256 is None  # hash só na Sprint 05
    assert txt.is_stable is False  # estabilização só no monitor (Sprint 05)
    # size + mtime via lstat (não stat que segue link).
    st = os.lstat(root / "a.txt")
    assert txt.size_bytes == st.st_size == 3
    assert txt.mtime_ns == st.st_mtime_ns

    pdf = by_name["b.pdf"]
    assert pdf.mime_type == "application/pdf"


def test_scan_mime_fallback_octet_stream(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root / "semextensao").write_bytes(b"data")
    (root / "estranho.zzz9desconhecido").write_bytes(b"data")

    records, _stats = SafeScanner([root]).scan_with_stats(root)
    assert len(records) == 2
    assert all(r.mime_type == "application/octet-stream" for r in records)


def test_scan_nao_escreve_no_destino(tmp_path: Path) -> None:
    """Dry-run por padrão: varrer não cria/modifica nada."""
    root = _make_root(tmp_path)
    (root / "a.txt").write_bytes(b"ola")
    before = sorted(p.name for p in root.iterdir())

    SafeScanner([root]).scan(root)

    after = sorted(p.name for p in root.iterdir())
    assert before == after == ["a.txt"]


def test_scan_conforma_ao_protocol_scanner(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    scanner = SafeScanner([root])
    assert isinstance(scanner, Scanner)
    assert scanner.scan(root) == []  # raiz vazia: lista vazia, sem erro


# -- symlinks: nunca seguir --------------------------------------------------------

def test_scan_pula_symlink_de_arquivo(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    real = root / "real.txt"
    real.write_bytes(b"conteudo")
    (root / "atalho.txt").symlink_to(real)
    # Symlink quebrado (dangling) também deve ser pulado, não crashar.
    (root / "quebrado.txt").symlink_to(root / "alvo-inexistente.txt")

    records, stats = SafeScanner([root]).scan_with_stats(root)

    assert [Path(r.path).name for r in records] == ["real.txt"]
    assert stats.skipped_symlinks == 2
    assert stats.skipped_errors == 0


def test_scan_nao_segue_symlink_de_dir_para_fora(tmp_path: Path) -> None:
    """Escape de raízes: link p/ fora do escopo nunca é atravessado."""
    root = _make_root(tmp_path)
    outside = _make_root(tmp_path, "outside")
    (outside / "segredo.txt").write_bytes(b"top-secret")
    (root / "dentro.txt").write_bytes(b"ok")
    (root / "linkfora").symlink_to(outside, target_is_directory=True)

    records, stats = SafeScanner([root]).scan_with_stats(root)

    names = [Path(r.path).name for r in records]
    assert names == ["dentro.txt"]
    assert "segredo.txt" not in names
    assert all("outside" not in r.path for r in records)
    assert stats.skipped_symlinks == 1


def test_scan_root_symlink_rejeitada(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root / "a.txt").write_bytes(b"x")
    linkroot = tmp_path / "linkroot"
    linkroot.symlink_to(root, target_is_directory=True)

    scanner = SafeScanner([linkroot])
    with pytest.raises(PermissionError):
        scanner.scan(linkroot)


# -- arquivos especiais e temporários ----------------------------------------------

def test_scan_pula_fifo_como_especial(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root / "normal.txt").write_bytes(b"ok")
    os.mkfifo(root / "canal.fifo")

    records, stats = SafeScanner([root]).scan_with_stats(root)

    assert [Path(r.path).name for r in records] == ["normal.txt"]
    assert stats.skipped_special == 1


def test_scan_pula_marcadores_temporarios(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root / "ok.txt").write_bytes(b"ok")
    temp_names = [
        "video.mp4.part",
        "arquivo.crdownload",
        "rascunho.tmp",
        "~backup.docx",
        ".~lock.relatorio.odt#",
    ]
    for name in temp_names:
        (root / name).write_bytes(b"parcial")

    records, stats = SafeScanner([root]).scan_with_stats(root)

    assert [Path(r.path).name for r in records] == ["ok.txt"]
    assert stats.skipped_temp == len(temp_names)


# -- limites -----------------------------------------------------------------------

def test_scan_respeita_max_depth(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    (root / "nivel0.txt").write_bytes(b"0")
    deep = root
    for i in range(1, 8):
        deep = deep / f"l{i}"
        deep.mkdir()
    (deep / "profundo.txt").write_bytes(b"fundo")  # 7 níveis abaixo da raiz

    scanner = SafeScanner([root], max_depth=2)
    records, _stats = scanner.scan_with_stats(root)
    names = [Path(r.path).name for r in records]
    assert "nivel0.txt" in names
    assert "profundo.txt" not in names

    # Com profundidade suficiente, o arquivo aparece.
    records_full, _ = SafeScanner([root]).scan_with_stats(root)  # default max_depth=5? não alcança
    assert "profundo.txt" not in [Path(r.path).name for r in records_full]
    records_deep, _ = SafeScanner([root], max_depth=10).scan_with_stats(root)
    assert "profundo.txt" in [Path(r.path).name for r in records_deep]


def test_scan_max_files_trunca(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    for i in range(10):
        (root / f"f{i:02d}.txt").write_bytes(b"x")

    records, stats = SafeScanner([root], max_files=3).scan_with_stats(root)

    assert len(records) == 3
    assert stats.scanned == 3
    assert stats.truncated is True


def test_scan_max_files_via_override_por_chamada(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    for i in range(5):
        (root / f"g{i}.txt").write_bytes(b"x")

    records, stats = SafeScanner([root]).scan_with_stats(root, max_files=2)
    assert len(records) == 2
    assert stats.truncated is True


# -- autorização: raízes, .., travessia ----------------------------------------------

def test_scan_root_nao_autorizada_levanta_permissionerror(tmp_path: Path) -> None:
    allowed = _make_root(tmp_path, "allowed")
    other = _make_root(tmp_path, "other")
    (other / "a.txt").write_bytes(b"x")

    scanner = SafeScanner([allowed])
    with pytest.raises(PermissionError):
        scanner.scan(other)


def test_scan_escape_via_dotdot_levanta_permissionerror(tmp_path: Path) -> None:
    allowed = _make_root(tmp_path, "allowed")
    outside = _make_root(tmp_path, "outside")
    (outside / "a.txt").write_bytes(b"x")

    scanner = SafeScanner([allowed])
    # Travessia com `..` que sai do escopo autorizado.
    with pytest.raises(PermissionError):
        scanner.scan(allowed / ".." / "outside")
    # O pai comum também está fora do escopo.
    with pytest.raises(PermissionError):
        scanner.scan(tmp_path)


def test_scan_subraiz_autorizada_ok(tmp_path: Path) -> None:
    base = _make_root(tmp_path, "base")
    sub = base / "sub"
    sub.mkdir()
    (sub / "a.txt").write_bytes(b"x")

    # Autorizar a base permite varrer a sub-raiz contida nela.
    records, _ = SafeScanner([base]).scan_with_stats(sub)
    assert len(records) == 1


# -- erros e concorrência simulada ----------------------------------------------------

class _FakeScandir:
    """Context manager + iterável imitando o retorno de os.scandir."""

    def __init__(self, entries: list) -> None:
        self._entries = entries

    def __enter__(self) -> _FakeScandir:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._entries)


class _PhantomEntry:
    """Entrada que some antes do lstat (TOCTOU): stat levanta FileNotFoundError."""

    name = "fantasma.txt"
    path = "/tmp/synth/fantasma.txt"

    def is_symlink(self) -> bool:
        return False

    def stat(self, *, follow_symlinks: bool = False) -> os.stat_result:
        raise FileNotFoundError("arquivo removido em corrida (simulado)")


def test_scan_permissao_negada_num_subdir_nao_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_root(tmp_path)
    (root / "visivel.txt").write_bytes(b"ok")
    blocked = root / "bloqueado"
    blocked.mkdir()
    (blocked / "dentro.txt").write_bytes(b"secreto")

    real_scandir = os.scandir

    def fake_scandir(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if os.path.abspath(os.fspath(path)) == os.path.abspath(os.fspath(blocked)):
            raise PermissionError("EACCES simulado (monkeypatch)")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    records, stats = SafeScanner([root]).scan_with_stats(root)

    assert [Path(r.path).name for r in records] == ["visivel.txt"]
    assert stats.skipped_errors >= 1
    assert stats.truncated is False


def test_scan_arquivo_some_durante_scan_nao_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: FileNotFoundError entre listagem e lstat conta erro, não crash."""
    root = _make_root(tmp_path)
    (root / "estavel.txt").write_bytes(b"ok")

    real_scandir = os.scandir
    abs_root = os.path.abspath(os.fspath(root))
    injected = False

    def fake_scandir(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal injected
        it = real_scandir(path, *args, **kwargs)
        if os.path.abspath(os.fspath(path)) == abs_root and not injected:
            injected = True
            with it:
                entries = list(it)
            entries.append(_PhantomEntry())
            return _FakeScandir(entries)
        return it

    monkeypatch.setattr(os, "scandir", fake_scandir)

    records, stats = SafeScanner([root]).scan_with_stats(root)

    assert [Path(r.path).name for r in records] == ["estavel.txt"]
    assert stats.skipped_errors == 1
