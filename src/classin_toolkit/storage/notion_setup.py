"""Create the Notion databases required by classin-toolkit."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from notion_client import Client


@dataclass(frozen=True)
class CreatedNotionSchema:
    students: str
    lessons: str
    reports: str
    memos: str
    exams: str

    def config_snippet(self) -> str:
        return (
            "notion:\n"
            '  token: "secret_..."\n'
            "  databases:\n"
            f'    students: "{self.students}"\n'
            f'    lessons: "{self.lessons}"\n'
            f'    reports: "{self.reports}"\n'
            f'    memos: "{self.memos}"\n'
            f'    exams: "{self.exams}"\n'
        )


def create_notion_schema(
    *,
    token: str,
    parent_page_id: str,
    prefix: str = "ClassIn Toolkit",
    client: Any | None = None,
) -> CreatedNotionSchema:
    nc = client or Client(auth=token)
    students = _create_database(
        nc,
        parent_page_id=parent_page_id,
        title=f"{prefix} - 학생 Master",
        properties=_student_properties(),
    )
    lessons = _create_database(
        nc,
        parent_page_id=parent_page_id,
        title=f"{prefix} - 수업 기록",
        properties=_lesson_properties(students),
    )
    reports = _create_database(
        nc,
        parent_page_id=parent_page_id,
        title=f"{prefix} - 리포트",
        properties=_report_properties(students),
    )
    memos = _create_database(
        nc,
        parent_page_id=parent_page_id,
        title=f"{prefix} - 메모",
        properties=_memo_properties(students),
    )
    exams = _create_database(
        nc,
        parent_page_id=parent_page_id,
        title=f"{prefix} - 시험",
        properties=_exam_properties(students),
    )
    return CreatedNotionSchema(
        students=students,
        lessons=lessons,
        reports=reports,
        memos=memos,
        exams=exams,
    )


def dry_run_schema(prefix: str = "ClassIn Toolkit") -> list[tuple[str, list[str]]]:
    return [
        (f"{prefix} - 학생 Master", list(_student_properties().keys())),
        (f"{prefix} - 수업 기록", list(_lesson_properties("학생_DB_ID").keys())),
        (f"{prefix} - 리포트", list(_report_properties("학생_DB_ID").keys())),
        (f"{prefix} - 메모", list(_memo_properties("학생_DB_ID").keys())),
        (f"{prefix} - 시험", list(_exam_properties("학생_DB_ID").keys())),
    ]


def _create_database(
    nc: Any,
    *,
    parent_page_id: str,
    title: str,
    properties: dict[str, dict],
) -> str:
    page = nc.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": title}}],
        initial_data_source={"properties": properties},
    )
    return page["data_sources"][0]["id"]


def _student_properties() -> dict[str, dict]:
    return {
        "학생명": {"title": {}},
        "ClassIn ID": {"rich_text": {}},
        "반": {"select": {"options": _options(["고2-A", "고2-B", "중3-A"])}},
        "학부모 연락처": {"phone_number": {}},
        "등록일": {"date": {}},
        "상태": {"select": {"options": _options(["재원", "휴원", "퇴원"])}},
    }


def _lesson_properties(students_db_id: str) -> dict[str, dict]:
    return {
        "기록": {"title": {}},
        "학생": {"relation": _relation(students_db_id)},
        "수업일시": {"date": {}},
        "출석 여부": {"select": {"options": _options(["출석", "지각", "결석"])}},
        "실참여시간(초)": {"number": {"format": "number"}},
        "손들기 횟수": {"number": {"format": "number"}},
        "트로피 수": {"number": {"format": "number"}},
        "카메라 시간(분)": {"number": {"format": "number"}},
        "Poll 응답": {"number": {"format": "number"}},
        "숙제 제출": {"checkbox": {}},
        "지각 제출": {"checkbox": {}},
        "숙제 점수": {"number": {"format": "number"}},
        "ClassIn 숙제 ID": {"rich_text": {}},
        "ClassIn 수업 ID": {"rich_text": {}},
        "ClassIn 반 ID": {"rich_text": {}},
    }


def _report_properties(students_db_id: str) -> dict[str, dict]:
    return {
        "리포트명": {"title": {}},
        "학생": {"relation": _relation(students_db_id)},
        "리포트 기간": {"date": {}},
        "학부모 발송 문구": {"rich_text": {}},
        "HTML 링크": {"url": {}},
        "승인됨": {"checkbox": {}},
        "발송 여부": {"checkbox": {}},
        "발송일시": {"date": {}},
    }


def _memo_properties(students_db_id: str) -> dict[str, dict]:
    return {
        "내용": {"title": {}},
        "학생": {"relation": _relation(students_db_id)},
        "일자": {"date": {}},
        "태그": {"select": {"options": _options(["상담", "행동", "학습", "건강"])}},
    }


def _exam_properties(students_db_id: str) -> dict[str, dict]:
    return {
        "시험명": {"title": {}},
        "학생": {"relation": _relation(students_db_id)},
        "시험일": {"date": {}},
        "반": {"rich_text": {}},
        "과목": {"rich_text": {}},
        "응시 여부": {"checkbox": {}},
        "원점수": {"number": {"format": "number"}},
        "만점": {"number": {"format": "number"}},
        "백분율": {"number": {"format": "percent"}},
        "데이터 출처": {"rich_text": {}},
        "외부 시험 ID": {"rich_text": {}},
    }


def _options(names: list[str]) -> list[dict[str, str]]:
    colors = ["blue", "yellow", "red", "green", "purple", "gray"]
    return [{"name": name, "color": colors[i % len(colors)]} for i, name in enumerate(names)]


def _relation(data_source_id: str) -> dict:
    return {
        "data_source_id": data_source_id,
        "type": "single_property",
        "single_property": {},
    }
