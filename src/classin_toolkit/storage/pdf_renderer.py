from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


@dataclass(frozen=True)
class LearningReportPdf:
    path: Path
    filename: str


def render_learning_report_pdf(
    *,
    output_dir: Path,
    academy_name: str,
    payload: dict[str, Any] | None = None,
) -> LearningReportPdf:
    data = payload or {}
    output_dir.mkdir(parents=True, exist_ok=True)
    target_date = date.today()
    student_name = _clean(data.get("student_name") or "김도윤")
    class_name = _clean(data.get("class_name") or "중2 수학 심화 A")
    filename = _safe_filename(
        f"learning_report_{student_name}_{target_date.isoformat()}.pdf"
    )
    path = output_dir / filename

    font = _register_korean_font()
    bold_font = _register_korean_font(bold=True) or font
    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4

    green = colors.HexColor("#16a34a")
    dark = colors.HexColor("#142018")
    muted = colors.HexColor("#65736a")
    line = colors.HexColor("#e8eee6")
    pale = colors.HexColor("#f7faf6")
    warn = colors.HexColor("#f59e0b")

    c.setFillColor(pale)
    c.rect(0, 0, width, height, fill=1, stroke=0)
    margin = 18 * mm
    card_x = margin
    card_y = margin
    card_w = width - margin * 2
    card_h = height - margin * 2
    _round_rect(c, card_x, card_y, card_w, card_h, 10, colors.white, line)

    top = card_y + card_h - 22 * mm
    c.setFillColor(dark)
    c.setFont(bold_font, 20)
    c.drawString(card_x + 16 * mm, top, f"{student_name} 학생 학습 리포트")
    c.setFont(font, 9.5)
    c.setFillColor(muted)
    c.drawString(card_x + 16 * mm, top - 7 * mm, f"{class_name} · {target_date.isoformat()}")

    c.setFillColor(green)
    c.setFont(bold_font, 11)
    brand = _clean(academy_name or "ClassIn Toolkit")
    c.drawRightString(card_x + card_w - 16 * mm, top, brand)
    c.setStrokeColor(green)
    c.setLineWidth(2.2)
    c.line(card_x + 16 * mm, top - 14 * mm, card_x + card_w - 16 * mm, top - 14 * mm)

    metric_y = top - 34 * mm
    metrics = [
        ("출석률", "96%", "14/15회"),
        ("숙제 제출", "88%", "목표 90%"),
        ("평균 점수", "84점", "+6점 전월"),
        ("반 내 순위", "상위 22%", "총 18명 중"),
    ]
    gap = 7 * mm
    metric_w = (card_w - 32 * mm - gap * 3) / 4
    for idx, (label, value, sub) in enumerate(metrics):
        x = card_x + 16 * mm + idx * (metric_w + gap)
        _round_rect(c, x, metric_y, metric_w, 29 * mm, 7, colors.white, line)
        c.setFillColor(muted)
        c.setFont(font, 8)
        c.drawString(x + 5 * mm, metric_y + 20 * mm, label)
        c.setFillColor(green)
        c.setFont(bold_font, 16)
        c.drawString(x + 5 * mm, metric_y + 11 * mm, value)
        c.setFillColor(muted)
        c.setFont(font, 7.5)
        c.drawString(x + 5 * mm, metric_y + 5 * mm, sub)

    section_y = metric_y - 20 * mm
    _section_title(c, "단원평가 · 시험 성적", card_x + 16 * mm, section_y, font, muted)
    bars = [
        ("단원평가 1", 72, 68),
        ("OMR 모의 #1", 81, 74),
        ("단원평가 2", 88, 71),
        ("OMR 모의 #2", 84, 76),
        ("종합평가", 92, 78),
    ]
    y = section_y - 10 * mm
    for label, score, avg in bars:
        c.setFillColor(dark)
        c.setFont(font, 8.5)
        c.drawString(card_x + 16 * mm, y + 2 * mm, label)
        c.setFillColor(muted)
        c.drawRightString(card_x + card_w - 16 * mm, y + 2 * mm, f"{score}점 · 반평균 {avg}")
        bar_x = card_x + 16 * mm
        bar_y = y - 3 * mm
        bar_w = card_w - 32 * mm
        c.setFillColor(colors.HexColor("#f1f5f0"))
        c.roundRect(bar_x, bar_y, bar_w, 4, 2, fill=1, stroke=0)
        c.setFillColor(green)
        c.roundRect(bar_x, bar_y, bar_w * score / 100, 4, 2, fill=1, stroke=0)
        c.setFillColor(warn)
        c.rect(bar_x + bar_w * avg / 100, bar_y - 1, 1.5, 6, fill=1, stroke=0)
        y -= 13 * mm

    strengths_y = y - 5 * mm
    _section_title(c, "강점 · 보완", card_x + 16 * mm, strengths_y, font, muted)
    tag_y = strengths_y - 15 * mm
    _tag(c, card_x + 16 * mm, tag_y, "도형의 성질", green, font)
    _tag(c, card_x + 46 * mm, tag_y, "방정식 응용", green, font)
    _tag(c, card_x + 90 * mm, tag_y, "부등식 영역", warn, font)
    _tag(c, card_x + 122 * mm, tag_y, "확률", warn, font)

    comment = _clean(
        data.get("comment")
        or "기본 개념은 안정적입니다. 부등식 영역에서 부호 처리에 실수가 반복돼 다음 주 보충 권장."
    )
    comment_y = tag_y - 18 * mm
    _section_title(c, "강사 코멘트", card_x + 16 * mm, comment_y, font, muted)
    note_x = card_x + 16 * mm
    note_y = comment_y - 38 * mm
    note_w = card_w - 32 * mm
    _round_rect(c, note_x, note_y, note_w, 30 * mm, 6, pale, line)
    c.setFillColor(dark)
    c.setFont(font, 9.5)
    for idx, line_text in enumerate(_wrap(comment, font, 9.5, note_w - 12 * mm)[:4]):
        c.drawString(note_x + 6 * mm, note_y + 21 * mm - idx * 5.5 * mm, line_text)

    c.setFillColor(muted)
    c.setFont(font, 7.5)
    c.drawRightString(
        card_x + card_w - 16 * mm,
        card_y + 9 * mm,
        "ClassIn Toolkit · 자동 생성 PDF",
    )
    c.save()
    return LearningReportPdf(path=path, filename=filename)


def _register_korean_font(*, bold: bool = False) -> str:
    name = "MalgunGothicBold" if bold else "MalgunGothic"
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    candidates = (
        Path("C:/Windows/Fonts/malgunbd.ttf") if bold else Path("C:/Windows/Fonts/malgun.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    )
    for path in candidates:
        if path.exists():
            pdfmetrics.registerFont(TTFont(name, str(path)))
            return name
    cid_name = "HYSMyeongJo-Medium"
    if cid_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(cid_name))
    return cid_name


def _section_title(c: canvas.Canvas, text: str, x: float, y: float, font: str, color) -> None:
    c.setFillColor(color)
    c.setFont(font, 9)
    c.drawString(x, y, text)


def _tag(c: canvas.Canvas, x: float, y: float, text: str, color, font: str) -> None:
    w = pdfmetrics.stringWidth(text, font, 8.5) + 10 * mm
    bg = colors.Color(color.red, color.green, color.blue, alpha=0.12)
    _round_rect(c, x, y, w, 8 * mm, 4, bg, colors.white)
    c.setFillColor(color)
    c.setFont(font, 8.5)
    c.drawString(x + 5 * mm, y + 2.5 * mm, text)


def _round_rect(c: canvas.Canvas, x: float, y: float, w: float, h: float, r: float, fill, stroke) -> None:
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(0.8)
    c.roundRect(x, y, w, h, r, fill=1, stroke=1)


def _wrap(text: str, font: str, size: float, max_width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text).splitlines() or [""]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and pdfmetrics.stringWidth(candidate, font, size) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines or [""]


def _clean(value: Any) -> str:
    return str(value or "").replace("\r", "").strip()


def _safe_filename(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "_", value)
    value = re.sub(r"\s+", "_", value.strip())
    return value or "learning_report.pdf"
