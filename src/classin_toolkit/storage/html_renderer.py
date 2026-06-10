"""HTML 리포트 렌더러 (DailyOutput / WeeklyOutput 구현).

지침(feedback_storage_notion, 2026-04-24):
- 매일 바뀌는 일일 현황은 Notion 푸시 대신 HTML 정적 파일로 — 토큰 낭비·rate limit 회피.
- 모바일 카톡 링크에서 즉시 열림 (Cloudflare Tunnel 공개 URL 기반).
- 주간 리포트는 초안(draft) HTML 먼저. Notion 아카이브는 승인 후 별도 단계.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import AppConfig
from .output_port import DailySnapshot, RenderResult, WeeklyRenderInput

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass
class _Env:
    env: Environment


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


class HtmlDailyRenderer:
    def write(self, cfg: AppConfig, snap: DailySnapshot) -> RenderResult:
        out_dir = Path(cfg.output.daily.path)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{snap.date}.html"

        html = _env().get_template("daily.html").render(
            title=f"일일 현황 {snap.date}",
            academy=snap.academy,
            meta_line=f"{snap.date} 자동 생성",
            generated_at=snap.generated_at.strftime("%Y-%m-%d %H:%M"),
            lessons=snap.lessons,
            missing_homework=snap.missing_homework,
            attendance_rate=snap.attendance_rate,
        )
        path.write_text(html, encoding="utf-8")
        log.info("daily html rendered: %s", path)

        return RenderResult(path=path, public_url=_public_url(cfg, "daily", path.name))


class HtmlWeeklyRenderer:
    def write_draft(self, cfg: AppConfig, inp: WeeklyRenderInput) -> RenderResult:
        out_dir = Path(cfg.output.weekly.path)
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = _weekly_filename(inp)
        path = out_dir / filename

        att_rate = _attendance_rate(inp.lessons)
        hw_rate = _homework_rate(inp.lessons)
        summary_html = _md_to_html(inp.summary_markdown)

        html = _env().get_template("weekly.html").render(
            title=f"{inp.student_name} 주간 리포트",
            academy=_academy_from(cfg),
            meta_line=(
                f"{inp.period_start.date()} ~ {inp.period_end.date()} · "
                f"{inp.class_name or '반 미지정'}"
            ),
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            lessons=inp.lessons,
            exam_results=inp.exam_results or [],
            attendance_rate=att_rate,
            hw_rate=hw_rate,
            summary_html=summary_html,
            parent_message=inp.parent_message,
        )
        path.write_text(html, encoding="utf-8")
        log.info("weekly draft html: %s", path)
        return RenderResult(path=path, public_url=_public_url(cfg, "weekly", filename))

    def approve(self, cfg: AppConfig, inp: WeeklyRenderInput, draft: RenderResult) -> RenderResult:
        # HTML 만 쓰는 모드면 approve 는 no-op (이미 파일 존재)
        # Notion 아카이브가 필요한 모드는 pipelines.weekly 가 NotionRepo 를 직접 호출
        return draft


def _weekly_filename(inp: WeeklyRenderInput) -> str:
    slug = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", inp.student_name) or inp.student_classin_id
    return f"{inp.period_start.date()}_{slug}.html"


def _attendance_rate(lessons: list[dict]) -> float:
    if not lessons:
        return 0.0
    present = sum(1 for lesson in lessons if lesson.get("attendance") in ("출석", "지각"))
    return present / len(lessons)


def _homework_rate(lessons: list[dict]) -> float:
    considered = [lesson for lesson in lessons if lesson.get("homework_submitted") is not None]
    if not considered:
        return 0.0
    submitted = sum(1 for lesson in considered if lesson.get("homework_submitted"))
    return submitted / len(considered)


def _md_to_html(md: str) -> str:
    """최소 Markdown 지원 — ## 헤더, -/* 리스트, 빈 줄 분리."""
    from markupsafe import escape

    lines = md.splitlines()
    out: list[str] = []
    in_ul = False

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for raw in lines:
        line = raw.rstrip()
        if not line:
            close_ul()
            continue
        if line.startswith("## "):
            close_ul()
            out.append(f"<h3>{escape(line[3:])}</h3>")
        elif line.startswith(("- ", "* ")):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{escape(line[2:])}</li>")
        else:
            close_ul()
            out.append(f"<p>{escape(line)}</p>")
    close_ul()
    return "\n".join(out)


def _public_url(cfg: AppConfig, kind: str, filename: str) -> str | None:
    if kind == "daily":
        base = cfg.output.daily.public_url_base
    else:
        base = cfg.output.daily.public_url_base  # 같은 tunnel 공유
    if not base:
        return None
    return f"{base.rstrip('/')}/{kind}/{filename}"


def _academy_from(cfg: AppConfig) -> str:
    return cfg.academy.name
