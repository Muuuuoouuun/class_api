import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from classin_toolkit.classin.webhook_schemas import parse_event
from classin_toolkit.config import AppConfig
from classin_toolkit.notify.message import OutgoingMessage
from classin_toolkit.pipelines.exams import (
    create_answer_sheet_activity,
    ExamImportRow,
    load_exam_rows,
    merge_exam_results,
    sweep_missing_exam,
)
from classin_toolkit.pipelines.ingest import ingest_answer_sheet_score
from classin_toolkit.storage.notion_repo import StudentRecord

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


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


def test_create_answer_sheet_activity_dry_run_does_not_call_classin(
    monkeypatch, tmp_path: Path
) -> None:
    def client_should_not_run(**_kwargs: Any):
        raise AssertionError("dry-run must not instantiate ClassInClient")

    monkeypatch.setattr("classin_toolkit.pipelines.exams.ClassInClient", client_should_not_run)

    result = create_answer_sheet_activity(
        _cfg(tmp_path),
        course_id="414193",
        unit_id="22360790",
        name="  6월 OMR 답안지  ",
        teacher_uid="1006368",
        release=True,
        dry_run=True,
    )

    assert result.activity_id is None
    assert result.name == "6월 OMR 답안지"
    assert result.released is True
    assert result.dry_run is True


def test_create_answer_sheet_activity_uses_activity_type_7_and_can_release(
    monkeypatch, tmp_path: Path
) -> None:
    class FakeClassInClient:
        instances: list["FakeClassInClient"] = []

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.v2_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
            self.instances.append(self)

        def __enter__(self) -> "FakeClassInClient":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def call_v2(self, path: str, body: dict[str, Any], **kwargs: Any) -> Any:
            self.v2_calls.append((path, body, kwargs))
            if path == "/lms/activity/createActivityNoClass":
                return {"activityId": 26019953}
            if path == "/lms/activity/release":
                return {"activityId": body["activityId"]}
            return {}

    monkeypatch.setattr("classin_toolkit.pipelines.exams.ClassInClient", FakeClassInClient)
    cfg = _cfg(tmp_path)
    cfg.classin.default_teacher_uid = "1006368"

    result = create_answer_sheet_activity(
        cfg,
        course_id="414193",
        unit_id="22360790",
        name="6월 OMR 답안지",
        start_at=datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
        release=True,
        dry_run=False,
    )

    client = FakeClassInClient.instances[-1]
    assert result.activity_id == 26019953
    assert result.released is True
    assert client.kwargs["school_id"] == "sid"
    assert client.v2_calls == [
        (
            "/lms/activity/createActivityNoClass",
            {
                "courseId": 414193,
                "unitId": 22360790,
                "activityType": 7,
                "name": "6월 OMR 답안지",
                "teacherUid": 1006368,
                "startTime": 1781168400,
                "endTime": 1781254800,
            },
            {},
        ),
        (
            "/lms/activity/release",
            {"courseId": 414193, "activityId": 26019953},
            {},
        ),
    ]


def test_ingest_answer_sheet_score_upserts_exam_result(monkeypatch, tmp_path: Path) -> None:
    repo_calls: list[dict[str, Any]] = []

    class FakeRepo:
        def upsert_exam_result(self, **kwargs: Any) -> str:
            repo_calls.append(kwargs)
            return "exam-page-1"

    monkeypatch.setattr(
        "classin_toolkit.pipelines.ingest.NotionRepo.from_config",
        staticmethod(lambda _cfg: FakeRepo()),
    )
    raw = json.loads((SAMPLES / "answer_sheet_score_sample.json").read_text(encoding="utf-8"))
    event = parse_event(raw)

    asyncio.run(ingest_answer_sheet_score(event, _cfg(tmp_path)))  # type: ignore[arg-type]

    assert repo_calls == [
        {
            "student_classin_id": "10001",
            "exam_name": "6월 OMR 답안지",
            "exam_date": datetime(2026, 5, 1, 4, 50, tzinfo=timezone.utc),
            "class_name": "고2-A",
            "attended": True,
            "score": 12,
            "max_score": 14.0,
            "source": "classin-answer-sheet",
            "external_exam_id": "answer-sheet:99007:10001",
        }
    ]


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
