"""Teacher-facing action queue rules for the Academy Ops Hub."""
from __future__ import annotations

from typing import Any


def build_teacher_action_queue(
    *,
    missing_items: list[dict[str, Any]],
    data_review_items: list[dict[str, Any]],
    weekly_draft_items: list[dict[str, Any]] | None = None,
    report_composition_items: list[dict[str, Any]] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    queue.extend(_missing_action(item) for item in missing_items)
    queue.extend(_data_review_action(item) for item in data_review_items[:5])
    queue.extend(_weekly_draft_action(item) for item in (weekly_draft_items or []))
    queue.extend(_composition_action(item) for item in (report_composition_items or []))
    queue = [_dedupe_badges(item) for item in queue if item]
    queue.sort(
        key=lambda item: (
            int(item.get("priority") or 99),
            item.get("student_name") or "",
            item.get("reason") or "",
            item.get("id") or "",
        )
    )
    return queue[:limit]


def _missing_action(item: dict[str, Any]) -> dict[str, Any]:
    action = item.get("action_required") or "needs_review"
    priority_by_action = {
        "needs_retry": 10,
        "needs_phone": 20,
        "needs_message": 30,
        "needs_review": 70,
    }
    tone_by_action = {
        "needs_retry": "danger",
        "needs_phone": "danger",
        "needs_message": "warn",
        "needs_review": "info",
    }
    next_action_by_action = {
        "needs_retry": "다시 보내기",
        "needs_phone": "연락처 입력",
        "needs_message": "보호자에게 보내기",
        "needs_review": "상태 확인",
    }
    reason_by_action = {
        "needs_retry": "지난 발송 실패",
        "needs_phone": "보호자 연락처 없음",
        "needs_message": "숙제 미제출 연락 필요",
        "needs_review": "발송 후 확인",
    }
    reason = reason_by_action.get(action, "상태 확인")
    missing_count = int(item.get("missing_count") or 0)
    if missing_count > 1:
        reason = f"{reason} · {missing_count}회째"
    quality_status = item.get("notification_quality_status") or ""
    quality_warnings = item.get("notification_quality_warnings") or []
    if quality_status == "blocked":
        action = "notification_quality_blocked"
        reason = "알림 문구 품질 blocked"

    report_context = item.get("report_context") or {}
    badges = [
        *(report_context.get("badges") or [])[:3],
        *([f"문구 {quality_status}"] if quality_status else []),
        *quality_warnings[:1],
    ]
    can_execute = action in {"needs_message", "needs_retry"}
    queue_item = {
        "id": f"missing:{item.get('selection_key') or item.get('student_classin_id') or item.get('student_name')}",
        "kind": "missing_homework",
        "priority": 5 if action == "notification_quality_blocked" else priority_by_action.get(action, 80),
        "tone": "danger" if action == "notification_quality_blocked" else tone_by_action.get(action, "info"),
        "student_name": item.get("student_name") or "미등록",
        "class_name": item.get("class_name") or "",
        "reason": reason,
        "next_action": "문구 수정" if action == "notification_quality_blocked" else next_action_by_action.get(action, "상태 확인"),
        "action_label": "미제출 확인",
        "tab": "missing",
        "lane": "ClassIn Data Sub",
        "can_execute": can_execute,
        "blocking_reason": "" if can_execute else reason,
        "badges": badges,
        "evidence": item.get("lesson_classin_id") or item.get("date") or "",
    }
    return _with_operating_guidance(queue_item)


def _data_review_action(item: dict[str, Any]) -> dict[str, Any]:
    return _with_operating_guidance({
        "id": f"data_review:{item.get('source') or item.get('student_name') or item.get('kind')}",
        "kind": "data_review",
        "priority": 40,
        "tone": "warn",
        "student_name": item.get("student_name") or "미등록",
        "class_name": item.get("class_name") or "",
        "reason": item.get("reason") or "학생 자동 매칭 필요",
        "next_action": "데이터 확인",
        "action_label": "데이터 확인",
        "tab": "missing",
        "lane": "학원 데이터 융합",
        "can_execute": False,
        "blocking_reason": "자동 매칭 확인 필요",
        "badges": [item.get("kind") or "local_data"],
        "evidence": item.get("source") or "",
    })


def _weekly_draft_action(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("approved"):
        return None
    status = item.get("quality_status") or ""
    if status not in {"blocked", "review", "ready"}:
        return None
    priority = {"blocked": 15, "review": 50, "ready": 85}[status]
    next_action = {
        "blocked": "리포트 수정",
        "review": "교사 검토",
        "ready": "승인하기",
    }[status]
    tone = {"blocked": "danger", "review": "warn", "ready": "info"}[status]
    warnings = item.get("quality_warnings") or []
    return _with_operating_guidance({
        "id": f"weekly_draft:{item.get('student_classin_id') or item.get('student_name')}",
        "kind": "weekly_report",
        "priority": priority,
        "tone": tone,
        "student_name": item.get("student_name") or "미등록",
        "class_name": item.get("class_name") or "",
        "reason": f"리포트 품질 {status}",
        "next_action": next_action,
        "action_label": "리포트 검토",
        "tab": "reports",
        "lane": "개별 리포트",
        "can_execute": status == "ready",
        "blocking_reason": "" if status == "ready" else (warnings[0] if warnings else f"품질 {status}"),
        "badges": [
            f"{status} {item.get('quality_score') or 0}/100",
            *warnings[:2],
            *([item.get("report_context_summary")] if item.get("report_context_summary") else []),
        ],
        "evidence": item.get("html_path") or item.get("public_url") or "",
    })


def _composition_action(item: dict[str, Any]) -> dict[str, Any] | None:
    status = item.get("readiness_status") or ""
    if status == "ready":
        return None
    priority = 18 if status == "blocked" else 55
    warnings = item.get("readiness_warnings") or []
    counts = item.get("source_counts") or {}
    return _with_operating_guidance({
        "id": f"report_composition:{item.get('student_classin_id') or item.get('student_name')}",
        "kind": "report_composition",
        "priority": priority,
        "tone": "danger" if status == "blocked" else "warn",
        "student_name": item.get("student_name") or "미등록",
        "class_name": item.get("class_name") or "",
        "reason": f"리포트 구성 {status}",
        "next_action": "근거 보강",
        "action_label": "구성 확인",
        "tab": "reports",
        "lane": "개별 리포트",
        "can_execute": False,
        "blocking_reason": warnings[0] if warnings else "리포트 근거 확인 필요",
        "badges": [
            item.get("focus") or "구성 확인",
            f"ClassIn {counts.get('classin_lessons', 0)}",
            f"시험 {(counts.get('exam_results', 0) or 0) + (counts.get('offline_scores', 0) or 0)}",
            f"메모 {counts.get('memos', 0)}",
        ],
        "evidence": item.get("context_summary") or "",
    })


def _dedupe_badges(item: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    badges: list[str] = []
    for badge in item.get("badges") or []:
        text = str(badge).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        badges.append(text)
    item["badges"] = badges[:5]
    return item


def _with_operating_guidance(item: dict[str, Any]) -> dict[str, Any]:
    kind = item.get("kind") or ""
    can_execute = bool(item.get("can_execute"))
    item["execution_state"] = "ready" if can_execute else "review_required"
    item["safety_gate"] = _safety_gate(kind, item)
    item["completion_check"] = _completion_check(kind, item)
    item["operator_note"] = _operator_note(kind, item)
    return item


def _safety_gate(kind: str, item: dict[str, Any]) -> str:
    if kind == "missing_homework":
        if item.get("can_execute"):
            return "연락처·문구 품질·발송 모드 확인 후 dry-run 또는 승인된 발송만 진행"
        return item.get("blocking_reason") or "문구 품질과 학생 상태를 먼저 확인"
    if kind == "data_review":
        return "동명이인·반·날짜를 확인하기 전에는 리포트 문장에 반영 금지"
    if kind == "weekly_report":
        if item.get("can_execute"):
            return "품질 ready와 승인 대상 학생을 확인한 뒤 Notion 아카이브"
        return item.get("blocking_reason") or "품질 경고를 보강한 뒤 승인"
    if kind == "report_composition":
        return "출결·숙제·시험·메모 근거가 비어 있으면 초안 생성 전 보강"
    return "담당자가 근거를 확인한 뒤 진행"


def _completion_check(kind: str, item: dict[str, Any]) -> str:
    if kind == "missing_homework":
        return "알림 기록이 dry_run/sent로 남고 다음 조회에서 pending/failed가 줄어야 함"
    if kind == "data_review":
        return "확인 필요 항목이 학생 ID·반·날짜 기준으로 매칭 또는 보류 처리됨"
    if kind == "weekly_report":
        return "드래프트가 승인됐거나 품질 경고가 ready/review 가능한 수준으로 낮아짐"
    if kind == "report_composition":
        return "preflight 섹션 경고가 줄고 학생별 초안 생성 근거가 확보됨"
    return "운영 리포트에 처리 결과가 남음"


def _operator_note(kind: str, item: dict[str, Any]) -> str:
    if item.get("can_execute"):
        return "바로 실행 가능"
    reason = item.get("blocking_reason") or ""
    if reason:
        return f"검토 필요: {reason}"
    if kind == "weekly_report":
        return "검토 필요: 품질 경고 확인"
    return "검토 필요"
