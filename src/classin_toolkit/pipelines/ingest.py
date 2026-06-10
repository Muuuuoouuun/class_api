"""Webhook Cmd 별 ingest 핸들러 (Layer 4).

- Attendance      → 수업 기록 DB 에 학생별 row upsert (출석·실참여시간)
- End             → 수업 전체 요약치를 수업 기록 row 에 분배 (손들기·트로피·카메라·Poll)
- HomeworkSubmit  → 수업 기록 row 에 숙제 제출 = True
- HomeworkScore   → 수업 기록 row 에 점수 저장

"미제출자 카톡" 은 여기가 아니라 `pipelines.missing_homework.sweep_missing_homework` 에서
배치로 실행. Webhook 핸들러는 I/O 얇게, 부가 로직 최소.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..classin.webhook_schemas import (
    AnswerSheetScoreEvent,
    AttendanceEvent,
    EndEvent,
    HomeworkScoreEvent,
    HomeworkSubmitEvent,
)
from ..config import AppConfig
from ..storage.notion_repo import NotionRepo

log = logging.getLogger(__name__)


async def ingest_attendance(event: AttendanceEvent, cfg: AppConfig) -> None:
    repo = NotionRepo.from_config(cfg)
    for m in event.Data:
        if m.Identity and m.Identity != 1:  # 학생만 (1=student)
            continue
        repo.upsert_lesson_record(
            lesson_id=event.class_id or "",
            course_id=event.course_id or "",
            student_classin_id=str(m.Uid),
            class_start=event.ClassStartTime,
            class_end=event.ClassEndTime,
            attendance_seconds=m.AttendanceTime,
            first_in_time=m.FirstInTime,
            last_out_time=m.LastOutTime,
        )
    log.info("attendance ingested lesson=%s n=%d", event.class_id, len(event.Data))


async def ingest_end_summary(event: EndEvent, cfg: AppConfig) -> None:
    repo = NotionRepo.from_config(cfg)
    camera = event.camera_minutes_by_uid()
    hand_raise = event.hand_raise_by_uid()
    trophies = event.trophy_by_uid()
    polls = event.poll_by_uid()
    for uid in set(camera) | set(hand_raise) | set(trophies) | set(polls):
        repo.patch_lesson_record(
            lesson_id=event.class_id or "",
            student_classin_id=uid,
            camera_minutes=camera.get(uid),
            hand_raise=hand_raise.get(uid),
            trophy=trophies.get(uid),
            poll=polls.get(uid),
        )
    log.info(
        "end summary ingested lesson=%s hand_total=%d trophy_total=%d",
        event.class_id,
        event.hand_raise_total(),
        event.trophy_total(),
    )


async def ingest_homework_submit(event: HomeworkSubmitEvent, cfg: AppConfig) -> None:
    repo = NotionRepo.from_config(cfg)
    sid = event.Data.StudentInfo.Uid if event.Data.StudentInfo else None
    if not sid:
        log.warning("homework submit without StudentInfo.Uid event=%s", event.Cmd)
        return
    repo.patch_lesson_record(
        lesson_id=event.class_id,
        student_classin_id=str(sid),
        homework_submitted=True,
        homework_submitted_late=bool(event.Data.IsSubmitLate),
        homework_activity_id=str(event.Data.ActivityId),
    )


async def ingest_homework_score(event: HomeworkScoreEvent, cfg: AppConfig) -> None:
    repo = NotionRepo.from_config(cfg)
    sid = event.Data.StudentInfo.Uid if event.Data.StudentInfo else None
    if not sid:
        return
    repo.patch_lesson_record(
        lesson_id=event.class_id,
        student_classin_id=str(sid),
        homework_score=event.Data.Score,
        homework_activity_id=str(event.Data.ActivityId),
    )


async def ingest_answer_sheet_score(event: AnswerSheetScoreEvent, cfg: AppConfig) -> None:
    repo = NotionRepo.from_config(cfg)
    sid = event.Data.StudentInfo.Uid if event.Data.StudentInfo else None
    if not sid:
        log.warning("answer sheet score without StudentInfo.Uid event=%s", event.Cmd)
        return
    exam_name = event.Data.ActivityName or f"Answer Sheet {event.Data.ActivityId}"
    exam_date = _event_datetime(
        event.Data.SubmissionTime or event.Data.CorrectionTime or event.ActionTime or event.TimeStamp
    )
    page_id = repo.upsert_exam_result(
        student_classin_id=str(sid),
        exam_name=exam_name,
        exam_date=exam_date,
        class_name=event.CourseName,
        attended=True,
        score=event.Data.earned_score(),
        max_score=event.Data.max_score(),
        source="classin-answer-sheet",
        external_exam_id=f"answer-sheet:{event.Data.ActivityId}:{sid}",
    )
    log.info(
        "answer sheet score ingested activity=%s student=%s page=%s",
        event.Data.ActivityId,
        sid,
        page_id,
    )


def _event_datetime(timestamp: int | None) -> datetime:
    if not timestamp:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
