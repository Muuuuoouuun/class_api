from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..config import AppConfig, NeisSchoolConfig
from ..neis import NeisClient, NeisSchool, NeisScheduleEvent

EXAM_KEYWORDS = ("시험", "고사", "학력평가", "모의평가", "모의고사")
DEFAULT_KEYWORDS = (*EXAM_KEYWORDS, "방학식")
_KOR_WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")


@dataclass(frozen=True)
class SchoolLookup:
    name: str
    office_code: str = ""
    school_code: str = ""


def fetch_relevant_school_schedules(
    cfg: AppConfig,
    *,
    schools: Any = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    keywords: list[str] | str | None = None,
) -> dict[str, Any]:
    if not (cfg.neis.api_key or "").strip():
        raise ValueError("neis.api_key is required")

    start = _parse_date(start_date) or date.today()
    end = _parse_date(end_date) or (start + timedelta(days=180))
    if start > end:
        raise ValueError("start_date must be before end_date")

    lookups = parse_school_lookups(schools) or [
        SchoolLookup(
            name=school.name,
            office_code=school.office_code,
            school_code=school.school_code,
        )
        for school in cfg.neis.schools
    ]
    if not lookups:
        raise ValueError("school names or neis.schools are required")

    active_keywords = normalize_keywords(keywords) or cfg.neis.event_keywords or list(DEFAULT_KEYWORDS)
    items: list[dict[str, Any]] = []
    matched_schools: list[dict[str, str]] = []
    unmatched: list[str] = []

    with NeisClient(api_key=cfg.neis.api_key, base_url=cfg.neis.base_url) as client:
        for lookup in lookups:
            school = resolve_school(client, lookup)
            if not school:
                unmatched.append(lookup.name)
                continue
            matched_schools.append(
                {
                    "name": school.name,
                    "office_code": school.office_code,
                    "school_code": school.school_code,
                    "office_name": school.office_name,
                    "school_kind": school.school_kind,
                }
            )
            events = client.school_schedule(school=school, start=start, end=end)
            items.extend(_events_to_schedule_items(events, active_keywords))

    items.sort(key=lambda row: (row["date"], row["school_name"], row["event_name"]))
    return {
        "ok": True,
        "source": "neis",
        "items": items,
        "schools": matched_schools,
        "unmatched": unmatched,
        "keywords": active_keywords,
        "summary": {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "school_count": len(matched_schools),
            "event_count": len(items),
            "unmatched_count": len(unmatched),
        },
    }


def parse_school_lookups(value: Any) -> list[SchoolLookup]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in re.split(r"[\n;,]+", value) if part.strip()]
        return [_lookup_from_text(part) for part in parts]
    if isinstance(value, list):
        lookups: list[SchoolLookup] = []
        for item in value:
            if isinstance(item, str):
                lookups.extend(parse_school_lookups(item))
            elif isinstance(item, dict):
                lookups.append(
                    SchoolLookup(
                        name=str(item.get("name") or item.get("school_name") or "").strip(),
                        office_code=str(
                            item.get("office_code") or item.get("ATPT_OFCDC_SC_CODE") or ""
                        ).strip(),
                        school_code=str(
                            item.get("school_code") or item.get("SD_SCHUL_CODE") or ""
                        ).strip(),
                    )
                )
            elif isinstance(item, NeisSchoolConfig):
                lookups.append(
                    SchoolLookup(
                        name=item.name,
                        office_code=item.office_code,
                        school_code=item.school_code,
                    )
                )
        return [lookup for lookup in lookups if lookup.name or (lookup.office_code and lookup.school_code)]
    return []


def normalize_keywords(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[\n;,]+", value) if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


def resolve_school(client: NeisClient, lookup: SchoolLookup) -> NeisSchool | None:
    if lookup.office_code and lookup.school_code:
        return NeisSchool(
            name=lookup.name or lookup.school_code,
            office_code=lookup.office_code,
            school_code=lookup.school_code,
        )
    candidates = client.search_schools(lookup.name, limit=20)
    if not candidates:
        return None
    exact = [school for school in candidates if school.name == lookup.name]
    return exact[0] if exact else candidates[0]


def _lookup_from_text(value: str) -> SchoolLookup:
    parts = [part.strip() for part in value.split("|")]
    if len(parts) >= 3:
        return SchoolLookup(name=parts[0], office_code=parts[1], school_code=parts[2])
    return SchoolLookup(name=value.strip())


def _parse_date(value: str | date | None) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    return date.fromisoformat(text)


def _event_matches(event: NeisScheduleEvent, keywords: list[str]) -> bool:
    haystack = f"{event.event_name} {event.content}"
    return any(keyword in haystack for keyword in keywords)


def _events_to_schedule_items(
    events: list[NeisScheduleEvent],
    active_keywords: list[str],
) -> list[dict[str, Any]]:
    items = [
        _event_to_schedule_item(event)
        for event in events
        if _event_matches(event, active_keywords) and not _is_vacation_period(event)
    ]
    if _wants_vacation_start(active_keywords):
        break_ceremonies = [event for event in events if _is_break_ceremony(event)]
        items.extend(_vacation_start_items(events, break_ceremonies))
    return items


def _wants_vacation_start(active_keywords: list[str]) -> bool:
    return any(keyword in ("방학", "방학식") for keyword in active_keywords)


def _is_break_ceremony(event: NeisScheduleEvent) -> bool:
    return "방학식" in f"{event.event_name} {event.content}"


def _is_vacation_period(event: NeisScheduleEvent) -> bool:
    text = f"{event.event_name} {event.content}"
    return "방학" in text and "방학식" not in text


def _vacation_start_items(
    events: list[NeisScheduleEvent],
    break_ceremonies: list[NeisScheduleEvent],
) -> list[dict[str, Any]]:
    starts: list[NeisScheduleEvent] = []
    last_dates: dict[tuple[str, str, tuple[str, ...]], date] = {}
    vacation_events = sorted(
        (event for event in events if _is_vacation_period(event)),
        key=lambda event: (event.school_code, event.event_name, event.grades, event.date),
    )
    for event in vacation_events:
        if _has_nearby_break_ceremony(event, break_ceremonies):
            continue
        key = (event.school_code, event.event_name, event.grades)
        last_date = last_dates.get(key)
        if last_date is None or event.date > last_date + timedelta(days=1):
            starts.append(event)
        last_dates[key] = event.date
    return [
        _event_to_schedule_item(
            event,
            event_name=f"{event.event_name} 시작",
            category_override=("break_ceremony", "방학 시작"),
        )
        for event in starts
    ]


def _has_nearby_break_ceremony(
    vacation_event: NeisScheduleEvent,
    break_ceremonies: list[NeisScheduleEvent],
) -> bool:
    for ceremony in break_ceremonies:
        if ceremony.school_code != vacation_event.school_code:
            continue
        days_after_ceremony = (vacation_event.date - ceremony.date).days
        if 0 <= days_after_ceremony <= 7:
            return True
    return False


def _event_category(event: NeisScheduleEvent) -> tuple[str, str]:
    text = f"{event.event_name} {event.content}"
    if "방학식" in text:
        return "break_ceremony", "방학식"
    if "방학" in text:
        return "break_ceremony", "방학 시작"
    if any(word in text for word in EXAM_KEYWORDS):
        return "exam", "시험"
    return "event", "행사"


def _event_to_schedule_item(
    event: NeisScheduleEvent,
    *,
    event_name: str | None = None,
    category_override: tuple[str, str] | None = None,
) -> dict[str, Any]:
    category, category_label = category_override or _event_category(event)
    display_event_name = event_name or event.event_name
    grade_label = f"{','.join(event.grades)}학년" if event.grades else ""
    return {
        "id": f"neis:{event.school_code}:{event.date.isoformat()}:{display_event_name}",
        "source": "neis",
        "day": _KOR_WEEKDAYS[event.date.weekday()],
        "date": event.date.isoformat(),
        "time": event.date.isoformat(),
        "class_name": display_event_name,
        "teacher": event.school_name,
        "room": category_label,
        "school_name": event.school_name,
        "school_code": event.school_code,
        "office_code": event.office_code,
        "event_name": display_event_name,
        "event_content": event.content,
        "category": category,
        "category_label": category_label,
        "grades": list(event.grades),
        "grade_label": grade_label,
        "confidence": 1.0,
    }
