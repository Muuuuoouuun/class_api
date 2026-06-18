"""Deterministic quality checks for parent-facing notification messages."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

QualityLevel = Literal["pass", "warn", "fail"]
QualityStatus = Literal["ready", "review", "blocked"]

_HOMEWORK_TERMS = ("숙제", "과제", "제출", "미제출", "보완")
_ACTION_TERMS = ("확인", "제출", "부탁", "안내", "오늘", "까지", "완료")
_BANNED_TERMS = ("문제아", "불성실", "게으른", "부진아", "낙오", "답이 없다")


@dataclass(frozen=True)
class NotificationQualityCheck:
    code: str
    label: str
    level: QualityLevel
    detail: str


@dataclass(frozen=True)
class NotificationQualityResult:
    status: QualityStatus
    score: int
    checks: list[NotificationQualityCheck]

    @property
    def warnings(self) -> list[str]:
        return [
            f"{check.label}: {check.detail}"
            for check in self.checks
            if check.level in ("warn", "fail")
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "warnings": self.warnings,
            "checks": [asdict(check) for check in self.checks],
        }


def evaluate_missing_homework_message(
    *,
    student_name: str,
    parent_phone: str | None,
    message: str,
    missing_rows: list[dict[str, Any]],
) -> NotificationQualityResult:
    text = _compact(message)
    checks = [
        _message_present_check(text),
        _length_check(text),
        _phone_check(parent_phone),
        _student_name_check(text, student_name),
        _homework_context_check(text, missing_rows),
        _action_check(text),
        _safety_check(text),
    ]
    failures = sum(1 for check in checks if check.level == "fail")
    warnings = sum(1 for check in checks if check.level == "warn")
    score = max(0, 100 - failures * 25 - warnings * 10)
    if failures:
        status: QualityStatus = "blocked"
    elif warnings:
        status = "review"
    else:
        status = "ready"
    return NotificationQualityResult(status=status, score=score, checks=checks)


def _message_present_check(text: str) -> NotificationQualityCheck:
    if not text:
        return NotificationQualityCheck("message_present", "문구", "fail", "발송 문구가 비어 있습니다.")
    return NotificationQualityCheck("message_present", "문구", "pass", "발송 문구가 있습니다.")


def _length_check(text: str) -> NotificationQualityCheck:
    if not text:
        return NotificationQualityCheck("message_length", "길이", "fail", "발송 문구가 비어 있습니다.")
    if len(text) < 25:
        return NotificationQualityCheck("message_length", "길이", "warn", "문구가 너무 짧습니다.")
    if len(text) > 300:
        return NotificationQualityCheck("message_length", "길이", "warn", "카톡 문구로는 길어 다듬는 편이 좋습니다.")
    return NotificationQualityCheck("message_length", "길이", "pass", "길이가 적절합니다.")


def _phone_check(parent_phone: str | None) -> NotificationQualityCheck:
    if not parent_phone:
        return NotificationQualityCheck(
            "parent_phone",
            "연락처",
            "fail",
            "보호자 연락처가 없어 자동 발송할 수 없습니다.",
        )
    return NotificationQualityCheck("parent_phone", "연락처", "pass", "보호자 연락처가 있습니다.")


def _student_name_check(text: str, student_name: str) -> NotificationQualityCheck:
    if student_name and student_name in text:
        return NotificationQualityCheck("student_name", "개인화", "pass", "학생명이 반영됐습니다.")
    return NotificationQualityCheck("student_name", "개인화", "warn", "학생명이 없어 일반 문구처럼 보입니다.")


def _homework_context_check(
    text: str,
    missing_rows: list[dict[str, Any]],
) -> NotificationQualityCheck:
    if missing_rows and not any(term in text for term in _HOMEWORK_TERMS):
        return NotificationQualityCheck("homework_context", "숙제 맥락", "warn", "숙제 미제출 맥락이 약합니다.")
    return NotificationQualityCheck("homework_context", "숙제 맥락", "pass", "숙제 맥락이 포함됐습니다.")


def _action_check(text: str) -> NotificationQualityCheck:
    if any(term in text for term in _ACTION_TERMS):
        return NotificationQualityCheck("next_action", "다음 액션", "pass", "보호자가 할 일이 드러납니다.")
    return NotificationQualityCheck("next_action", "다음 액션", "warn", "제출·확인 등 다음 액션이 약합니다.")


def _safety_check(text: str) -> NotificationQualityCheck:
    found = [term for term in _BANNED_TERMS if term in text]
    if found:
        return NotificationQualityCheck(
            "tone_safety",
            "표현 안전",
            "fail",
            f"학부모 문구에 부적절한 낙인 표현이 있습니다: {', '.join(found)}",
        )
    return NotificationQualityCheck("tone_safety", "표현 안전", "pass", "낙인 표현이 없습니다.")


def _compact(value: str) -> str:
    return " ".join((value or "").split())
