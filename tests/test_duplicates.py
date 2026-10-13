"""Testes do StreamingDuplicateDetector — Sprint 05.

TODOS os testes usam `tmp_path` + fixtures sintéticas. Nenhum toca em
`~` real, nenhum usa `sudo`, nenhum instala serviços, nenhuma escrita
fora de `tmp_path`. O detector nunca apaga/limpa/renomeia (verificado em
`test_nunca_apaga_limpa_ou_move` via snapshot antes/depois).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import hexavante.services.duplicates as dup_mod
from hexavante.models.entities import FileRecord
from hexavante.services.duplicates import (
    StreamingDuplicateDetector,
    hash_file_streaming,
)
from hexavante.services.interfaces import DuplicateDetector


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _rec_for(path: Path, sha: str | None) -> FileRecord:
    st = os.lstat(path)
    return FileRecord(
        path=os.fspath(path),
        size_bytes=st.st_size,
        mtime_ns=st.st_mtime_ns,
        mime_type="application/octet-stream",
        sha256=sha,
        is_stable=False,
    )


# -- agrupamento puro (sha já conhecido, sem I/O) ------------------------------

def test_grupos_iguais_com_sha_pronto(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.bin", b"conteudo-igual-123")
    b = _write(tmp_path / "b.bin", b"conteudo-igual-123")
    digest = _sha(b"conteudo-igual-123")
    files = [_rec_for(a, digest), _rec_for(b, digest)]

    groups = StreamingDuplicateDetector().find_duplicates(files)

    assert len(groups) == 1
    assert sorted(r.path for r in groups[0]) == sorted(r.path for r in files)


def test_arquivos_diferentes_nao_agrupam(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.bin", b"alpha")
    b = _write(tmp_path / "b.bin", b"betaaa")
    files = [_rec_for(a, _sha(b"alpha")), _rec_for(b, _sha(b"betaaa"))]

    assert StreamingDuplicateDetector().find_duplicates(files) == []


def test_lista_vazia_retorna_vazia() -> None:
    assert StreamingDuplicateDetector().find_duplicates([]) == []


def test_singleton_nao_forma_grupo(tmp_path: Path) -> None:
    a = _write(tmp_path / "unico.bin", b"sozinho")
    assert StreamingDuplicateDetector().find_duplicates([_rec_for(a, _sha(b"sozinho"))]) == []


# -- sha None calculado em streaming -------------------------------------------

def test_sha_none_calculado_em_streaming(tmp_path: Path) -> None:
    data = b"x" * 200_000  # > 1 chunk de 64KB: exercita o loop de streaming
    a = _write(tmp_path / "a.bin", data)
    b = _write(tmp_path / "b.bin", data)
    detector = StreamingDuplicateDetector([tmp_path])
    files = [
        FileRecord(path=os.fspath(a), size_bytes=0, sha256=None),
        FileRecord(path=os.fspath(b), size_bytes=0, sha256=None),
    ]

    groups = detector.find_duplicates(files)

    assert len(groups) == 1
    assert len(groups[0]) == 2
    assert {r.sha256 for r in groups[0]} == {_sha(data)}
    assert detector.skipped_changed == []


def test_hash_file_streaming_confere_com_hashlib(tmp_path: Path) -> None:
    target = _write(tmp_path / "alvo.bin", b"abc" * 50_000)
    assert hash_file_streaming(target) == _sha(b"abc" * 50_000)


# -- mudança durante o hash: excluída, sem falso positivo -----------------------

def test_mudanca_durante_hash_excluida(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _write(tmp_path / "a.bin", b"estavel")
    b = _write(tmp_path / "b.bin", b"estavel")
    real_hash = dup_mod.hash_file_streaming

    def mutating_hash(path, **kwargs):  # type: ignore[no-untyped-def]
        # Simula escritor concorrente: altera o arquivo DURANTE a leitura.
        with open(path, "ab") as handle:
            handle.write(b"MUTACAO-CONCORRENTE")
        return real_hash(path, **kwargs)

    monkeypatch.setattr(dup_mod, "hash_file_streaming", mutating_hash)
    detector = StreamingDuplicateDetector([tmp_path])
    files = [
        FileRecord(path=os.fspath(a), size_bytes=0, sha256=None),
        FileRecord(path=os.fspath(b), size_bytes=0, sha256=None),
    ]

    groups = detector.find_duplicates(files)

    # Ambos mudaram durante o hash -> nenhum grupo (sem falso positivo).
    assert groups == []
    assert len(detector.skipped_changed) == 2


# -- symlinks, escopo, grandes: pulados com motivo --------------------------------

def test_symlink_pulado(tmp_path: Path) -> None:
    real = _write(tmp_path / "real.bin", b"dados-reais")
    link = tmp_path / "atalho.bin"
    link.symlink_to(real)
    detector = StreamingDuplicateDetector([tmp_path])
    files = [
        FileRecord(path=os.fspath(real), size_bytes=0, sha256=None),
        FileRecord(path=os.fspath(link), size_bytes=0, sha256=None),
    ]

    groups = detector.find_duplicates(files)

    assert groups == []  # só 1 regular estável restante: sem par
    assert detector.skipped_symlink == [os.fspath(link)]


def test_arquivo_grande_pulado_com_reason(tmp_path: Path) -> None:
    a = _write(tmp_path / "g1.bin", b"G" * 100)
    b = _write(tmp_path / "g2.bin", b"G" * 100)
    detector = StreamingDuplicateDetector([tmp_path], max_read_bytes=16)
    files = [
        FileRecord(path=os.fspath(a), size_bytes=0, sha256=None),
        FileRecord(path=os.fspath(b), size_bytes=0, sha256=None),
    ]

    groups = detector.find_duplicates(files)

    assert groups == []
    assert sorted(detector.skipped_large) == sorted([os.fspath(a), os.fspath(b)])


def test_fora_do_escopo_pulado(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _write(outside / "v.bin", b"segredo")
    detector = StreamingDuplicateDetector([allowed])

    groups = detector.find_duplicates(
        [FileRecord(path=os.fspath(victim), size_bytes=0, sha256=None)]
    )

    assert groups == []
    assert detector.skipped_out_of_scope == [os.fspath(victim)]


# -- segurança: nunca apaga/limpa; determinismo; protocolo ------------------------

def test_nunca_apaga_limpa_ou_renomeia(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.bin", b"mesmo")
    b = _write(tmp_path / "b.bin", b"mesmo")
    sub = tmp_path / "sub"
    sub.mkdir()
    c = _write(sub / "c.bin", b"mesmo")
    before = {
        p.name: p.read_bytes()
        for p in [a, b, c]
    }
    before_listing = sorted(os.fspath(p) for p in tmp_path.rglob("*"))

    detector = StreamingDuplicateDetector([tmp_path])
    groups = detector.find_duplicates(
        [
            FileRecord(path=os.fspath(a), size_bytes=0, sha256=None),
            FileRecord(path=os.fspath(b), size_bytes=0, sha256=None),
            FileRecord(path=os.fspath(c), size_bytes=0, sha256=None),
        ]
    )

    after_listing = sorted(os.fspath(p) for p in tmp_path.rglob("*"))
    assert before_listing == after_listing
    assert {p.name: p.read_bytes() for p in [a, b, c]} == before
    assert len(groups) == 1 and len(groups[0]) == 3


def test_determinismo_ordem_estavel(tmp_path: Path) -> None:
    blobs = [b"blob-1" * 10, b"blob-2" * 10]
    files: list[FileRecord] = []
    for i, blob in enumerate(blobs):
        for j in range(2):
            p = _write(tmp_path / f"f{i}{j}.bin", blob)
            files.append(_rec_for(p, _sha(blob)))
    detector = StreamingDuplicateDetector()

    first = detector.find_duplicates(list(reversed(files)))
    second = detector.find_duplicates(list(files))

    def canonical(groups):  # type: ignore[no-untyped-def]
        return [[(r.path, r.size_bytes, r.sha256) for r in g] for g in groups]

    assert canonical(first) == canonical(second)
    assert all(g == sorted(g) for g in canonical(first))


def test_conforma_ao_protocol_duplicate_detector() -> None:
    assert isinstance(StreamingDuplicateDetector(), DuplicateDetector)
