"""Local report/offline data merge helpers for the teacher dashboard.

This pipeline is deliberately read-only. It turns weekly draft indexes and
optional local shared files into compact per-student context without mutating
the source files.
"""
from __future__ import annotations

import csv
import json
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ..config import AppConfig


@dataclass(frozen=True)
class KnownStudent:
    classin_id: str
    name: str
    class_name: str = ""


@dataclass
class MergeItem:
    kind: str
    source: str
    student_classin_id: str = ""
    student_name: str = ""
    class_name: str = ""
    date: str = ""
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MergeResult:
    contexts: dict[str, dict[str, Any]]
    needs_review_items: list[dict[str, Any]]
    summary: dict[str, int]


def build_report_contexts(
    cfg: AppConfig,
    students: list[dict[str, Any]],
    *,
    inbox_dir: str | Path | None = None,
) -> MergeResult:
    """Build compact per-student report/offline context for dashboard rows."""

    known = _known_students(students)
    contexts = {student.classin_id: _empty_context() for student in known}
    by_id = {student.classin_id: student for student in known}
    needs_review: list[dict[str, Any]] = []

    items = [
        *_weekly_report_items(Path(cfg.output.weekly.path)),
        *_local_inbox_items(Path(inbox_dir) if inbox_dir else Path("local_data/inbox")),
    ]
    for item in items:
        student_id = _match_student(item, known)
        if not student_id:
            needs_review.append(_review_item(item))
            continue
        if student_id not in contexts:
            contexts[student_id] = _empty_context()
        _apply_item(contexts[student_id], item, by_id.get(student_id))

    for context in contexts.values():
        _finalize_context(context)

    summary = {
        "students_with_context": sum(1 for context in contexts.values() if context["has_context"]),
        "weekly_reports": sum(1 for context in contexts.values() if context["weekly_report"]["status"]),
        "offline_attendance": sum(context["offline_attendance"] for context in contexts.values()),
        "offline_scores": sum(context["offline_scores"] for context in contexts.values()),
        "memos": sum(context["memos"] for context in contexts.values()),
        "attachments": sum(context["attachments"] for context in contexts.values()),
        "needs_review": len(needs_review),
    }
    return MergeResult(contexts=contexts, needs_review_items=needs_review, summary=summary)


def _known_students(rows: list[dict[str, Any]]) -> list[KnownStudent]:
    seen: set[str] = set()
    students: list[KnownStudent] = []
    for row in rows:
        classin_id = _clean(row.get("student_classin_id"))
        if not classin_id or classin_id in seen:
            continue
        seen.add(classin_id)
        students.append(
            KnownStudent(
                classin_id=classin_id,
                name=_clean(row.get("student_name")),
                class_name=_clean(row.get("student_class_name") or row.get("class_name")),
            )
        )
    return students


def _empty_context() -> dict[str, Any]:
    return {
        "has_context": False,
        "weekly_report": {
            "status": "",
            "period_start": "",
            "period_end": "",
            "html_path": "",
            "public_url": "",
            "approved": False,
        },
        "offline_attendance": 0,
        "offline_scores": 0,
        "memos": 0,
        "attachments": 0,
        "badges": [],
        "summary": "",
        "sources": [],
    }


def _weekly_report_items(weekly_dir: Path) -> list[MergeItem]:
    if not weekly_dir.exists():
        return []
    items: list[MergeItem] = []
    for path in sorted(weekly_dir.glob("*_drafts.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, list):
            continue
        for record in raw:
            if not isinstance(record, dict):
                continue
            approved = bool(record.get("approved"))
            items.append(
                MergeItem(
                    kind="weekly_report",
                    source=str(path),
                    student_classin_id=_clean(record.get("student_classin_id")),
                    student_name=_clean(record.get("student_name")),
                    date=_clean(record.get("period_start")),
                    detail="승인됨" if approved else "초안",
                    metadata={
                        "status": "approved" if approved else "draft_ready",
                        "period_start": _clean(record.get("period_start")),
                        "period_end": _clean(record.get("period_end")),
                        "html_path": _clean(record.get("html_path")),
                        "public_url": _clean(record.get("public_url")),
                        "approved": approved,
                    },
                )
            )
    return items


def _local_inbox_items(inbox_dir: Path) -> list[MergeItem]:
    if not inbox_dir.exists():
        return []
    return [
        *_attendance_items(inbox_dir / "attendance"),
        *_score_items(inbox_dir / "scores"),
        *_memo_items(inbox_dir / "memos"),
        *_attachment_items(inbox_dir / "attachments"),
    ]


def _attendance_items(path: Path) -> list[MergeItem]:
    items: list[MergeItem] = []
    for csv_path in sorted(path.glob("*.csv")) if path.exists() else []:
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
                rows = list(csv.DictReader(fp))
        except OSError:
            continue
        for row in rows:
            status = _pick(row, "attendance", "출석", "출결", "status")
            items.append(
                MergeItem(
                    kind="offline_attendance",
                    source=str(csv_path),
                    student_classin_id=_pick(row, "student_classin_id", "classin_id", "ClassIn ID"),
                    student_name=_pick(row, "student_name", "학생명", "이름"),
                    class_name=_pick(row, "class_name", "반", "class"),
                    date=_pick(row, "date", "일자", "수업일"),
                    detail=status or "오프라인 출결",
                    metadata={"attendance": status},
                )
            )
    return items


def _score_items(path: Path) -> list[MergeItem]:
    items: list[MergeItem] = []
    for xlsx_path in sorted(path.glob("*.xlsx")) if path.exists() else []:
        try:
            rows = _xlsx_dict_rows(xlsx_path)
        except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile):
            continue
        for row in rows:
            score = _pick(row, "score", "점수", "성적")
            subject = _pick(row, "subject", "과목", "시험")
            detail = " ".join(part for part in (subject, score) if part) or "오프라인 성적"
            items.append(
                MergeItem(
                    kind="offline_score",
                    source=str(xlsx_path),
                    student_classin_id=_pick(row, "student_classin_id", "classin_id", "ClassIn ID"),
                    student_name=_pick(row, "student_name", "학생명", "이름"),
                    class_name=_pick(row, "class_name", "반", "class"),
                    date=_pick(row, "date", "일자", "시험일"),
                    detail=detail,
                    metadata={"score": score, "subject": subject},
                )
            )
    return items


def _memo_items(path: Path) -> list[MergeItem]:
    items: list[MergeItem] = []
    for memo_path in sorted(path.glob("*.md")) if path.exists() else []:
        try:
            text = memo_path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _memo_metadata(text)
        classin_id = _clean(meta.get("student_classin_id") or meta.get("classin_id"))
        if not classin_id:
            classin_id = _id_from_filename(memo_path)
        items.append(
            MergeItem(
                kind="memo",
                source=str(memo_path),
                student_classin_id=classin_id,
                student_name=_clean(meta.get("student_name") or meta.get("student")),
                class_name=_clean(meta.get("class_name") or meta.get("class")),
                date=_clean(meta.get("date")),
                detail=_memo_excerpt(text),
                metadata={"title": memo_path.stem},
            )
        )
    return items


def _attachment_items(path: Path) -> list[MergeItem]:
    items: list[MergeItem] = []
    if not path.exists():
        return items
    for attachment_path in sorted(item for item in path.glob("*") if item.is_file()):
        if attachment_path.suffix.lower() == ".json":
            continue
        meta = _attachment_metadata(attachment_path)
        classin_id = _clean(meta.get("student_classin_id") or meta.get("classin_id"))
        if not classin_id:
            classin_id = _id_from_filename(attachment_path)
        title = _clean(meta.get("detail") or meta.get("title")) or attachment_path.name
        items.append(
            MergeItem(
                kind="attachment",
                source=str(attachment_path),
                student_classin_id=classin_id,
                student_name=_clean(meta.get("student_name") or meta.get("student")),
                class_name=_clean(meta.get("class_name") or meta.get("class")),
                date=_clean(meta.get("date")),
                detail=f"공유 자료: {title}",
                metadata={
                    "filename": attachment_path.name,
                    "extension": attachment_path.suffix.lower(),
                },
            )
        )
    return items


def _match_student(item: MergeItem, known: list[KnownStudent]) -> str:
    if item.student_classin_id:
        for student in known:
            if student.classin_id == item.student_classin_id:
                return student.classin_id
        return ""
    if item.student_name and item.class_name:
        matched = [
            student
            for student in known
            if _norm(student.name) == _norm(item.student_name)
            and _norm(student.class_name) == _norm(item.class_name)
        ]
        if len(matched) == 1:
            return matched[0].classin_id
    return ""


def _apply_item(
    context: dict[str, Any],
    item: MergeItem,
    student: KnownStudent | None,
) -> None:
    context["has_context"] = True
    if item.kind == "weekly_report":
        current = context["weekly_report"]
        if not current["period_start"] or item.metadata.get("period_start", "") >= current["period_start"]:
            context["weekly_report"] = {
                "status": item.metadata.get("status", ""),
                "period_start": item.metadata.get("period_start", ""),
                "period_end": item.metadata.get("period_end", ""),
                "html_path": item.metadata.get("html_path", ""),
                "public_url": item.metadata.get("public_url", ""),
                "approved": bool(item.metadata.get("approved")),
            }
    elif item.kind == "offline_attendance":
        context["offline_attendance"] += 1
    elif item.kind == "offline_score":
        context["offline_scores"] += 1
    elif item.kind == "memo":
        context["memos"] += 1
    elif item.kind == "attachment":
        context["attachments"] += 1

    context["sources"].append(
        {
            "kind": item.kind,
            "source": item.source,
            "date": item.date,
            "detail": item.detail,
            "student": student.name if student else item.student_name,
        }
    )


def _finalize_context(context: dict[str, Any]) -> None:
    badges: list[str] = []
    weekly = context["weekly_report"]
    if weekly["status"] == "approved":
        badges.append("리포트 승인됨")
    elif weekly["status"] == "draft_ready":
        badges.append("리포트 초안")
    if context["offline_attendance"]:
        badges.append(f"오프라인 출결 {context['offline_attendance']}건")
    if context["offline_scores"]:
        badges.append(f"오프라인 시험 {context['offline_scores']}건")
    if context["memos"]:
        badges.append(f"상담 메모 {context['memos']}건")
    if context["attachments"]:
        badges.append(f"공유 자료 {context['attachments']}건")
    context["badges"] = badges
    context["summary"] = " · ".join(badges[:3])


def _review_item(item: MergeItem) -> dict[str, Any]:
    data = asdict(item)
    data["reason"] = "학생 자동 매칭 필요"
    return data


def _pick(row: dict[str, Any], *keys: str) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        if key in row:
            return _clean(row.get(key))
        value = lowered.get(key.lower())
        if value is not None:
            return _clean(value)
    return ""


def _memo_metadata(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in text.splitlines()[:12]:
        if not line.strip():
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace("-", "_")
        if key in {"student_classin_id", "classin_id", "student_name", "student", "class_name", "class", "date"}:
            meta[key] = value.strip()
    return meta


def _memo_excerpt(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or ":" in stripped[:32]:
            continue
        return stripped[:120]
    return "상담 메모"


def _attachment_metadata(path: Path) -> dict[str, Any]:
    candidates = [path.with_suffix(path.suffix + ".json"), path.with_suffix(".json")]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict):
            return raw
    return {}


def _id_from_filename(path: Path) -> str:
    match = re.search(r"(?<!\d)\d{4,}(?!\d)", path.stem)
    return match.group(0) if match else ""


def _xlsx_dict_rows(path: Path) -> list[dict[str, str]]:
    rows = _xlsx_rows(path)
    if not rows:
        return []
    headers = [_clean(value) for value in rows[0]]
    out: list[dict[str, str]] = []
    for values in rows[1:]:
        if not any(_clean(value) for value in values):
            continue
        out.append({headers[i]: _clean(values[i]) if i < len(values) else "" for i in range(len(headers))})
    return out


def _xlsx_rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as zf:
        shared = _xlsx_shared_strings(zf)
        sheet_name = _first_sheet_name(zf)
        root = ET.fromstring(zf.read(sheet_name))
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", ns):
        values: list[str] = []
        for cell in row.findall("x:c", ns):
            col_index = _column_index(cell.attrib.get("r", ""))
            while len(values) < col_index:
                values.append("")
            values.append(_cell_value(cell, shared, ns))
        rows.append(values)
    return rows


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings: list[str] = []
    for item in root.findall("x:si", ns):
        strings.append("".join(node.text or "" for node in item.findall(".//x:t", ns)))
    return strings


def _first_sheet_name(zf: zipfile.ZipFile) -> str:
    if "xl/worksheets/sheet1.xml" in zf.namelist():
        return "xl/worksheets/sheet1.xml"
    for name in zf.namelist():
        if name.startswith("xl/worksheets/") and name.endswith(".xml"):
            return name
    raise KeyError("no worksheet xml found")


def _cell_value(cell: ET.Element, shared: list[str], ns: dict[str, str]) -> str:
    if cell.attrib.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//x:t", ns))
    raw = cell.findtext("x:v", default="", namespaces=ns)
    if cell.attrib.get("t") == "s" and raw:
        index = int(float(raw))
        return shared[index] if index < len(shared) else ""
    return raw


def _column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    if not letters:
        return 0
    total = 0
    for letter in letters:
        total = total * 26 + (ord(letter) - ord("A") + 1)
    return total - 1


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()
