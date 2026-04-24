"""출력 포트 (Layer 2↔4 경계).

비즈니스 로직은 HTML 을 쓸지 Notion 을 쓸지 **몰라야 한다**. config 에서 주입된
포트 구현체를 호출할 뿐.

데이터 진실원(학생 Master, 수업 기록, 메모)은 여전히 Notion. 출력 포트는
"파생 리포트" (일일 현황, 주간 리포트) 를 어디에 써낼지를 담당한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..config import AppConfig


@dataclass
class DailySnapshot:
    date: str                        # YYYY-MM-DD
    academy: str
    lessons: list[dict]              # 수업 단위 집계
    missing_homework: list[dict]     # 학생별 미제출
    attendance_rate: float           # 0~1
    generated_at: datetime


@dataclass
class WeeklyRenderInput:
    student_classin_id: str
    student_name: str
    class_name: str | None
    period_start: datetime
    period_end: datetime
    lessons: list[dict]
    prev_week_lessons: list[dict]
    summary_markdown: str            # Claude 생성
    parent_message: str              # Claude 생성 카톡 문구


@dataclass
class RenderResult:
    path: Path | None = None         # HTML 파일 경로 (html/both 모드)
    public_url: str | None = None    # Cloudflare Tunnel 노출 URL (있으면)
    notion_page_id: str | None = None  # Notion 아카이브 페이지 (승인 후)


class DailyOutput(Protocol):
    def write(self, cfg: AppConfig, snap: DailySnapshot) -> RenderResult: ...


class WeeklyOutput(Protocol):
    def write_draft(self, cfg: AppConfig, inp: WeeklyRenderInput) -> RenderResult: ...
    def approve(self, cfg: AppConfig, inp: WeeklyRenderInput, draft: RenderResult) -> RenderResult: ...


class MemoOutput(Protocol):
    def write_memo(
        self, cfg: AppConfig, *, student_classin_id: str, text: str, tag: str | None = None
    ) -> str | None: ...
