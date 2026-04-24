"""코어 엔진: 스케줄 입력 → Claude 파싱 → CED API → Notion ID 저장.

지침(03_plan §1): 이 엔진 없이는 나머지 MVP 불가. 가장 먼저 구현.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

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
        for c in parsed:
            log.info("DRY course=%s lessons=%d", c.course_name, len(c.lessons))
        return result

    with ClassInClient(
        base_url=cfg.classin.base_url,
        school_id=cfg.classin.school_id,
        secret_key=cfg.classin.secret_key,
    ) as client:
        ced = CEDClient(client)
        _ = NotionRepo.from_config(cfg)  # Course/Class ID 저장용. Course Master DB 추가 시 upsert.

        for pc in parsed:
            try:
                course = ced.add_course(Course(name=pc.course_name))
                result.courses_created += 1
            except Exception as e:
                result.errors.append(f"course {pc.course_name}: {e}")
                continue

            for pl in pc.lessons:
                try:
                    ced.add_course_class(
                        Lesson(
                            course_id=course.classin_id or "",
                            title=pl.title,
                            start_at=pl.start_at,
                            end_at=pl.end_at,
                        )
                    )
                    result.lessons_created += 1
                except Exception as e:
                    result.errors.append(f"lesson {pl.title}: {e}")
                    continue

                # Homework: LMS Unit → Classroom → Activity 를 먼저 생성한 뒤 releaseActivity.
                # 현재 단계는 스케줄 파싱까지만 지원. LMS 생성기는 후속 TODO.
                if pl.homework:
                    result.errors.append(
                        f"homework {pl.homework.title}: LMS Unit/Activity 선생성 플로우 미구현"
                    )

    # TODO: Course/Lesson ID 를 Notion "수업 Master DB" 에 저장. 현재 Notion 스키마는
    #       학생/수업기록/리포트/메모/시험 5개이므로 Course Master DB 추가 시 이 블록에 upsert 로직 삽입.
    return result
