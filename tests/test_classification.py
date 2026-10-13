"""Testes da Sprint 02 — classificação determinística.

TODOS sintéticos: só `:memory:`/`tmp_path`, nenhum I/O pessoal,
nenhum `sudo`, nenhum serviço, nenhum LLM, nenhuma movimentação real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from hexavante.models.entities import ClassificationResult, FileRecord, Rule
from hexavante.services.classifier import (
    DeterministicClassifier,
    DeterministicRuleEngine,
)
from hexavante.services.interfaces import Classifier, RuleEngine
from hexavante.services.rule_store import SqliteRuleStore


def _file(path: str, mime: str = "") -> FileRecord:
    return FileRecord(path=path, size_bytes=10, mtime_ns=1, mime_type=mime)


def _requires_review(res: ClassificationResult) -> bool:
    """Baixa confiança exige revisão, nunca movimentação (Sprint 02)."""
    return res.destination is None or res.confidence < 0.5


# -- Classifier: extensões conhecidas -----------------------------------

def test_classifier_extensoes_conhecidas() -> None:
    clf = DeterministicClassifier()
    cases = [
        ("/tmp/synth/rel.pdf", "application/pdf", "Documentos"),
        ("/tmp/synth/foto.jpg", "image/jpeg", "Imagens"),
        ("/tmp/synth/foto.png", "image/png", "Imagens"),
        ("/tmp/synth/musica.mp3", "audio/mpeg", "Áudio"),
        ("/tmp/synth/filme.mp4", "video/mp4", "Vídeos"),
        ("/tmp/synth/pacote.zip", "application/zip", "Arquivos"),
        ("/tmp/synth/pacote.tar.gz", "application/gzip", "Arquivos"),
        ("/tmp/synth/script.py", "text/x-python", "Código"),
        ("/tmp/synth/app.js", "text/javascript", "Código"),
        ("/tmp/synth/main.rs", "text/plain", "Código"),
        ("/tmp/synth/nota.txt", "text/plain", "Documentos"),
        ("/tmp/synth/nota.md", "text/markdown", "Documentos"),
    ]
    for path, mime, expected_dest in cases:
        res = clf.classify(_file(path, mime))
        assert res.destination == expected_dest, path
        assert res.confidence == 0.9, path
        assert res.rule_id is not None, path
        assert res.reason, path
        assert res.file_path == path


def test_classifier_maiusculas_case_insensitive() -> None:
    clf = DeterministicClassifier()
    res_pdf = clf.classify(_file("/tmp/synth/RELATORIO.PDF", "application/pdf"))
    assert res_pdf.destination == "Documentos"
    assert res_pdf.confidence == 0.9
    res_jpg = clf.classify(_file("/tmp/synth/FOTO.JPG", "image/jpeg"))
    assert res_jpg.destination == "Imagens"
    assert res_jpg.confidence == 0.9
    res_tar = clf.classify(_file("/tmp/synth/BACKUP.TAR.GZ", "application/gzip"))
    assert res_tar.destination == "Arquivos"
    assert res_tar.confidence == 0.9


def test_classifier_desconhecida_retorna_none_zero() -> None:
    clf = DeterministicClassifier()
    res = clf.classify(_file("/tmp/synth/arquivo.zzz9desconhecido", "application/octet-stream"))
    assert res.destination is None
    assert res.confidence == 0.0
    assert res.rule_id is None
    assert res.reason != ""
    # Sem extensão também é desconhecido.
    res2 = clf.classify(_file("/tmp/synth/semextensao", "application/octet-stream"))
    assert res2.destination is None
    assert res2.confidence == 0.0


def test_classifier_mime_fallback() -> None:
    clf = DeterministicClassifier()
    res_img = clf.classify(_file("/tmp/synth/estranho.xyz1", "image/png"))
    assert res_img.destination == "Imagens"
    assert res_img.confidence == 0.5
    res_audio = clf.classify(_file("/tmp/synth/estranho.xyz2", "audio/mpeg"))
    assert res_audio.destination == "Áudio"
    assert res_audio.confidence == 0.5
    res_video = clf.classify(_file("/tmp/synth/estranho.xyz3", "video/mp4"))
    assert res_video.destination == "Vídeos"
    assert res_video.confidence == 0.5
    res_text = clf.classify(_file("/tmp/synth/estranho.xyz4", "text/plain"))
    assert res_text.destination == "Documentos"
    assert res_text.confidence == 0.5


def test_classifier_reason_sempre_ptbr_nao_vazio() -> None:
    clf = DeterministicClassifier()
    samples = [
        _file("/tmp/synth/a.pdf", "application/pdf"),
        _file("/tmp/synth/b.xyz9", "application/octet-stream"),
        _file("/tmp/synth/c.xyz9", "image/jpeg"),
    ]
    for f in samples:
        res = clf.classify(f)
        assert isinstance(res.reason, str)
        assert res.reason.strip() != ""


def test_classifier_determinismo_duas_chamadas_iguais() -> None:
    clf = DeterministicClassifier()
    f = _file("/tmp/synth/det.pdf", "application/pdf")
    r1 = clf.classify(f)
    r2 = clf.classify(f)
    assert r1 == r2
    f2 = _file("/tmp/synth/det2.xyz9", "application/octet-stream")
    assert clf.classify(f2) == clf.classify(f2)


def test_classifier_nao_infere_conteudo() -> None:
    clf = DeterministicClassifier()
    # PDF é Documentos, nunca "Livro/livro".
    res_pdf = clf.classify(_file("/tmp/synth/livro.pdf", "application/pdf"))
    assert res_pdf.destination == "Documentos"
    assert "livro" not in (res_pdf.destination or "").lower()
    # ZIP é Arquivos, nunca Código (mesmo com MIME de código no registro).
    res_zip = clf.classify(_file("/tmp/synth/pacote.zip", "text/x-python"))
    assert res_zip.destination == "Arquivos"
    assert res_zip.destination != "Código"
    # Extensão vence MIME divergente (só extensão/MIME contam, sem conteúdo).
    res_div = clf.classify(_file("/tmp/synth/doc.pdf", "image/png"))
    assert res_div.destination == "Documentos"
    assert res_div.confidence == 0.9


def test_classifier_conforma_ao_protocol() -> None:
    assert isinstance(DeterministicClassifier(), Classifier)


def test_classifier_allowed_destinations_penaliza(tmp_path: Path) -> None:
    clf = DeterministicClassifier(allowed_destinations=["Imagens"])
    # Fora da lista: 0.9 -> 0.45 + aviso.
    res_fora = clf.classify(_file("/tmp/synth/a.pdf", "application/pdf"))
    assert res_fora.destination == "Documentos"
    assert res_fora.confidence == pytest.approx(0.45)
    assert "não autorizado" in res_fora.reason.lower()
    # Dentro da lista: sem penalidade.
    res_dentro = clf.classify(_file("/tmp/synth/foto.jpg", "image/jpeg"))
    assert res_dentro.confidence == 0.9
    assert "não autorizado" not in res_dentro.reason.lower()
    # Override por chamada também funciona.
    clf2 = DeterministicClassifier()
    res_over = clf2.classify(
        _file("/tmp/synth/a.pdf", "application/pdf"),
        allowed_destinations=["Documentos"],
    )
    assert res_over.confidence == 0.9


# -- RuleEngine ----------------------------------------------------------

def test_rule_engine_precedencia_priority() -> None:
    eng = DeterministicRuleEngine()
    low = Rule(rule_id="r-low", name="baixa", pattern="*.pdf", destination="/tmp/synth/a", priority=1)
    high = Rule(rule_id="r-high", name="alta", pattern="*.pdf", destination="/tmp/synth/b", priority=10)
    res = eng.suggest(_file("/tmp/synth/x.pdf"), [low, high])
    assert res.destination == "/tmp/synth/b"
    assert res.rule_id == "r-high"
    assert res.confidence == 0.9
    # Ordem de entrada invertida não muda o vencedor (determinístico).
    res2 = eng.suggest(_file("/tmp/synth/x.pdf"), [high, low])
    assert res2 == res


def test_rule_engine_empate_resolve_por_menor_rule_id() -> None:
    eng = DeterministicRuleEngine()
    rb = Rule(rule_id="r-b", name="B", pattern="*.pdf", destination="/tmp/synth/b", priority=5)
    ra = Rule(rule_id="r-a", name="A", pattern="*.pdf", destination="/tmp/synth/a", priority=5)
    res = eng.suggest(_file("/tmp/synth/x.pdf"), [rb, ra])
    assert res.rule_id == "r-a"
    assert res.destination == "/tmp/synth/a"
    # Repetível.
    assert eng.suggest(_file("/tmp/synth/x.pdf"), [ra, rb]) == res


def test_rule_engine_disabled_ignorada() -> None:
    eng = DeterministicRuleEngine()
    off = Rule(rule_id="r-off", name="desligada", pattern="*.pdf", destination="/tmp/synth/off", priority=100, enabled=False)
    on = Rule(rule_id="r-on", name="ligada", pattern="*.pdf", destination="/tmp/synth/on", priority=1, enabled=True)
    res = eng.suggest(_file("/tmp/synth/x.pdf"), [off, on])
    assert res.rule_id == "r-on"
    # Só desligadas -> sem match.
    res2 = eng.suggest(_file("/tmp/synth/x.pdf"), [off])
    assert res2.destination is None
    assert res2.confidence == 0.0


def test_rule_engine_glob_estrela() -> None:
    eng = DeterministicRuleEngine()
    rule = Rule(rule_id="r1", name="pdfs", pattern="*.pdf", destination="/tmp/synth/docs", priority=1)
    assert eng.suggest(_file("/tmp/synth/rel.pdf"), [rule]).destination == "/tmp/synth/docs"
    nomatch = eng.suggest(_file("/tmp/synth/rel.txt"), [rule])
    assert nomatch.destination is None
    assert nomatch.confidence == 0.0


def test_rule_engine_substring_no_path() -> None:
    eng = DeterministicRuleEngine()
    rule = Rule(rule_id="r1", name="relatorios", pattern="relatorio", destination="/tmp/synth/docs", priority=1)
    # fnmatch("relatorio_2024.pdf", "relatorio") seria False; substring casa.
    res = eng.suggest(_file("/tmp/synth/relatorio_2024.pdf"), [rule])
    assert res.destination == "/tmp/synth/docs"
    assert res.rule_id == "r1"


def test_rule_engine_sem_match_requer_revisao() -> None:
    eng = DeterministicRuleEngine()
    rule = Rule(rule_id="r1", name="pdfs", pattern="*.pdf", destination="/tmp/synth/docs")
    res = eng.suggest(_file("/tmp/synth/foto.jpg", "image/jpeg"), [rule])
    assert res.destination is None
    assert res.rule_id is None
    assert res.confidence == 0.0
    assert res.reason == "sem regra correspondente; requer revisão"
    # Lista vazia também.
    res2 = eng.suggest(_file("/tmp/synth/x.pdf"), [])
    assert res2.reason == "sem regra correspondente; requer revisão"


def test_rule_engine_allowed_penaliza() -> None:
    eng = DeterministicRuleEngine(allowed_destinations=["/tmp/synth/autorizado"])
    rule = Rule(rule_id="r1", name="pdfs", pattern="*.pdf", destination="/tmp/synth/outro", priority=1)
    res = eng.suggest(_file("/tmp/synth/a.pdf"), [rule])
    assert res.confidence == pytest.approx(0.45)
    assert "não autorizado" in res.reason.lower()
    # Destino permitido não penaliza.
    eng2 = DeterministicRuleEngine(allowed_destinations=["/tmp/synth/docs"])
    rule2 = Rule(rule_id="r1", name="pdfs", pattern="*.pdf", destination="/tmp/synth/docs")
    res2 = eng2.suggest(_file("/tmp/synth/a.pdf"), [rule2])
    assert res2.confidence == 0.9


def test_rule_engine_list_rules_ordenado_e_deterministico() -> None:
    r1 = Rule(rule_id="r-b", name="B", pattern="*.txt", destination="/tmp/synth/b", priority=5)
    r2 = Rule(rule_id="r-a", name="A", pattern="*.pdf", destination="/tmp/synth/a", priority=5)
    r3 = Rule(rule_id="r-c", name="C", pattern="*", destination="/tmp/synth/c", priority=10)
    eng = DeterministicRuleEngine(rules=[r1, r2, r3])
    listed = list(eng.list_rules())
    assert [r.rule_id for r in listed] == ["r-c", "r-a", "r-b"]
    # suggest determinístico em 2x.
    f = _file("/tmp/synth/x.pdf")
    assert eng.suggest(f, [r1, r2, r3]) == eng.suggest(f, [r3, r2, r1])


def test_rule_engine_conforma_ao_protocol() -> None:
    assert isinstance(DeterministicRuleEngine(), RuleEngine)


# -- RuleStore -----------------------------------------------------------

def test_rule_store_save_load_roundtrip_memory() -> None:
    store = SqliteRuleStore(":memory:")
    try:
        rule = Rule(rule_id="r1", name="PDFs", pattern="*.pdf", destination="/tmp/synth/docs", priority=5)
        store.save(rule)
        got = store.get("r1")
        assert got == rule
        loaded = store.load("r1")
        assert loaded == rule
        assert store.count() == 1
    finally:
        store.close()


def test_rule_store_upsert() -> None:
    store = SqliteRuleStore(":memory:")
    try:
        store.save(Rule(rule_id="r1", name="v1", pattern="*.pdf", destination="/tmp/synth/a", priority=1))
        store.save(Rule(rule_id="r1", name="v2", pattern="*.pdf", destination="/tmp/synth/b", priority=9))
        assert store.count() == 1
        got = store.get("r1")
        assert got is not None
        assert got.destination == "/tmp/synth/b"
        assert got.priority == 9
        assert got.name == "v2"
    finally:
        store.close()


def test_rule_store_ordenacao_priority_desc() -> None:
    store = SqliteRuleStore(":memory:")
    try:
        store.save(Rule(rule_id="r-low", name="low", pattern="*.txt", destination="/tmp/synth/a", priority=1))
        store.save(Rule(rule_id="r-high", name="high", pattern="*.pdf", destination="/tmp/synth/b", priority=10))
        store.save(Rule(rule_id="r-mid", name="mid", pattern="*.jpg", destination="/tmp/synth/c", priority=5))
        ids = [r.rule_id for r in store.list_rules()]
        assert ids == ["r-high", "r-mid", "r-low"]
        # Empate: menor rule_id primeiro.
        store.save(Rule(rule_id="r-a", name="A", pattern="x", destination="/tmp/synth/a", priority=5))
        store.save(Rule(rule_id="r-b", name="B", pattern="y", destination="/tmp/synth/b", priority=5))
        ids2 = [r.rule_id for r in store.list_rules() if r.priority == 5]
        assert ids2 == sorted(ids2)
    finally:
        store.close()


def test_rule_store_disabled_persistido() -> None:
    store = SqliteRuleStore(":memory:")
    try:
        store.save(Rule(rule_id="r-off", name="off", pattern="*.pdf", destination="/tmp/synth/docs", enabled=False))
        got = store.get("r-off")
        assert got is not None
        assert got.enabled is False
        # Recarrega via nova lista: persiste.
        assert store.list_rules()[0].enabled is False
    finally:
        store.close()


def test_rule_store_arquivo_tmp_path_e_validacao(tmp_path: Path) -> None:
    target = tmp_path / "rules.db"
    store = SqliteRuleStore(target)
    try:
        store.save(Rule(rule_id="r1", name="n", pattern="*.pdf", destination="/tmp/synth/docs"))
        assert target.exists()
        # Store valida strings só-espaço (Rule permite, store recusa).
        with pytest.raises(ValueError):
            store.save(Rule(rule_id="r-bad", name="bad", pattern="*.pdf", destination="   "))
        with pytest.raises(ValueError):
            store.save(Rule(rule_id="r-bad2", name="bad", pattern="   ", destination="/tmp/synth/docs"))
    finally:
        store.close()


def test_rule_store_nao_cria_pasta_no_fs(tmp_path: Path) -> None:
    store = SqliteRuleStore(":memory:")
    try:
        dest = str(tmp_path / "dest-inexistente-xyz" / "sub")
        assert not Path(dest).exists()
        store.save(Rule(rule_id="r1", name="n", pattern="*.pdf", destination=dest))
        # Persistiu mas NÃO criou diretório.
        assert not Path(dest).exists()
        assert not (tmp_path / "dest-inexistente-xyz").exists()
    finally:
        store.close()


# -- Conflitos / incerteza: revisão, nunca mover --------------------------

def test_baixa_confianca_exige_revisao_nunca_mover() -> None:
    clf = DeterministicClassifier()
    eng = DeterministicRuleEngine()
    desconhecido = clf.classify(_file("/tmp/synth/q.zzz9", "application/octet-stream"))
    assert _requires_review(desconhecido)
    assert desconhecido.destination is None or desconhecido.confidence < 0.5
    sem_regra = eng.suggest(_file("/tmp/synth/foto.jpg"), [])
    assert _requires_review(sem_regra)
    # Penalizado cai para revisão mesmo com match.
    eng_pen = DeterministicRuleEngine(allowed_destinations=["/tmp/synth/autorizado"])
    rule = Rule(rule_id="r1", name="n", pattern="*.pdf", destination="/tmp/synth/outro")
    pen = eng_pen.suggest(_file("/tmp/synth/a.pdf"), [rule])
    assert pen.confidence < 0.5
    assert _requires_review(pen)
    # Alta confiança conhecida não exige revisão (mas ainda é só sugestão).
    alta = clf.classify(_file("/tmp/synth/a.pdf", "application/pdf"))
    assert not _requires_review(alta)


def test_conflito_ordem_entrada_nao_altera_vencedor() -> None:
    eng = DeterministicRuleEngine()
    alta = Rule(rule_id="r-alta", name="alta", pattern="*.pdf", destination="/tmp/synth/alta", priority=10)
    baixa = Rule(rule_id="r-baixa", name="baixa", pattern="*", destination="/tmp/synth/baixa", priority=1)
    f = _file("/tmp/synth/doc.pdf")
    assert eng.suggest(f, [baixa, alta]).rule_id == "r-alta"
    assert eng.suggest(f, [alta, baixa]).rule_id == "r-alta"


def test_classify_suggest_nao_escrevem_no_fs(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.pdf").write_bytes(b"%PDF fake")
    before = sorted(p.name for p in root.iterdir())
    clf = DeterministicClassifier()
    eng = DeterministicRuleEngine()
    clf.classify(_file(str(root / "a.pdf"), "application/pdf"))
    eng.suggest(
        _file(str(root / "a.pdf"), "application/pdf"),
        [Rule(rule_id="r1", name="n", pattern="*.pdf", destination=str(root / "dest"))],
    )
    after = sorted(p.name for p in root.iterdir())
    assert before == after == ["a.pdf"]
    assert not (root / "dest").exists()
