"""Compatibility wrapper for academy context merge helpers."""
from __future__ import annotations

from ..intelligence.academy_context import (
    KnownStudent,
    MergeItem,
    MergeResult,
    build_report_contexts,
)

__all__ = [
    "KnownStudent",
    "MergeItem",
    "MergeResult",
    "build_report_contexts",
]
