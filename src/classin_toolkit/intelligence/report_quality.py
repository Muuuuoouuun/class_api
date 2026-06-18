"""Deterministic quality checks for AI-generated weekly reports."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

QualityLevel = Literal["pass", "warn", "fail"]
QualityStatus = Literal["ready", "review", "blocked"]

_EVIDENCE_TERMS = (
    "출석",
    "지각",
    "결석",
    "숙제",
    "시험",
    "점수",
    "손들기",
    "트로피",
    "오프라인",
    "상담",
    "메모",
    "보강",
    "참여",
)
_ACTION_TERMS = (
    "권장",
    "제안",
    "필요",
    "다음",
    "보강",
    "루틴",
    "상담",
    "확인",
    "심화",
    "연습",
)
_CONTEXT_TERMS = ("오프라인", "상담", "메모", "보강", "자료", "성적")
_BANNED_LABELS = ("문제아", "불성실", "게으른", "부진아", "낙오", "답이 없다")


@dataclass(frozen=True)
class ReportQualityCheck:
    code: str
    label: str
    level: QualityLevel
    detail: str


@dataclass(frozen=True)
class ReportQualityResult:
    status: QualityStatus
    score: int
    checks: list[ReportQualityCheck]

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


def evaluate_weekly_report_quality(
    *,
    student_name: str,
    summary_markdown: str,
    parent_message: str,
    lessons: list[dict[str, Any]],
    exam_results: list[dict[str, Any]] | None = None,
    academy_context: dict[str, Any] | None = None,
) -> ReportQualityResult:
    """Return review signals for a weekly draft before parent-facing approval."""

    summary = _compact(summary_markdown)
    parent = _compact(parent_message)
    combined = f"{summary} {parent}"
    checks = [
        _summary_check(summary),
        _parent_message_check(parent),
        _evidence_check(combined, lessons, exam_results or [], academy_context or {}),
        _context_check(combined, academy_context or {}),
        _action_check(combined),
        _safety_check(combined),
        _personalization_check(combined, student_name),
    ]
    failures = sum(1 for check in checks if check.level == "fail")
    warnings = sum(1 for check in checks if check.level == "warn")
    score = max(0, 100 - failures * 25 - warnings * 10)
    status: QualityStatus
    if failures:
        status = "blocked"
    elif warnings:
        status = "review"
    else:
        status = "ready"
    return ReportQualityResult(status=status, score=score, checks=checks)


def _summary_check(summary: str) -> ReportQualityCheck:
    if not summary:
        return ReportQualityCheck("summary_present", "요약", "fail", "본문 요약이 비어 있습니다.")
    if len(summary) < 80:
        return ReportQualityCheck("summary_depth", "요약", "warn", "요약이 너무 짧습니다.")
    return ReportQualityCheck("summary_depth", "요약", "pass", "검토 가능한 분량입니다.")


def _parent_message_check(parent: str) -> ReportQualityCheck:
    if not parent:
        return ReportQualityCheck(
            "parent_message_present",
            "학부모 문구",
            "fail",
            "학부모 발송 문구가 비어 있습니다.",
        )
    if len(parent) < 40:
        return ReportQualityCheck("parent_message_depth", "학부모 문구", "warn", "문구가 너무 짧습니다.")
    if len(parent) > 350:
        return ReportQualityCheck(
            "parent_message_length",
            "학부모 문구",
            "warn",
            "문구가 길어 카톡 발송 전 다듬는 편이 좋습니다.",
        )
    return ReportQualityCheck("parent_message_length", "학부모 문구", "pass", "길이가 적절합니다.")


def _evidence_check(
    text: str,
    lessons: list[dict[str, Any]],
    exam_results: list[dict[str, Any]],
    academy_context: dict[str, Any],
) -> ReportQualityCheck:
    has_input = bool(lessons or exam_results or academy_context.get("has_context"))
    has_evidence = any(term in text for term in _EVIDENCE_TERMS) or bool(re.search(r"\d", text))
    if has_input and not has_evidence:
        return ReportQualityCheck("evidence", "근거", "warn", "입력 데이터가 있지만 리포트에서 근거가 약합니다.")
    return ReportQualityCheck("evidence", "근거", "pass", "데이터 근거 표현이 있습니다.")


def _context_check(text: str, academy_context: dict[str, Any]) -> ReportQualityCheck:
    sources = academy_context.get("sources") or []
    if not sources:
        return ReportQualityCheck("academy_context", "학원 데이터", "pass", "추가 학원 데이터가 없습니다.")
    if any(term in text for term in _CONTEXT_TERMS):
        return ReportQualityCheck("academy_context", "학원 데이터", "pass", "학원 데이터 맥락이 반영됐습니다.")
    return ReportQualityCheck(
        "academy_context",
        "학원 데이터",
        "warn",
        "병합된 학원 데이터가 있지만 본문에서 드러나지 않습니다.",
    )


def _action_check(text: str) -> ReportQualityCheck:
    if any(term in text for term in _ACTION_TERMS):
        return ReportQualityCheck("next_action", "다음 액션", "pass", "다음 액션이 포함됐습니다.")
    return ReportQualityCheck("next_action", "다음 액션", "warn", "보강·상담·연습 등 다음 액션이 약합니다.")


def _safety_check(text: str) -> ReportQualityCheck:
    found = [label for label in _BANNED_LABELS if label in text]
    if found:
        return ReportQualityCheck(
            "tone_safety",
            "표현 안전",
            "fail",
            f"학부모 문구에 부적절한 낙인 표현이 있습니다: {', '.join(found)}",
        )
    return ReportQualityCheck("tone_safety", "표현 안전", "pass", "낙인 표현이 없습니다.")


def _personalization_check(text: str, student_name: str) -> ReportQualityCheck:
    if student_name and student_name in text:
        return ReportQualityCheck("personalization", "개인화", "pass", "학생명이 반영됐습니다.")
    return ReportQualityCheck("personalization", "개인화", "warn", "학생명이 드러나지 않아 일반 문구처럼 보일 수 있습니다.")


def _compact(value: str) -> str:
    return " ".join((value or "").split())
