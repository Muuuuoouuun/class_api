from pathlib import Path
from datetime import datetime, timezone

from classin_toolkit.config import AppConfig
from classin_toolkit.notify.message import OutgoingMessage
from classin_toolkit.pipelines.exams import (
    ExamImportRow,
    load_exam_rows,
    merge_exam_results,
    sweep_missing_exam,
)
from classin_toolkit.storage.notion_repo import StudentRecord


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "academy": {"name": "테스트학원", "timezone": "Asia/Seoul"},
            "classin": {
                "school_id": "sid",
                "secret_key": "secret",
                "webhook_secret": "webhook",
            },
            "notion": {
                "token": "secret_test",
                "databases": {
                    "students": "students",
                    "lessons": "lessons",
                    "reports": "reports",
                    "memos": "memos",
                    "exams": "exams",
                },
            },
            "anthropic": {"api_key": "sk-ant-test"},
            "reports": {"output_dir": str(tmp_path / "reports")},
            "notify": {"mode": "dry_run", "provider": "aligo"},
        }
    )


def test_load_exam_rows_from_csv_applies_defaults(tmp_path: Path) -> None:
    path = tmp_path / "exam.csv"
    path.write_text(
        "student_name,class_name,subject,score,max_score,attended\n"
        "홍길동,고2-A,수학,95점,100,true\n"
        "김영희,고2-A,수학,미응시,\"1,000\",false\n",
        encoding="utf-8",
    )

    rows = load_exam_rows(
        path,
        default_exam_name="4월 월말평가",
        default_exam_date="2026-04-24",
        default_source="academy-db",
    )

    assert len(rows) == 2
    assert rows[0].exam_name == "4월 월말평가"
    assert rows[0].exam_date.date().isoformat() == "2026-04-24"
    assert rows[0].score == 95.0
    assert rows[0].attended is True
    assert rows[1].attended is False
    assert rows[1].score is None
    assert rows[1].max_score == 1000.0
    assert rows[1].source == "academy-db"


def test_merge_exam_results_resolves_students_by_id_and_name(tmp_path: Path) -> None:
    students = [
        StudentRecord(
            page_id="p1",
            classin_id="10001",
            name="홍길동",
            parent_phone="01012345678",
            class_name="고2-A",
        ),
        StudentRecord(
            page_id="p2",
            classin_id="10002",
            name="김영희",
            parent_phone="01055556666",
            class_name="고2-A",
        ),
    ]
    merged_calls: list[dict] = []

    class FakeRepo:
        def list_active_students(self):
            return students

        def upsert_exam_result(self, **kwargs):
            merged_calls.append(kwargs)
            return f"exam-{len(merged_calls)}"

    exam_date = datetime(2026, 4, 24, tzinfo=timezone.utc)
    rows = [
        ExamImportRow(
            exam_name="4월 월말평가",
            exam_date=exam_date,
            student_classin_id="10001",
            score=92,
            max_score=100,
        ),
        ExamImportRow(
            exam_name="4월 월말평가",
            exam_date=exam_date,
            student_name="김영희",
            class_name="고2-A",
            attended=False,
        ),
        ExamImportRow(
            exam_name="4월 월말평가",
            exam_date=exam_date,
            student_name="없는학생",
        ),
    ]

    result = merge_exam_results(_cfg(tmp_path), rows, repo=FakeRepo())

    assert result.total_rows == 3
    assert result.merged_rows == 2
    assert result.unresolved_rows == 1
    assert len(merged_calls) == 2
    assert merged_calls[0]["student_classin_id"] == "10001"
    assert merged_calls[0]["student"].page_id == "p1"
    assert merged_calls[1]["student_classin_id"] == "10002"
    assert merged_calls[1]["attended"] is False


def test_merge_exam_results_dry_run_does_not_write(tmp_path: Path) -> None:
    students = [
        StudentRecord(
            page_id="p1",
            classin_id="10001",
            name="홍길동",
            parent_phone="01012345678",
            class_name="고2-A",
        )
    ]

    class FakeRepo:
        def list_active_students(self):
            return students

        def upsert_exam_result(self, **kwargs):
            raise AssertionError("dry-run must not write to Notion")

    result = merge_exam_results(
        _cfg(tmp_path),
        [
            ExamImportRow(
                exam_name="4월 월말평가",
                exam_date=datetime(2026, 4, 24, tzinfo=timezone.utc),
                student_classin_id="10001",
                score=92,
                max_score=100,
            )
        ],
        repo=FakeRepo(),
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.total_rows == 1
    assert result.merged_rows == 1
    assert result.skipped_rows == 0


def test_sweep_missing_exam_dispatches_missing_exam_event(monkeypatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    students = {
        "10002": StudentRecord(
            page_id="p2",
            classin_id="10002",
            name="김영희",
            parent_phone="01055556666",
            class_name="고2-A",
        )
    }

    class FakeRepo:
        def find_missing_exam(self, *, exam_name, exam_date, class_name=None):
            assert exam_name == "4월 월말평가"
            assert exam_date.date().isoformat() == "2026-04-24"
            assert class_name == "고2-A"
            return [
                {
                    "student_classin_id": "10002",
                    "student_name": "김영희",
                    "student_class_name": "고2-A",
                    "parent_phone": "01055556666",
                    "exam_name": "4월 월말평가",
                    "exam_date": "2026-04-24",
                }
            ]

        def resolve_students(self, ids):
            return {student_id: students[student_id] for student_id in ids}

    captured: dict = {}

    def fake_from_config(_cfg):
        return FakeRepo()

    def fake_compose_messages(*, cfg, exam_name, exam_date, by_student, students_lookup):
        captured["compose"] = {
            "exam_name": exam_name,
            "exam_date": exam_date,
            "student_ids": sorted(by_student.keys()),
            "lookup_ids": sorted(students_lookup.keys()),
        }
        return [
            OutgoingMessage(
                student_classin_id="10002",
                student_name="김영희",
                parent_phone="01055556666",
                message="시험 안내",
            )
        ]

    async def fake_dispatch_notifications(cfg, messages, *, event_type):
        captured["dispatch"] = {
            "event_type": event_type,
            "message_count": len(messages),
            "student_id": messages[0].student_classin_id,
        }

    monkeypatch.setattr("classin_toolkit.pipelines.exams.NotionRepo.from_config", fake_from_config)
    monkeypatch.setattr(
        "classin_toolkit.pipelines.exams.compose_messages_from_rows",
        fake_compose_messages,
    )
    monkeypatch.setattr(
        "classin_toolkit.pipelines.exams.dispatch_notifications",
        fake_dispatch_notifications,
    )

    count = sweep_missing_exam(
        cfg,
        exam_name="4월 월말평가",
        exam_date="2026-04-24",
        class_name="고2-A",
    )

    assert count == 1
    assert captured["compose"]["student_ids"] == ["10002"]
    assert captured["dispatch"] == {
        "event_type": "missing_exam",
        "message_count": 1,
        "student_id": "10002",
    }
