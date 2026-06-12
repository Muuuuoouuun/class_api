from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx


class NeisApiError(RuntimeError):
    """Raised when the NEIS Open API returns an error response."""


@dataclass(frozen=True)
class NeisSchool:
    name: str
    office_code: str
    school_code: str
    office_name: str = ""
    school_kind: str = ""


@dataclass(frozen=True)
class NeisScheduleEvent:
    school_name: str
    office_code: str
    school_code: str
    date: date
    event_name: str
    content: str = ""
    office_name: str = ""
    grades: tuple[str, ...] = ()


class NeisClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://open.neis.go.kr/hub",
        timeout: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self._client = http_client or httpx.Client(timeout=timeout)
        self._owns_client = http_client is None
        self._school_listing_cache: list[NeisSchool] | None = None

    def __enter__(self) -> "NeisClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search_schools(self, name: str, *, limit: int = 20) -> list[NeisSchool]:
        query = name.strip()
        if not query:
            return []
        try:
            rows = self._get_rows("schoolInfo", {"SCHUL_NM": query}, p_size=min(max(limit, 1), 100))
        except NeisApiError as exc:
            if not _is_server_error(exc):
                raise
            return self._search_school_listing(query, limit=limit)

        schools = [_school_from_row(row) for row in rows[:limit]]
        if schools:
            return schools
        return self._search_school_listing(query, limit=limit)

    def school_schedule(
        self,
        *,
        school: NeisSchool,
        start: date,
        end: date,
    ) -> list[NeisScheduleEvent]:
        if start > end:
            raise ValueError("start date must be before end date")

        params = {
            "ATPT_OFCDC_SC_CODE": school.office_code,
            "SD_SCHUL_CODE": school.school_code,
            "AA_FROM_YMD": start.strftime("%Y%m%d"),
            "AA_TO_YMD": end.strftime("%Y%m%d"),
        }
        try:
            rows = self._get_rows("SchoolSchedule", params, p_size=1000)
        except NeisApiError:
            rows = self._get_schedule_rows_by_day(school=school, start=start, end=end)
        return [_event_from_row(row, fallback_school=school) for row in rows]

    def _get_schedule_rows_by_day(
        self,
        *,
        school: NeisSchool,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        current = start
        while current <= end:
            try:
                rows.extend(
                    self._get_rows(
                        "SchoolSchedule",
                        {
                            "ATPT_OFCDC_SC_CODE": school.office_code,
                            "SD_SCHUL_CODE": school.school_code,
                            "AA_YMD": current.strftime("%Y%m%d"),
                        },
                        p_size=100,
                    )
                )
            except NeisApiError:
                pass
            current += timedelta(days=1)
        return rows

    def _search_school_listing(self, query: str, *, limit: int) -> list[NeisSchool]:
        normalized_query = _normalize_school_name(query)
        if not normalized_query:
            return []

        schools = [
            school
            for school in self._load_school_listing()
            if normalized_query in _normalize_school_name(school.name)
        ]
        schools.sort(
            key=lambda school: (
                _normalize_school_name(school.name) != normalized_query,
                len(school.name),
                school.name,
            )
        )
        return schools[:limit]

    def _load_school_listing(self) -> list[NeisSchool]:
        if self._school_listing_cache is None:
            rows = self._get_rows("schoolInfo", {}, p_size=1000)
            self._school_listing_cache = [_school_from_row(row) for row in rows]
        return self._school_listing_cache

    def _get_rows(
        self,
        endpoint: str,
        params: dict[str, Any],
        *,
        p_size: int = 100,
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise NeisApiError("NEIS api key is not configured")

        rows: list[dict[str, Any]] = []
        total: int | None = None
        p_index = 1
        while True:
            payload = {
                "KEY": self.api_key,
                "Type": "json",
                "pIndex": p_index,
                "pSize": p_size,
                **params,
            }
            response = self._client.get(f"{self.base_url}/{endpoint}", params=payload)
            response.raise_for_status()
            page_rows, page_total = _extract_neis_rows(response.json(), endpoint)
            if total is None:
                total = page_total
            rows.extend(page_rows)
            if not page_rows or total is None or len(rows) >= total or len(page_rows) < p_size:
                break
            p_index += 1
        return rows


def _extract_neis_rows(data: dict[str, Any], endpoint: str) -> tuple[list[dict[str, Any]], int | None]:
    if "RESULT" in data:
        result = data["RESULT"] or {}
        code = str(result.get("CODE") or "")
        if code == "INFO-200":
            return [], 0
        message = result.get("MESSAGE") or "NEIS API error"
        raise NeisApiError(f"{code}: {message}")

    sections = data.get(endpoint)
    if not isinstance(sections, list):
        raise NeisApiError(f"unexpected NEIS response for {endpoint}")

    total: int | None = None
    rows: list[dict[str, Any]] = []
    for section in sections:
        if "head" in section:
            for head_item in section.get("head") or []:
                if "list_total_count" in head_item:
                    total = int(head_item["list_total_count"])
                result = head_item.get("RESULT")
                if result and result.get("CODE") not in (None, "INFO-000"):
                    raise NeisApiError(f"{result.get('CODE')}: {result.get('MESSAGE')}")
        if "row" in section:
            rows.extend(section.get("row") or [])
    return rows, total


def _school_from_row(row: dict[str, Any]) -> NeisSchool:
    return NeisSchool(
        name=str(row.get("SCHUL_NM") or "").strip(),
        office_code=str(row.get("ATPT_OFCDC_SC_CODE") or "").strip(),
        school_code=str(row.get("SD_SCHUL_CODE") or "").strip(),
        office_name=str(row.get("ATPT_OFCDC_SC_NM") or "").strip(),
        school_kind=str(row.get("SCHUL_KND_SC_NM") or "").strip(),
    )


def _normalize_school_name(name: str) -> str:
    return "".join(str(name or "").lower().split())


def _is_server_error(error: NeisApiError) -> bool:
    return "ERROR-500" in str(error)


def _event_from_row(row: dict[str, Any], *, fallback_school: NeisSchool) -> NeisScheduleEvent:
    raw_date = str(row.get("AA_YMD") or "").strip()
    event_date = datetime.strptime(raw_date, "%Y%m%d").date()
    return NeisScheduleEvent(
        school_name=str(row.get("SCHUL_NM") or fallback_school.name).strip(),
        office_code=str(row.get("ATPT_OFCDC_SC_CODE") or fallback_school.office_code).strip(),
        school_code=str(row.get("SD_SCHUL_CODE") or fallback_school.school_code).strip(),
        date=event_date,
        event_name=str(row.get("EVENT_NM") or "").strip(),
        content=str(row.get("EVENT_CNTNT") or "").strip(),
        office_name=str(row.get("ATPT_OFCDC_SC_NM") or fallback_school.office_name).strip(),
        grades=_grades_from_row(row),
    )


def _grades_from_row(row: dict[str, Any]) -> tuple[str, ...]:
    grade_fields = (
        ("1", "ONE_GRADE_EVENT_YN"),
        ("2", "TW_GRADE_EVENT_YN"),
        ("3", "THREE_GRADE_EVENT_YN"),
        ("4", "FR_GRADE_EVENT_YN"),
        ("5", "FIV_GRADE_EVENT_YN"),
        ("6", "SIX_GRADE_EVENT_YN"),
    )
    grades = [grade for grade, field in grade_fields if str(row.get(field) or "").upper() == "Y"]
    return tuple(grades)
