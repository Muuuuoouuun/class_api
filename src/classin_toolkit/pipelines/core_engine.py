"""코어 엔진: 스케줄 입력 → Claude 파싱 → CED API → Notion ID 저장.

지침(03_plan §1): 이 엔진 없이는 나머지 MVP 불가. 가장 먼저 구현.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from ..classin.ced import CEDClient
from ..classin.client import ClassInClient
from ..classin.schemas import Course, Lesson
from ..config import AppConfig
from ..intelligence.schedule_parser import parse_schedule
from ..storage.notion_repo import NotionRepo

log = logging.getLogger(__name__)


@dataclass
class EngineResult:
    courses_created: int
    lessons_created: int
    homework_created: int
    errors: list[str]


def run_core_engine(cfg: AppConfig, *, schedule_text: str, dry_run: bool = False) -> EngineResult:
    parsed = parse_schedule(cfg, schedule_text)
    log.info("parsed %d courses", len(parsed))

    result = EngineResult(0, 0, 0, [])

    if dry_run:
        for pc in parsed:
            teacher_uid = _resolve_teacher_uid(cfg, pc.teacher_name)
            if cfg.classin.schedule_api == "lms" and not teacher_uid:
                result.errors.append(
                    f"course {pc.course_name}: teacher UID missing"
                    + (f" for {pc.teacher_name}" if pc.teacher_name else "")
                )
                continue
            result.courses_created += 1
            result.lessons_created += len(pc.lessons)
            result.homework_created += sum(1 for lesson in pc.lessons if lesson.homework)
            log.info(
                "DRY course=%s lessons=%d homework=%d api=%s",
                pc.course_name,
                len(pc.lessons),
                sum(1 for lesson in pc.lessons if lesson.homework),
                cfg.classin.schedule_api,
            )
        return result

    with ClassInClient(
        base_url=cfg.classin.base_url,
        school_id=cfg.classin.school_id,
        secret_key=cfg.classin.secret_key,
    ) as client:
        ced = CEDClient(client)
        _ = NotionRepo.from_config(cfg)  # Course/Class ID 저장용. Course Master DB 추가 시 upsert.

        for pc in parsed:
            teacher_uid = _resolve_teacher_uid(cfg, pc.teacher_name)
            if cfg.classin.schedule_api == "lms" and not teacher_uid:
                result.errors.append(
                    f"course {pc.course_name}: teacher UID missing"
                    + (f" for {pc.teacher_name}" if pc.teacher_name else "")
                )
                continue

            try:
                course = ced.add_course(
                    Course(
                        name=pc.course_name,
                        teacher_ids=[teacher_uid] if teacher_uid else [],
                    )
                )
                result.courses_created += 1
            except Exception as e:
                result.errors.append(f"course {pc.course_name}: {e}")
                continue

            unit_id: int | None = None
            if cfg.classin.schedule_api == "lms":
                try:
                    unit_id = ced.create_unit(
                        course_id=course.classin_id or "",
                        name=_lms_unit_name(cfg, pc.course_name, [pl.start_at for pl in pc.lessons]),
                        publish_flag=2,
                    )
                except Exception as e:
                    result.errors.append(f"unit {pc.course_name}: {e}")
                    continue

            for pl in pc.lessons:
                lesson = Lesson(
                    course_id=course.classin_id or "",
                    title=pl.title,
                    start_at=pl.start_at,
                    end_at=pl.end_at,
                    teacher_id=teacher_uid,
                )
                try:
                    if cfg.classin.schedule_api == "lms":
                        if unit_id is None:
                            raise ValueError("unit_id is required for LMS classroom creation")
                        ced.create_classroom(lesson, unit_id=unit_id, teacher_uid=teacher_uid)
                    else:
                        ced.add_course_class(lesson)
                    result.lessons_created += 1
                except Exception as e:
                    result.errors.append(f"lesson {pl.title}: {e}")
                    continue

                if pl.homework:
                    try:
                        if not teacher_uid:
                            raise ValueError("teacher_uid is required for LMS homework creation")
                        if unit_id is None:
                            unit_id = ced.create_unit(
                                course_id=course.classin_id or "",
                                name=_lms_unit_name(
                                    cfg, pc.course_name, [pl.start_at for pl in pc.lessons]
                                ),
                                publish_flag=2,
                            )
                        activity_id = ced.create_non_class_activity(
                            course_id=course.classin_id or "",
                            unit_id=unit_id,
                            name=pl.homework.title,
                            teacher_uid=teacher_uid,
                            activity_type=2,
                            start_time=int(pl.end_at.timestamp()),
                            end_time=(
                                int(pl.homework.due_at.timestamp())
                                if pl.homework.due_at
                                else None
                            ),
                        )
                        ced.release_activity(course_id=course.classin_id or "", activity_ids=activity_id)
                        result.homework_created += 1
                    except Exception as e:
                        result.errors.append(f"homework {pl.homework.title}: {e}")

    # TODO: Course/Lesson ID 를 Notion "수업 Master DB" 에 저장. 현재 Notion 스키마는
    #       학생/수업기록/리포트/메모/시험 5개이므로 Course Master DB 추가 시 이 블록에 upsert 로직 삽입.
    return result


def _resolve_teacher_uid(cfg: AppConfig, teacher_name: str | None) -> str | None:
    if teacher_name:
        uid = cfg.classin.teacher_uids.get(teacher_name)
        if uid:
            return uid
    return cfg.classin.default_teacher_uid or None


def _lms_unit_name(cfg: AppConfig, course_name: str, starts: list[datetime]) -> str:
    if not starts:
        return f"{cfg.classin.lms_unit_prefix} - {course_name}"
    first = min(starts).date()
    last = max(starts).date()
    if first == last:
        date_label = first.isoformat()
    else:
        date_label = f"{first.isoformat()}~{last.isoformat()}"
    return f"{cfg.classin.lms_unit_prefix} - {course_name} - {date_label}"
