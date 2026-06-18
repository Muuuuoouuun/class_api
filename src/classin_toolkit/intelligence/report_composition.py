"""Deterministic per-student report composition preflight.

This module does not call an LLM. It turns ClassIn lesson rows, exam rows, and
academy context into a compact checklist of the sections a teacher-facing
individual report can safely cover.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SectionStatus = Literal["ready", "review", "missing"]
ReadinessStatus = Literal["ready", "review", "blocked"]


@dataclass(frozen=True)
class ReportCompositionSection:
    id: str
    title: str
    status: SectionStatus
    summary: str
    evidence: list[str] = field(default_factory=list)
    next_action: str = ""
    sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IndividualReportComposition:
    student_classin_id: str
    student_name: str
    class_name: str
    readiness_status: ReadinessStatus
    readiness_score: int
    readiness_warnings: list[str]
    focus: str
    context_summary: str
    source_counts: dict[str, int]
    sections: list[ReportCompositionSection]

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sections"] = [asdict(section) for section in self.sections]
        return data


def compose_individual_report(
    *,
    student: Any,
    lessons: list[dict[str, Any]],
    exam_results: list[dict[str, Any]] | None = None,
    academy_context: dict[str, Any] | None = None,
    latest_notification: dict[str, Any] | None = None,
) -> IndividualReportComposition:
    """Build a report-section readiness map for one student."""

    exams = exam_results or []
    context = academy_context or {}
    counts = _source_counts(lessons, exams, context, latest_notification)
    stats = _lesson_stats(lessons)
    focus = _focus(stats, exams, context)

    sections = [
        _one_line_section(focus, stats, exams, context),
        _attendance_section(stats, context),
        _homework_section(stats, latest_notification),
        _exam_section(exams, context),
        _memo_section(context),
        _next_action_section(focus, stats, exams, context),
        _parent_message_section(focus, stats, context, latest_notification),
    ]
    status, score, warnings = _readiness(sections, lessons)
    sections.append(_quality_section(status, score, warnings))

    return IndividualReportComposition(
        student_classin_id=_attr(student, "classin_id"),
        student_name=_attr(student, "name") or "미등록",
        class_name=_attr(student, "class_name"),
        readiness_status=status,
        readiness_score=score,
        readiness_warnings=warnings,
        focus=focus,
        context_summary=_clean(context.get("summary")) or _context_summary(counts),
        source_counts=counts,
        sections=sections,
    )


def _source_counts(
    lessons: list[dict[str, Any]],
    exams: list[dict[str, Any]],
    context: dict[str, Any],
    latest_notification: dict[str, Any] | None,
) -> dict[str, int]:
    weekly = context.get("weekly_report") or {}
    return {
        "classin_lessons": len(lessons),
        "exam_results": len(exams),
        "offline_attendance": int(context.get("offline_attendance") or 0),
        "offline_scores": int(context.get("offline_scores") or 0),
        "memos": int(context.get("memos") or 0),
        "weekly_reports": 1 if weekly.get("status") else 0,
        "notifications": 1 if latest_notification else 0,
    }


def _lesson_stats(lessons: list[dict[str, Any]]) -> dict[str, Any]:
    attendance_values = [_clean(row.get("attendance")) for row in lessons if row.get("attendance")]
    known_homework = [row for row in lessons if row.get("homework_submitted") is not None]
    submitted = sum(1 for row in known_homework if row.get("homework_submitted") is True)
    missing = sum(1 for row in known_homework if row.get("homework_submitted") is False)
    late = sum(1 for row in lessons if row.get("homework_late") is True)
    scores = [
        float(row["homework_score"])
        for row in lessons
        if row.get("homework_score") is not None
    ]
    return {
        "lesson_count": len(lessons),
        "attendance_known": len(attendance_values),
        "present": sum(1 for value in attendance_values if value == "출석"),
        "late": sum(1 for value in attendance_values if value == "지각"),
        "absent": sum(1 for value in attendance_values if value == "결석"),
        "homework_known": len(known_homework),
        "homework_submitted": submitted,
        "homework_missing": missing,
        "homework_late": late,
        "homework_rate": submitted / len(known_homework) if known_homework else None,
        "homework_score_avg": sum(scores) / len(scores) if scores else None,
    }


def _focus(stats: dict[str, Any], exams: list[dict[str, Any]], context: dict[str, Any]) -> str:
    exam_avg = _exam_average(exams)
    if stats["lesson_count"] == 0:
        return "근거 수집 필요"
    if stats["absent"] >= 2:
        return "결석·상담 필요"
    if stats["homework_missing"] >= 2 or _is_low_rate(stats.get("homework_rate")):
        return "숙제 루틴 회복"
    if exam_avg is not None and exam_avg < 70:
        return "성적 보완"
    if context.get("memos"):
        return "상담 맥락 반영"
    if stats["late"]:
        return "등원 루틴 조정"
    if stats["lesson_count"] and stats["homework_missing"] == 0:
        return "안정 루틴 유지"
    return "기본 점검"


def _one_line_section(
    focus: str,
    stats: dict[str, Any],
    exams: list[dict[str, Any]],
    context: dict[str, Any],
) -> ReportCompositionSection:
    evidence = []
    if stats["lesson_count"]:
        evidence.append(f"ClassIn 수업 {stats['lesson_count']}회")
    if stats["homework_known"]:
        evidence.append(
            f"숙제 제출 {stats['homework_submitted']}/{stats['homework_known']}회"
        )
    exam_avg = _exam_average(exams)
    if exam_avg is not None:
        evidence.append(f"시험 평균 {exam_avg:.0f}점")
    if context.get("summary"):
        evidence.append(_clean(context.get("summary")))
    return ReportCompositionSection(
        id="one_line",
        title="이번 주 한 줄 판단",
        status="ready" if evidence else "missing",
        summary=focus,
        evidence=evidence[:4],
        next_action="근거가 부족하면 수업/시험/메모 데이터를 먼저 확인하세요." if not evidence else "",
        sources=_sources("ClassIn", context),
    )


def _attendance_section(stats: dict[str, Any], context: dict[str, Any]) -> ReportCompositionSection:
    evidence: list[str] = []
    if stats["attendance_known"]:
        evidence.append(
            f"출석 {stats['present']}회 · 지각 {stats['late']}회 · 결석 {stats['absent']}회"
        )
    if context.get("offline_attendance"):
        evidence.append(f"오프라인 출결 {context['offline_attendance']}건")
    status: SectionStatus = "ready" if evidence else "missing"
    return ReportCompositionSection(
        id="attendance_routine",
        title="출결·수업 루틴",
        status=status,
        summary=evidence[0] if evidence else "출결 근거가 없습니다.",
        evidence=evidence,
        next_action="" if evidence else "ClassIn 수업 기록 또는 오프라인 출결을 확인하세요.",
        sources=_sources("ClassIn 수업 기록", context, kind="offline_attendance"),
    )


def _homework_section(
    stats: dict[str, Any],
    latest_notification: dict[str, Any] | None,
) -> ReportCompositionSection:
    evidence: list[str] = []
    if stats["homework_known"]:
        rate = int((stats["homework_rate"] or 0) * 100)
        evidence.append(
            f"숙제 제출률 {rate}% ({stats['homework_submitted']}/{stats['homework_known']}회)"
        )
    if stats["homework_late"]:
        evidence.append(f"지각 제출 {stats['homework_late']}회")
    if stats["homework_score_avg"] is not None:
        evidence.append(f"숙제 평균 {stats['homework_score_avg']:.0f}점")
    if latest_notification:
        evidence.append(f"최근 알림 {latest_notification.get('status') or '-'}")
    status: SectionStatus = "ready" if stats["homework_known"] else "missing"
    return ReportCompositionSection(
        id="homework_attitude",
        title="숙제·학습 태도",
        status=status,
        summary=evidence[0] if evidence else "숙제 제출 근거가 없습니다.",
        evidence=evidence,
        next_action="" if evidence else "HomeworkSubmit/HomeworkScore 수신 여부를 확인하세요.",
        sources=["ClassIn homework", "알림 이력"] if latest_notification else ["ClassIn homework"],
    )


def _exam_section(
    exams: list[dict[str, Any]],
    context: dict[str, Any],
) -> ReportCompositionSection:
    evidence: list[str] = []
    if exams:
        avg = _exam_average(exams)
        if avg is not None:
            evidence.append(f"ClassIn/Notion 시험 {len(exams)}건 · 평균 {avg:.0f}점")
        else:
            evidence.append(f"ClassIn/Notion 시험 {len(exams)}건")
    if context.get("offline_scores"):
        evidence.append(f"오프라인 시험 {context['offline_scores']}건")
    status: SectionStatus = "ready" if evidence else "review"
    return ReportCompositionSection(
        id="exam_signal",
        title="성적·시험 신호",
        status=status,
        summary=evidence[0] if evidence else "이번 주 시험 데이터가 없습니다.",
        evidence=evidence,
        next_action="" if evidence else "시험이 있었다면 OMR/오프라인 성적 파일을 병합하세요.",
        sources=_sources("시험 DB", context, kind="offline_score"),
    )


def _memo_section(context: dict[str, Any]) -> ReportCompositionSection:
    memo_count = int(context.get("memos") or 0)
    sources = _sources("상담 메모", context, kind="memo")
    return ReportCompositionSection(
        id="memo_context",
        title="상담/메모 맥락",
        status="ready" if memo_count else "review",
        summary=f"상담 메모 {memo_count}건" if memo_count else "상담 메모가 없습니다.",
        evidence=[source for source in sources if source != "상담 메모"][:3],
        next_action="" if memo_count else "학부모 요청이나 교사 관찰이 있으면 메모를 남기세요.",
        sources=sources,
    )


def _next_action_section(
    focus: str,
    stats: dict[str, Any],
    exams: list[dict[str, Any]],
    context: dict[str, Any],
) -> ReportCompositionSection:
    action = _recommended_action(focus, stats, exams, context)
    return ReportCompositionSection(
        id="next_action",
        title="다음 액션",
        status="ready" if stats["lesson_count"] else "review",
        summary=action,
        evidence=[focus],
        next_action=action,
        sources=["리포트 구성 규칙"],
    )


def _parent_message_section(
    focus: str,
    stats: dict[str, Any],
    context: dict[str, Any],
    latest_notification: dict[str, Any] | None,
) -> ReportCompositionSection:
    evidence = [focus]
    if context.get("summary"):
        evidence.append(_clean(context.get("summary")))
    if latest_notification and latest_notification.get("quality_status"):
        evidence.append(f"최근 알림 품질 {latest_notification.get('quality_status')}")
    status: SectionStatus = "ready" if stats["lesson_count"] and evidence else "review"
    return ReportCompositionSection(
        id="parent_message",
        title="학부모 메시지",
        status=status,
        summary="관찰 근거와 다음 액션을 포함한 짧은 문구 생성 가능"
        if status == "ready"
        else "발송 전 근거와 표현 안전 검토가 필요합니다.",
        evidence=evidence,
        next_action="생성 후 품질 점검에서 낙인 표현과 다음 액션을 확인하세요.",
        sources=["ClassIn", "학원 context", "알림 품질 이력"],
    )


def _quality_section(
    status: ReadinessStatus,
    score: int,
    warnings: list[str],
) -> ReportCompositionSection:
    return ReportCompositionSection(
        id="quality_gate",
        title="AI 품질 점검",
        status="ready" if status == "ready" else "review",
        summary=f"{status} · {score}/100",
        evidence=warnings or ["리포트 구성 근거가 충분합니다."],
        next_action="" if not warnings else "경고 항목을 보강한 뒤 드래프트를 생성하세요.",
        sources=["deterministic preflight"],
    )


def _readiness(
    sections: list[ReportCompositionSection],
    lessons: list[dict[str, Any]],
) -> tuple[ReadinessStatus, int, list[str]]:
    warnings: list[str] = []
    if not lessons:
        warnings.append("ClassIn 수업 기록이 없어 리포트 근거가 부족합니다.")
    for section in sections:
        if section.status == "missing":
            warnings.append(f"{section.title}: {section.summary}")
        elif section.status == "review":
            warnings.append(f"{section.title}: {section.summary}")

    missing = sum(1 for section in sections if section.status == "missing")
    review = sum(1 for section in sections if section.status == "review")
    score = max(0, 100 - missing * 18 - review * 7)
    if not lessons:
        return "blocked", min(score, 55), warnings
    if missing or review:
        return "review", score, warnings
    return "ready", score, warnings


def _recommended_action(
    focus: str,
    stats: dict[str, Any],
    exams: list[dict[str, Any]],
    context: dict[str, Any],
) -> str:
    if "결석" in focus:
        return "보호자 상담과 보강 가능 시간을 먼저 확인하세요."
    if "숙제" in focus:
        return "다음 수업 전 숙제 확인 루틴과 보완 과제를 안내하세요."
    if "성적" in focus:
        return "오답 범위와 보강 과제를 묶어 안내하세요."
    if "등원" in focus:
        return "등원 시간을 10분 앞당기는 루틴을 제안하세요."
    if context.get("memos"):
        return "상담 메모를 반영해 학부모 메시지 톤을 조정하세요."
    if stats["lesson_count"] and not exams:
        return "수업 루틴은 유지하고 시험 데이터가 생기면 리포트에 보강하세요."
    return "현재 루틴을 유지하되 다음 주 변화 신호를 확인하세요."


def _exam_average(exams: list[dict[str, Any]]) -> float | None:
    scores = [float(row["score"]) for row in exams if row.get("score") is not None]
    return sum(scores) / len(scores) if scores else None


def _sources(
    default: str,
    context: dict[str, Any],
    *,
    kind: str | None = None,
) -> list[str]:
    sources = [default]
    for source in context.get("sources") or []:
        if kind and source.get("kind") != kind:
            continue
        detail = _clean(source.get("detail"))
        if detail:
            sources.append(detail)
    return sources


def _context_summary(counts: dict[str, int]) -> str:
    parts = []
    if counts["weekly_reports"]:
        parts.append("기존 리포트")
    if counts["offline_attendance"]:
        parts.append(f"오프라인 출결 {counts['offline_attendance']}건")
    if counts["offline_scores"]:
        parts.append(f"오프라인 시험 {counts['offline_scores']}건")
    if counts["memos"]:
        parts.append(f"상담 메모 {counts['memos']}건")
    return " · ".join(parts)


def _is_low_rate(value: float | None) -> bool:
    return value is not None and value < 0.6


def _attr(value: Any, name: str) -> str:
    if isinstance(value, dict):
        return _clean(value.get(name))
    return _clean(getattr(value, name, ""))


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
