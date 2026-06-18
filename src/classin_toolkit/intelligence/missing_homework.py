"""미제출자 학생별 카톡 문구 생성 (Layer 3).

입력: Notion 수업 기록 DB 에서 추려낸 `rows` (student_classin_id 기준 grouping).
출력: 학생별 OutgoingMessage 리스트. 실제 발송은 notify.dispatcher.
"""
from __future__ import annotations

import json

from ..config import AppConfig
from ..notify.message import OutgoingMessage
from ..storage.notion_repo import StudentRecord
from .claude_client import load_prompt, run_json
from .notification_quality import evaluate_missing_homework_message


def compose_messages_from_rows(
    *,
    cfg: AppConfig,
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
                "missing_lessons": [
                    {
                        "date": r.get("date"),
                        "lesson_id": r.get("lesson_classin_id"),
                    }
                    for r in rows
                ],
            }
        )

    system = load_prompt("missing_homework")
    user = (
        f"학원: {cfg.academy.name}\n"
        f"미제출 학생 목록 (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    data = run_json(cfg, system=system, user=user)
    if not isinstance(data, list):
        raise ValueError("missing_homework prompt must return a JSON array")

    by_id: dict[str, str] = {
        str(x.get("student_classin_id")): x.get("message", "") for x in data
    }

    out: list[OutgoingMessage] = []
    for cid in by_student:
        rec = students_lookup.get(cid)
        message = by_id.get(cid, "")
        quality = evaluate_missing_homework_message(
            student_name=rec.name if rec else "",
            parent_phone=rec.parent_phone if rec else None,
            message=message,
            missing_rows=by_student[cid],
        )
        out.append(
            OutgoingMessage(
                student_classin_id=cid,
                student_name=rec.name if rec else "",
                parent_phone=rec.parent_phone if rec else None,
                message=message,
                quality_status=quality.status,
                quality_score=quality.score,
                quality_warnings=quality.warnings,
            )
        )
    return out
