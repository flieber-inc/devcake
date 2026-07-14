"""Orchestrator package (docs/04): MissionManager façade + focused modules.

Split from the former god-module for maintainability (ISSUES #36). Public
imports remain `from devcake.domain.orchestrator import MissionManager, ...`.
"""

from ..mapper_service import MapperBusy, MapperService, MapperUnconfigured
from . import markers
from .manager import MissionManager
from .markers import AUDIT_PATH, LEGAL_OUTCOMES

__all__ = [
    "AUDIT_PATH",
    "LEGAL_OUTCOMES",
    "MapperBusy",
    "MapperService",
    "MapperUnconfigured",
    "MissionManager",
    "markers",
]
