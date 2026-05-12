from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OutgoingMessage:
    student_classin_id: str
    student_name: str
    parent_phone: str | None
    message: str
