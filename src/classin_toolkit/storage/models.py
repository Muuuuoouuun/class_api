from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StudentRecord:
    page_id: str
    classin_id: str
    name: str
    parent_phone: str | None
    class_name: str | None
