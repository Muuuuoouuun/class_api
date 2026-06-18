from __future__ import annotations

from classin_toolkit.intelligence.action_queue import build_teacher_action_queue


def test_teacher_action_queue_combines_missing_data_and_report_items() -> None:
    queue = build_teacher_action_queue(
        missing_items=[
            {
                "selection_key": "10001::lesson-1::2026-04-24",
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "class_name": "고2-A",
                "action_required": "needs_message",
                "missing_count": 2,
                "lesson_classin_id": "lesson-1",
                "report_context": {"badges": ["상담 메모 1건"]},
                "notification_quality_status": "ready",
            }
        ],
        data_review_items=[
            {
                "kind": "offline_score",
                "student_name": "동명이인",
                "class_name": "고2-A",
                "source": "scores/april.xlsx",
                "reason": "학생 자동 매칭 필요",
            }
        ],
        weekly_draft_items=[
            {
                "student_classin_id": "10003",
                "student_name": "이하락",
                "quality_status": "blocked",
                "quality_score": 41,
                "quality_warnings": ["다음 액션 부족"],
                "report_context_summary": "성적 하락 · 상담 메모 2건",
                "approved": False,
            }
        ],
        report_composition_items=[
            {
                "student_classin_id": "10004",
                "student_name": "정활발",
                "class_name": "고2-A",
                "readiness_status": "review",
                "readiness_warnings": ["성적·시험 신호: 이번 주 시험 데이터가 없습니다."],
                "focus": "안정 루틴 유지",
                "context_summary": "",
                "source_counts": {"classin_lessons": 2, "exam_results": 0, "offline_scores": 0, "memos": 0},
            }
        ],
    )

    assert [item["kind"] for item in queue] == [
        "weekly_report",
        "missing_homework",
        "data_review",
        "report_composition",
    ]
    assert queue[0]["student_name"] == "이하락"
    assert queue[0]["next_action"] == "리포트 수정"
    assert queue[0]["execution_state"] == "review_required"
    assert queue[0]["safety_gate"] == "다음 액션 부족"
    assert "품질 경고" in queue[0]["completion_check"]
    assert queue[1]["reason"] == "숙제 미제출 연락 필요 · 2회째"
    assert queue[1]["can_execute"] is True
    assert queue[1]["execution_state"] == "ready"
    assert "발송 모드" in queue[1]["safety_gate"]
    assert "알림 기록" in queue[1]["completion_check"]
    assert queue[2]["blocking_reason"] == "자동 매칭 확인 필요"
    assert "리포트 문장에 반영 금지" in queue[2]["safety_gate"]
    assert queue[3]["next_action"] == "근거 보강"
    assert "초안 생성 전 보강" in queue[3]["safety_gate"]


def test_teacher_action_queue_lifts_blocked_notification_copy_first() -> None:
    queue = build_teacher_action_queue(
        missing_items=[
            {
                "selection_key": "10001::lesson-1",
                "student_name": "홍길동",
                "action_required": "needs_message",
                "notification_quality_status": "blocked",
                "notification_quality_warnings": ["낙인 표현"],
            }
        ],
        data_review_items=[],
    )

    assert queue[0]["kind"] == "missing_homework"
    assert queue[0]["priority"] == 5
    assert queue[0]["next_action"] == "문구 수정"
    assert queue[0]["can_execute"] is False
    assert queue[0]["execution_state"] == "review_required"
    assert queue[0]["operator_note"] == "검토 필요: 알림 문구 품질 blocked"
    assert "낙인 표현" in queue[0]["badges"]
