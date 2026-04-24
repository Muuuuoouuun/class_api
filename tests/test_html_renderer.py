from datetime import datetime, timezone

from classin_toolkit.config import AppConfig
from classin_toolkit.storage.html_renderer import (
    HtmlDailyRenderer,
    HtmlWeeklyRenderer,
    _homework_rate,
    _md_to_html,
)
from classin_toolkit.storage.output_port import DailySnapshot, WeeklyRenderInput


def _cfg(tmp_path, daily_mode="html", weekly_mode="html+notion") -> AppConfig:
    return AppConfig.model_validate(
        {
            "academy": {"name": "○○수학학원"},
            "classin": {"school_id": "1", "secret_key": "b"},
            "notion": {
                "token": "t",
                "databases": {"students": "s", "lessons": "l", "reports": "r"},
            },
            "anthropic": {"api_key": "k"},
            "output": {
                "daily": {"mode": daily_mode, "path": str(tmp_path / "daily")},
                "weekly": {"mode": weekly_mode, "path": str(tmp_path / "weekly")},
            },
        }
    )


def test_daily_html_written(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    snap = DailySnapshot(
        date="2026-04-24",
        academy=cfg.academy.name,
        lessons=[{"time": "19:00", "lesson_title": "지수함수", "present_count": 3, "late_count": 1, "absent_count": 1}],
        missing_homework=[{"student_name": "김지각", "class_name": "고2-A"}],
        attendance_rate=0.8,
        generated_at=datetime(2026, 4, 24, 22, 15),
    )
    result = HtmlDailyRenderer().write(cfg, snap)
    assert result.path is not None and result.path.exists()
    body = result.path.read_text(encoding="utf-8")
    assert "일일 현황 2026-04-24" in body
    assert "김지각" in body
    assert "80%" in body  # attendance rate rendered


def test_weekly_draft_html(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    inp = WeeklyRenderInput(
        student_classin_id="10001",
        student_name="박성실",
        class_name="고2-A",
        period_start=datetime(2026, 4, 20, tzinfo=timezone.utc),
        period_end=datetime(2026, 4, 26, 23, 59, tzinfo=timezone.utc),
        lessons=[
            {"date": "2026-04-22", "attendance": "출석", "attendance_seconds": 7200,
             "hand_raise": 6, "trophy": 2, "homework_submitted": True},
        ],
        prev_week_lessons=[],
        summary_markdown="## 이번 주 요약\n박성실 학생은 출석률 100%.\n\n- 손들기 6회\n- 트로피 2개",
        parent_message="안녕하세요 성실 어머님, 이번 주 수고하셨어요.",
    )
    result = HtmlWeeklyRenderer().write_draft(cfg, inp)
    assert result.path is not None and result.path.exists()
    body = result.path.read_text(encoding="utf-8")
    assert "박성실" in body
    assert "<h3>이번 주 요약</h3>" in body  # md_to_html h2 -> h3
    assert "성실 어머님" in body


def test_md_to_html_lists_and_headers() -> None:
    html = _md_to_html("## 제목\n- 첫째\n- 둘째\n\n본문 한 줄")
    assert "<h3>제목</h3>" in html
    assert "<ul>" in html and "</ul>" in html
    assert "<li>첫째</li>" in html
    assert "<p>본문 한 줄</p>" in html


def test_homework_rate_only_counts_known() -> None:
    assert _homework_rate([]) == 0.0
    assert _homework_rate([{"homework_submitted": None}]) == 0.0
    rows = [
        {"homework_submitted": True},
        {"homework_submitted": False},
        {"homework_submitted": None},
    ]
    assert _homework_rate(rows) == 0.5
