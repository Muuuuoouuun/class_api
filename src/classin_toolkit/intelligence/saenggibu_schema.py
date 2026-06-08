from __future__ import annotations

from pydantic import BaseModel, Field


class SubjectDetail(BaseModel):
    subject: str
    text: str | None = None          # 파싱 실패 시 None + 플래그 (0점 아님)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    def is_low_confidence(self) -> bool:
        return self.text is None or self.confidence < 0.5


class Attendance(BaseModel):
    absent: int = Field(default=0, ge=0)
    late: int = Field(default=0, ge=0)


class GradeRecord(BaseModel):
    grade: int = Field(ge=1, le=3)
    subject_details: list[SubjectDetail] = Field(default_factory=list)
    creative_activities: list[str] = Field(default_factory=list)  # 창체
    behavior_notes: str | None = None                             # 행특
    attendance: Attendance = Field(default_factory=Attendance)


class StructuredSaenggibu(BaseModel):
    grade_records: list[GradeRecord] = Field(default_factory=list)

    def all_evidence(self) -> list[str]:
        """인용 검증용 — 생기부에 실제로 존재하는 모든 텍스트 조각."""
        out: list[str] = []
        for g in self.grade_records:
            out += [d.text for d in g.subject_details if d.text]
            out += g.creative_activities
            if g.behavior_notes:
                out.append(g.behavior_notes)
        return out
