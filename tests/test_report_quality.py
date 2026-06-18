from __future__ import annotations

from classin_toolkit.intelligence.report_quality import evaluate_weekly_report_quality


def test_weekly_report_quality_ready_when_specific_safe_and_actionable() -> None:
    result = evaluate_weekly_report_quality(
        student_name="김지각",
        summary_markdown=(
            "김지각 학생은 이번 주 출석 2회 중 지각 1회가 있었고, 숙제 제출률은 50%입니다. "
            "오프라인 자료의 단원평가 68점과 상담 메모를 함께 보면 등원 루틴 회복이 우선입니다. "
            "다음 주에는 보강 1회와 숙제 확인 루틴을 권장합니다."
        ),
        parent_message=(
            "김지각 학생은 이번 주 지각과 숙제 누락이 겹쳤습니다. "
            "학원에서도 보강과 숙제 확인 루틴을 함께 잡아보겠습니다."
        ),
        lessons=[{"attendance": "지각", "homework_submitted": False}],
        exam_results=[{"score": 68, "exam_name": "단원평가"}],
        academy_context={
            "has_context": True,
            "sources": [{"kind": "offline_score", "detail": "단원평가 68점"}],
        },
    )

    assert result.status == "ready"
    assert result.score == 100
    assert result.warnings == []


def test_weekly_report_quality_blocks_empty_or_unsafe_parent_copy() -> None:
    result = evaluate_weekly_report_quality(
        student_name="이하락",
        summary_markdown="이하락 학생은 문제아입니다.",
        parent_message="게으른 태도가 심합니다.",
        lessons=[{"attendance": "결석", "homework_submitted": False}],
    )

    assert result.status == "blocked"
    assert result.score < 70
    assert any("낙인 표현" in warning for warning in result.warnings)
