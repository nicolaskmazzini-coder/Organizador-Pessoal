"""Contratos de serviços — núcleo puro, sem GTK, sem I/O em destino."""

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

__all__ = [
    "Approval",
    "Classifier",
    "DuplicateDetector",
    "Monitor",
    "MoveService",
    "RuleEngine",
    "Scanner",
    "Scheduler",
]
