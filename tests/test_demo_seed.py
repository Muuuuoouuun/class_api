from datetime import date

from classin_toolkit.config import AppConfig
from classin_toolkit.pipelines import demo_seed
from classin_toolkit.pipelines.demo_seed import build_demo_dataset, seed_demo_data


def _cfg() -> AppConfig:
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
                },
            },
            "anthropic": {"api_key": "sk-ant-test"},
        }
    )


def test_build_demo_dataset_has_five_personas_and_two_lessons_per_week() -> None:
    dataset = build_demo_dataset(base_date=date(2026, 4, 24), weeks=3)

    assert len(dataset.students) == 5
    assert len(dataset.lesson_rows) == 5 * 2 * 3
    assert {s.name for s in dataset.students} == {
        "박성실",
        "김지각",
        "이하락",
        "정활발",
        "최결석",
    }


def test_demo_dataset_models_distinct_personas() -> None:
    dataset = build_demo_dataset(base_date=date(2026, 4, 24), weeks=1)
    by_student = {}
    for row in dataset.lesson_rows:
        by_student.setdefault(row.student.name, []).append(row)

    assert all(row.homework_submitted for row in by_student["박성실"])
    assert any(not row.homework_submitted for row in by_student["김지각"])
    assert all(row.attendance_seconds == 0 for row in by_student["최결석"])
    assert sum(row.hand_raise for row in by_student["정활발"]) > sum(
        row.hand_raise for row in by_student["이하락"]
    )


def test_seed_demo_data_dry_run_does_not_touch_repo() -> None:
    result = seed_demo_data(_cfg(), base_date=date(2026, 4, 24), weeks=2, dry_run=True)

    assert result.dry_run is True
    assert result.students == 5
    assert result.lesson_rows == 20


def test_seed_demo_data_writes_students_and_lesson_rows(monkeypatch) -> None:
    fake = FakeRepo()
    monkeypatch.setattr(demo_seed.NotionRepo, "from_config", lambda _cfg: fake)

    result = seed_demo_data(_cfg(), base_date=date(2026, 4, 24), weeks=1, dry_run=False)

    assert result.dry_run is False
    assert len(fake.students) == 5
    assert len(fake.upserts) == 10
    assert len(fake.patches) == 10
    assert fake.patches[-1]["homework_activity_id"] == "DEMO-HW-001"


class FakeRepo:
    def __init__(self) -> None:
        self.students = []
        self.upserts = []
        self.patches = []

    def upsert_student(self, **kwargs):
        self.students.append(kwargs)

    def upsert_lesson_record(self, **kwargs):
        self.upserts.append(kwargs)
        return "lesson-page"

    def patch_lesson_record(self, **kwargs):
        self.patches.append(kwargs)
        return "lesson-page"
