from __future__ import annotations

from types import SimpleNamespace

from classin_toolkit.intelligence.report_composition import compose_individual_report


def test_report_composition_ready_with_lessons_exam_and_context() -> None:
    result = compose_individual_report(
        student=SimpleNamespace(classin_id="10001", name="홍길동", class_name="고2-A"),
        lessons=[
            {"attendance": "출석", "homework_submitted": True, "homework_score": 95},
            {"attendance": "출석", "homework_submitted": True, "homework_score": 90},
        ],
        exam_results=[{"exam_name": "단원평가", "score": 92, "max_score": 100}],
        academy_context={
            "has_context": True,
            "offline_scores": 1,
            "memos": 1,
            "summary": "오프라인 시험 1건 · 상담 메모 1건",
            "sources": [
                {"kind": "offline_score", "detail": "단원평가 92점"},
                {"kind": "memo", "detail": "심화 과제 선호"},
            ],
        },
        latest_notification={"status": "dry_run", "quality_status": "ready"},
    )

    body = result.as_dict()

    assert body["readiness_status"] == "ready"
    assert body["focus"] in {"상담 맥락 반영", "안정 루틴 유지"}
    assert body["source_counts"] == {
        "classin_lessons": 2,
        "exam_results": 1,
        "offline_attendance": 0,
        "offline_scores": 1,
        "memos": 1,
        "weekly_reports": 0,
        "notifications": 1,
    }
    assert [section["id"] for section in body["sections"]] == [
        "one_line",
        "attendance_routine",
        "homework_attitude",
        "exam_signal",
        "memo_context",
        "next_action",
        "parent_message",
        "quality_gate",
    ]


def test_report_composition_blocks_without_classin_lessons() -> None:
    result = compose_individual_report(
        student={"classin_id": "10002", "name": "김영희", "class_name": "고2-B"},
        lessons=[],
        academy_context={},
    )

    assert result.readiness_status == "blocked"
    assert result.readiness_score <= 55
    assert "ClassIn 수업 기록" in result.readiness_warnings[0]
