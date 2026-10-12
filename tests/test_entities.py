"""Entidades puras: construção, imutabilidade, serialização, estados."""

from __future__ import annotations

import dataclasses

import pytest

from hexavante.models.entities import (
    ClassificationResult,
    FileRecord,
    FolderRecord,
    MovePlan,
    OperationRecord,
    OperationState,
    Rule,
    Schedule,
    compute_plan_hash,
)


def _sample_items() -> tuple[ClassificationResult, ...]:
    return (
        ClassificationResult(
            file_path="/tmp/synth/a.pdf",
            rule_id="r-pdf",
            destination="/tmp/synth/dest/docs",
            confidence=0.9,
            reason="extensão .pdf",
        ),
        ClassificationResult(
            file_path="/tmp/synth/b.jpg",
            rule_id="r-img",
            destination="/tmp/synth/dest/img",
            confidence=0.75,
            reason="mime image/jpeg",
        ),
    )


def test_operation_states_completos() -> None:
    assert [s.value for s in OperationState] == [
        "planejada",
        "aprovada",
        "em_andamento",
        "verificada",
        "concluida",
        "falhou",
        "recuperacao_pendente",
    ]
    assert len(OperationState) == 7


def test_construcao_entidades() -> None:
    f = FileRecord(path="/tmp/synth/a.pdf", size_bytes=10, mtime_ns=1, mime_type="application/pdf")
    assert f.path.endswith("a.pdf")
    assert FolderRecord(path="/tmp/synth", is_authorized_root=True).is_authorized_root is True
    r = Rule(rule_id="r1", name="PDFs", pattern="*.pdf", destination="/tmp/synth/docs")
    assert r.enabled is True
    c = ClassificationResult(
        file_path="/tmp/synth/a.pdf", rule_id="r1", destination="/tmp/synth/docs",
        confidence=1.0, reason="regra r1",
    )
    assert c.confidence == 1.0
    items = _sample_items()
    h = compute_plan_hash("p1", items)
    assert len(h) == 64
    plan = MovePlan(plan_id="p1", items=items, plan_hash=h)
    assert plan.dry_run is True
    op = OperationRecord(
        operation_id="o1", plan_id="p1", plan_hash=h, state=OperationState.PLANEJADA
    )
    assert op.state is OperationState.PLANEJADA
    s = Schedule(schedule_id="s1", name="diário")
    assert s.dry_run is True


def test_imutabilidade_frozen() -> None:
    f = FileRecord(path="/tmp/synth/a.pdf")
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.path = "/outro"  # type: ignore[misc]
    r = Rule(rule_id="r1", name="n", pattern="p", destination="d")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.enabled = False  # type: ignore[misc]
    items = _sample_items()
    plan = MovePlan(plan_id="p1", items=items, plan_hash=compute_plan_hash("p1", items))
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.dry_run = False  # type: ignore[misc]
    op = OperationRecord(
        operation_id="o1", plan_id="p1", plan_hash="h", state=OperationState.APROVADA
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        op.state = OperationState.FALHOU  # type: ignore[misc]


def test_serializacao_asdict_roundtrip() -> None:
    items = _sample_items()
    h = compute_plan_hash("p1", items)
    plan = MovePlan(plan_id="p1", items=items, plan_hash=h, created_at="2026-01-01T00:00:00Z")
    d = dataclasses.asdict(plan)
    assert d["plan_id"] == "p1"
    assert d["plan_hash"] == h
    assert len(d["items"]) == 2
    # Reconstrói itens + plano a partir do dict.
    items2 = tuple(ClassificationResult(**it) for it in d["items"])
    plan2 = MovePlan(
        plan_id=d["plan_id"], items=items2, plan_hash=d["plan_hash"],
        dry_run=d["dry_run"], created_at=d["created_at"],
    )
    assert plan2 == plan

    op = OperationRecord(
        operation_id="o1", plan_id="p1", plan_hash=h, state=OperationState.VERIFICADA
    )
    od = dataclasses.asdict(op)
    assert od["state"] == OperationState.VERIFICADA
    op2 = OperationRecord(**od)
    assert op2 == op


def test_validacoes_rejeitam_entrada_invalida() -> None:
    with pytest.raises(ValueError):
        FileRecord(path="")
    with pytest.raises(ValueError):
        FileRecord(path="/x", size_bytes=-1)
    with pytest.raises(ValueError):
        ClassificationResult(
            file_path="/x", rule_id=None, destination=None,
            confidence=1.5, reason="r",
        )
    with pytest.raises(ValueError):
        MovePlan(plan_id="", items=(), plan_hash="h")
    with pytest.raises(ValueError):
        OperationRecord(
            operation_id="o", plan_id="p", plan_hash="h",
            state="planejada",  # type: ignore[arg-type]
        )


def test_compute_plan_hash_vincula_mudanca() -> None:
    items = _sample_items()
    h1 = compute_plan_hash("p1", items)
    alterado = (dataclasses.replace(items[0], destination="/outro"), items[1])
    h2 = compute_plan_hash("p1", alterado)
    assert h1 != h2
