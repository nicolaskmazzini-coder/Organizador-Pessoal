"""Modelos puros — re-exporta entidades sem I/O e sem GTK."""

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

__all__ = [
    "ClassificationResult",
    "FileRecord",
    "FolderRecord",
    "MovePlan",
    "OperationRecord",
    "OperationState",
    "Rule",
    "Schedule",
    "compute_plan_hash",
]
