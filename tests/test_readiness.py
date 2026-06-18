from pathlib import Path

from classin_toolkit.config import AppConfig
from classin_toolkit.readiness import check_readiness


def _cfg(**overrides) -> AppConfig:
    data = {
        "academy": {"name": "테스트학원", "timezone": "Asia/Seoul"},
        "classin": {
                "school_id": "sid_123",
                "secret_key": "secret_123",
                "webhook_secret": "webhook_123",
                "default_teacher_uid": "teacher-1",
            },
        "notion": {
            "token": "secret_test",
            "databases": {
                "students": "students_db",
                "lessons": "lessons_db",
                "reports": "reports_db",
                "memos": "memos_db",
                "exams": "exams_db",
            },
        },
        "anthropic": {"api_key": "sk-ant-test"},
        "notify": {
            "mode": "dry_run",
            "provider": "aligo",
            "aligo": {"api_key": "", "user_id": "", "sender": ""},
        },
        "output": {
            "daily": {"path": "./reports_out/daily", "public_url_base": ""},
            "weekly": {"path": "./reports_out/weekly"},
            "memo": {"mode": "notion"},
        },
    }
    _deep_update(data, overrides)
    return AppConfig.model_validate(data)


def test_local_demo_ready_with_samples(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    for name in _SAMPLE_PAYLOADS:
        (samples / name).write_text("{}", encoding="utf-8")

    report = check_readiness(_cfg(), mode="local-demo", project_root=tmp_path)

    assert report.ready
    assert not report.blockers


def test_local_demo_flags_placeholder_values(tmp_path: Path) -> None:
    report = check_readiness(
        _cfg(
            notion={
                "token": "secret_REPLACE_ME",
                "databases": {
                    "students": "REPLACE_ME_STUDENTS_DB_ID",
                    "lessons": "lessons_db",
                    "reports": "reports_db",
                    "memos": "memos_db",
                    "exams": "exams_db",
                },
            }
        ),
        mode="local-demo",
        project_root=tmp_path,
    )

    labels = {item.label for item in report.blockers}
    assert "Notion 토큰" in labels
    assert "학생 Master DB ID" in labels
    assert any(item.label.startswith("샘플 페이로드") for item in report.blockers)


def test_local_demo_requires_exam_db_id(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    for name in _SAMPLE_PAYLOADS:
        (samples / name).write_text("{}", encoding="utf-8")

    report = check_readiness(
        _cfg(notion={"databases": {"exams": "REPLACE_ME_EXAMS_DB_ID"}}),
        mode="local-demo",
        project_root=tmp_path,
    )

    assert any(item.label == "시험 DB ID" for item in report.blockers)


def test_classin_live_requires_webhook_secret(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    for name in _SAMPLE_PAYLOADS:
        (samples / name).write_text("{}", encoding="utf-8")

    report = check_readiness(
        _cfg(classin={"webhook_secret": "REPLACE_ME"}),
        mode="classin-live",
        project_root=tmp_path,
    )

    assert any(item.label == "Webhook SafeKey secret" for item in report.blockers)


def test_classin_live_lms_requires_teacher_uid_mapping(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    for name in _SAMPLE_PAYLOADS:
        (samples / name).write_text("{}", encoding="utf-8")

    report = check_readiness(
        _cfg(classin={"schedule_api": "lms", "teacher_uids": {}, "default_teacher_uid": None}),
        mode="classin-live",
        project_root=tmp_path,
    )

    assert any(item.label == "ClassIn 교사 UID 매핑" for item in report.blockers)


def test_kakao_live_ready_when_aligo_template_config_is_complete(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    for name in _SAMPLE_PAYLOADS:
        (samples / name).write_text("{}", encoding="utf-8")

    report = check_readiness(
        _cfg(
            notify={
                "mode": "live",
                "provider": "aligo",
                "aligo": {
                    "api_key": "aligo-key",
                    "user_id": "moon",
                    "sender": "01012345678",
                    "sender_key": "sender-key",
                    "template_code_missing_homework": "TPL_HOMEWORK",
                },
            }
        ),
        mode="kakao-live",
        project_root=tmp_path,
    )

    assert report.ready
    assert any(item.label == "실제 알림톡 발송 구현" and item.status == "ok" for item in report.items)


def test_kakao_live_requires_aligo_senderkey_and_template_code(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    for name in _SAMPLE_PAYLOADS:
        (samples / name).write_text("{}", encoding="utf-8")

    report = check_readiness(
        _cfg(
            notify={
                "mode": "live",
                "provider": "aligo",
                "aligo": {
                    "api_key": "aligo-key",
                    "user_id": "moon",
                    "sender": "01012345678",
                },
            }
        ),
        mode="kakao-live",
        project_root=tmp_path,
    )

    labels = {item.label for item in report.blockers}
    assert "알리고 senderkey" in labels
    assert "숙제 미제출 템플릿 코드" in labels
    assert not report.ready


def _deep_update(target: dict, updates: dict) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


_SAMPLE_PAYLOADS = (
    "attendance_sample.json",
    "end_summary_sample.json",
    "homework_submit_sample.json",
)
