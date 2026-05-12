from __future__ import annotations

import json
import zipfile
from pathlib import Path

from classin_toolkit.config import AppConfig
from classin_toolkit.pipelines.data_merge import build_report_contexts


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
                },
            },
            "anthropic": {"api_key": "sk-ant-test"},
            "output": {"weekly": {"path": str(tmp_path / "weekly")}},
        }
    )


def test_build_report_contexts_merges_weekly_csv_xlsx_and_memos(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    weekly = tmp_path / "weekly"
    weekly.mkdir()
    (weekly / "2026-04-20_drafts.json").write_text(
        json.dumps(
            [
                {
                    "student_classin_id": "10002",
                    "student_name": "김지각",
                    "html_path": "reports_out/weekly/김지각.html",
                    "public_url": None,
                    "period_start": "2026-04-20T00:00:00+09:00",
                    "period_end": "2026-04-26T23:59:00+09:00",
                    "summary_markdown": "요약",
                    "parent_message": "문구",
                    "approved": False,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    inbox = tmp_path / "local_data" / "inbox"
    attendance = inbox / "attendance"
    scores = inbox / "scores"
    memos = inbox / "memos"
    attendance.mkdir(parents=True)
    scores.mkdir()
    memos.mkdir()

    attendance_file = attendance / "offline.csv"
    attendance_file.write_text(
        "student_classin_id,student_name,class_name,date,attendance\n"
        "10002,김지각,고2-A,2026-04-22,지각\n"
        ",미등록,고2-A,2026-04-22,출석\n",
        encoding="utf-8",
    )
    _write_xlsx(
        scores / "scores.xlsx",
        [
            ["student_name", "class_name", "subject", "score", "date"],
            ["김지각", "고2-A", "단원평가", "68", "2026-04-23"],
        ],
    )
    memo_file = memos / "10003_이하락.md"
    memo_file.write_text(
        "student_classin_id: 10003\n"
        "date: 2026-04-24\n\n"
        "집중도 하락으로 상담 필요",
        encoding="utf-8",
    )
    before = attendance_file.read_text(encoding="utf-8")

    result = build_report_contexts(
        cfg,
        [
            {
                "student_classin_id": "10002",
                "student_name": "김지각",
                "student_class_name": "고2-A",
            },
            {
                "student_classin_id": "10003",
                "student_name": "이하락",
                "student_class_name": "고2-A",
            },
        ],
        inbox_dir=inbox,
    )

    context = result.contexts["10002"]
    assert context["weekly_report"]["status"] == "draft_ready"
    assert context["offline_attendance"] == 1
    assert context["offline_scores"] == 1
    assert "리포트 초안" in context["badges"]
    assert "오프라인 시험 1건" in context["summary"]
    assert result.contexts["10003"]["memos"] == 1
    assert result.summary["needs_review"] == 1
    assert result.needs_review_items[0]["student_name"] == "미등록"
    assert attendance_file.read_text(encoding="utf-8") == before


def _write_xlsx(path: Path, rows: list[list[str]]) -> None:
    sheet_rows = []
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for col_number, value in enumerate(row):
            cell_ref = f"{chr(ord('A') + col_number)}{row_number}"
            cells.append(
                f'<c r="{cell_ref}" t="inlineStr"><is><t>{value}</t></is></c>'
            )
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        "</worksheet>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
