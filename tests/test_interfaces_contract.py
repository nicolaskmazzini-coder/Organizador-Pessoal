"""Contratos de serviços: fakes conformam a Protocols; approval inválida recusa."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from hexavante.models.entities import (
    ClassificationResult,
    FileRecord,
    MovePlan,
    OperationRecord,
    OperationState,
    Rule,
    Schedule,
    compute_plan_hash,
)
from hexavante.services.interfaces import (
    Approval,
    Classifier,
    DuplicateDetector,
    Monitor,
    MoveService,
    RuleEngine,
    Scanner,
    Scheduler,
)


class FakeScanner:
    def scan(self, root: Path, *, dry_run: bool = True) -> Sequence[FileRecord]:
        # Fake sintético: nunca toca no filesystem real.
        return [FileRecord(path=str(root / "a.pdf"), size_bytes=1)]


class FakeClassifier:
    def classify(self, file: FileRecord) -> ClassificationResult:
        return ClassificationResult(
            file_path=file.path, rule_id="r1", destination="/tmp/synth/dest",
            confidence=0.5, reason="fake",
        )


class FakeRuleEngine:
    def __init__(self) -> None:
        self._rules = [Rule(rule_id="r1", name="fake", pattern="*", destination="/tmp/synth/d")]

    def suggest(self, file: FileRecord, rules: Sequence[Rule]) -> ClassificationResult:
        return ClassificationResult(
            file_path=file.path, rule_id=rules[0].rule_id if rules else None,
            destination=rules[0].destination if rules else None,
            confidence=0.5, reason="fake-engine",
        )

    def list_rules(self) -> Sequence[Rule]:
        return list(self._rules)


class FakeMoveService:
    """MoveService fake que valida approval vinculada ao hash (nunca escreve)."""

    def preview(self, plan: MovePlan) -> MovePlan:
        return plan

    def execute(self, plan: MovePlan, approval: Approval) -> OperationRecord:
        if approval.plan_id != plan.plan_id or approval.plan_hash != plan.plan_hash:
            raise PermissionError("approval inválida ou desvinculada do plano")
        return OperationRecord(
            operation_id="op-fake-1", plan_id=plan.plan_id, plan_hash=plan.plan_hash,
            state=OperationState.APROVADA, approval_token=approval.plan_hash,
        )

    def undo(self, operation: OperationRecord) -> OperationRecord:
        import dataclasses

        return dataclasses.replace(operation, state=OperationState.RECUPERACAO_PENDENTE)


class FakeDuplicates:
    def find_duplicates(
        self, files: Sequence[FileRecord]
    ) -> Sequence[Sequence[FileRecord]]:
        return []


class FakeMonitor:
    def __init__(self) -> None:
        self.started = False

    def start(self, root: Path, *, dry_run: bool = True) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False


class FakeScheduler:
    def next_run(self, schedule: Schedule) -> str | None:
        return None if not schedule.enabled else "2026-01-01T00:00:00Z"

    def run_once(self, schedule: Schedule, *, dry_run: bool = True) -> OperationRecord | None:
        assert dry_run is True, "Scheduler v0.1 opera apenas em dry-run"
        return None


def _sample_plan() -> MovePlan:
    items = (
        ClassificationResult(
            file_path="/tmp/synth/a.pdf", rule_id="r1",
            destination="/tmp/synth/dest", confidence=0.9, reason="fake",
        ),
    )
    return MovePlan(plan_id="p1", items=items, plan_hash=compute_plan_hash("p1", items))


def test_fakes_conformam_aos_protocols() -> None:
    assert isinstance(FakeScanner(), Scanner)
    assert isinstance(FakeClassifier(), Classifier)
    assert isinstance(FakeRuleEngine(), RuleEngine)
    assert isinstance(FakeMoveService(), MoveService)
    assert isinstance(FakeDuplicates(), DuplicateDetector)
    assert isinstance(FakeMonitor(), Monitor)
    assert isinstance(FakeScheduler(), Scheduler)


def test_move_preview_nao_executa_e_retorna_plano() -> None:
    svc = FakeMoveService()
    plan = _sample_plan()
    assert svc.preview(plan) == plan


def test_execute_sem_approval_valida_recusa() -> None:
    import pytest

    svc = FakeMoveService()
    plan = _sample_plan()
    # Approval com hash divergente (plano mudou) deve recusar.
    bad = Approval(plan_id="p1", plan_hash="hash-antigo-divergente")
    with pytest.raises(PermissionError):
        svc.execute(plan, bad)
    # plan_id divergente também recusa.
    bad2 = Approval(plan_id="outro-plano", plan_hash=plan.plan_hash)
    with pytest.raises(PermissionError):
        svc.execute(plan, bad2)
    # Approval válida vinculada passa.
    good = Approval(plan_id=plan.plan_id, plan_hash=plan.plan_hash, approver="tester")
    op = svc.execute(plan, good)
    assert op.state is OperationState.APROVADA


def test_scanner_classifier_rule_engine_retornam_dados() -> None:
    scanner = FakeScanner()
    files = scanner.scan(Path("/tmp/synth-root"))
    assert len(files) == 1
    clf = FakeClassifier()
    res = clf.classify(files[0])
    assert res.file_path == files[0].path
    eng = FakeRuleEngine()
    assert len(eng.list_rules()) == 1


def test_scheduler_so_dry_run() -> None:
    import pytest

    sched = FakeScheduler()
    s = Schedule(schedule_id="s1", name="fake")
    assert sched.next_run(s) is not None
    assert sched.run_once(s, dry_run=True) is None
    with pytest.raises(AssertionError):
        sched.run_once(s, dry_run=False)  # type: ignore[call-arg]
