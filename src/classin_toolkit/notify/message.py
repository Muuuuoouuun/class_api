from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OutgoingMessage:
    student_classin_id: str
    student_name: str
    parent_phone: str | None
    message: str
    quality_status: str = "review"
    quality_score: int = 0
    quality_warnings: list[str] = field(default_factory=list)
