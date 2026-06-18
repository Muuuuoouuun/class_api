from classin_toolkit.intelligence.notification_quality import (
    evaluate_missing_homework_message,
)


def test_missing_homework_message_quality_ready_when_actionable_and_safe() -> None:
    result = evaluate_missing_homework_message(
        student_name="김지각",
        parent_phone="01012345678",
        message="김지각 학생의 오늘 숙제 미제출이 확인되어 안내드립니다. 오늘 중 제출 완료 부탁드립니다.",
        missing_rows=[{"lesson_classin_id": "lesson-1"}],
    )

    assert result.status == "ready"
    assert result.score == 100
    assert result.warnings == []


def test_missing_homework_message_quality_blocks_missing_phone_or_unsafe_tone() -> None:
    result = evaluate_missing_homework_message(
        student_name="이하락",
        parent_phone="",
        message="이하락 학생은 게으른 상태입니다.",
        missing_rows=[{"lesson_classin_id": "lesson-1"}],
    )

    assert result.status == "blocked"
    assert result.score < 70
    assert any("연락처" in warning for warning in result.warnings)
    assert any("표현 안전" in warning for warning in result.warnings)
