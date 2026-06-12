from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from classin_toolkit.classin.webhook_schemas import HomeworkScoreEvent, HomeworkSubmitEvent, parse_event
from classin_toolkit.config import AppConfig
from classin_toolkit.pipelines.ingest import ingest_homework_score, ingest_homework_submit
from classin_toolkit.storage.local_repo import LocalRepo


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "academy": {"name": "Test Academy", "timezone": "Asia/Seoul"},
            "classin": {"school_id": "1", "secret_key": "secret"},
            "anthropic": {"api_key": "test"},
            "storage": {"backend": "local", "path": str(tmp_path / "store.json")},
        }
    )


def test_classin_lms_payload_ingests_to_local_store(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    submit = parse_event(
        {
            "ActionTime": 1741334951,
            "CourseID": 26901289,
            "TimeStamp": 1741335551,
            "SafeKey": "safe",
            "Cmd": "HomeworkSubmit",
            "CourseName": "test course",
            "SID": 1068502,
            "Data": {
                "TeacherInfo": {
                    "TeacherName": "Lucy",
                    "TeacherAccount": "001-38102248",
                    "TeacherUid": 10602,
                },
                "ActivityName": "Magic Key",
                "UnitName": "test unit",
                "IsSubmitLate": 1,
                "StudentTotal": 2,
                "ActivityId": 54911996,
                "IsRevision": 0,
                "StudentInfo": {
                    "StudentName": "GoodGuy",
                    "StudentAccount": "23672340105",
                    "StudentUid": 102494,
                },
                "UnitId": 255618,
                "SubmissionTime": 1741334950,
                "SubmitTotal": 1,
            },
        }
    )
    score = parse_event(
        {
            "Cmd": "HomeworkScore",
            "SID": 1068502,
            "CourseID": 26901289,
            "CourseName": "test course",
            "ActionTime": 1741336000,
            "Data": {
                "ActivityId": 54911996,
                "ActivityName": "Magic Key",
                "Score": 100,
                "StudentScore": "90",
                "StudentScoringRate": 0.9,
                "CorrectionTime": 1741336000,
                "StudentInfo": {
                    "StudentUid": 102494,
                    "StudentName": "GoodGuy",
                    "StudentAccount": "23672340105",
                },
            },
        }
    )
    assert isinstance(submit, HomeworkSubmitEvent)
    assert isinstance(score, HomeworkScoreEvent)
    assert submit.Data.StudentInfo is not None
    assert submit.Data.StudentInfo.Uid == 102494
    assert submit.Data.StudentInfo.Name == "GoodGuy"

    asyncio.run(ingest_homework_submit(submit, cfg))
    asyncio.run(ingest_homework_score(score, cfg))

    repo = LocalRepo.from_config(cfg)
    students = repo.list_active_students()
    assert [(student.classin_id, student.name, student.class_name) for student in students] == [
        ("102494", "GoodGuy", "test course")
    ]

    rows = repo.lesson_records(
        since=datetime(2025, 1, 1, tzinfo=timezone.utc),
        until=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["lesson_classin_id"] == "homework:54911996"
    assert row["course_classin_id"] == "26901289"
    assert row["homework_activity_id"] == "54911996"
    assert row["homework_submitted"] is True
    assert row["homework_late"] is True
    assert row["homework_score"] == 90.0
    assert row["student_name"] == "GoodGuy"
