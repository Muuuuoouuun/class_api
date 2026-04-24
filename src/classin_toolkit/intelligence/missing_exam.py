"""시험 미응시자 학생별 카톡 문구 생성 (Layer 3)."""
from __future__ import annotations

import json

from ..config import AppConfig
from ..notify.message import OutgoingMessage
from ..storage.notion_repo import StudentRecord
from .claude_client import load_prompt, run_json


def compose_messages_from_rows(
    *,
    cfg: AppConfig,
    exam_name: str,
    exam_date: str,
    by_student: dict[str, list[dict]],
    students_lookup: dict[str, StudentRecord],
) -> list[OutgoingMessage]:
    if not by_student:
        return []

    payload = []
    for cid, rows in by_student.items():
        rec = students_lookup.get(cid)
        payload.append(
            {
                "student_classin_id": cid,
                "student_name": rec.name if rec else "",
                "class_name": rec.class_name if rec else None,
                "exam_name": exam_name,
                "exam_date": exam_date,
                "missing_exams": [
                    {
                        "exam_name": row.get("exam_name") or exam_name,
                        "exam_date": row.get("exam_date") or exam_date,
                        "subject": row.get("subject"),
                    }
                    for row in rows
                ],
            }
        )

    system = load_prompt("missing_exam")
    user = (
        f"학원: {cfg.academy.name}\n"
        f"시험 미응시 학생 목록 (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    data = run_json(cfg, system=system, user=user)
    if not isinstance(data, list):
        raise ValueError("missing_exam prompt must return a JSON array")

    by_id: dict[str, str] = {
        str(item.get("student_classin_id")): item.get("message", "") for item in data
    }

    out: list[OutgoingMessage] = []
    for cid in by_student:
        rec = students_lookup.get(cid)
        out.append(
            OutgoingMessage(
                student_classin_id=cid,
                student_name=rec.name if rec else "",
                parent_phone=rec.parent_phone if rec else None,
                message=by_id.get(cid, ""),
            )
        )
    return out
