from datetime import date

import httpx

from classin_toolkit.config import AppConfig
from classin_toolkit.neis import NeisClient
from classin_toolkit.neis import NeisScheduleEvent
from classin_toolkit.neis import NeisSchool
from classin_toolkit.pipelines import neis_schedule


def _cfg() -> AppConfig:
    return AppConfig.model_validate(
        {
            "academy": {"name": "Test Academy", "timezone": "Asia/Seoul"},
            "classin": {"school_id": "sid", "secret_key": "secret"},
            "anthropic": {"api_key": "sk-ant-test"},
            "neis": {"api_key": "neis-test"},
        }
    )


def test_neis_client_parses_school_and_schedule_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/schoolInfo"):
            return httpx.Response(
                200,
                json={
                    "schoolInfo": [
                        {"head": [{"list_total_count": 1}, {"RESULT": {"CODE": "INFO-000"}}]},
                        {
                            "row": [
                                {
                                    "SCHUL_NM": "서울고등학교",
                                    "ATPT_OFCDC_SC_CODE": "B10",
                                    "ATPT_OFCDC_SC_NM": "서울특별시교육청",
                                    "SD_SCHUL_CODE": "7010000",
                                    "SCHUL_KND_SC_NM": "고등학교",
                                }
                            ]
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "SchoolSchedule": [
                    {"head": [{"list_total_count": 1}, {"RESULT": {"CODE": "INFO-000"}}]},
                    {
                        "row": [
                            {
                                "SCHUL_NM": "서울고등학교",
                                "ATPT_OFCDC_SC_CODE": "B10",
                                "SD_SCHUL_CODE": "7010000",
                                "AA_YMD": "20260706",
                                "EVENT_NM": "1학기 기말고사",
                                "EVENT_CNTNT": "전학년",
                                "ONE_GRADE_EVENT_YN": "Y",
                            }
                        ]
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = NeisClient(api_key="key", http_client=http_client)
        schools = client.search_schools("서울고등학교")
        events = client.school_schedule(
            school=schools[0],
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )

    assert schools[0].office_code == "B10"
    assert schools[0].school_code == "7010000"
    assert events[0].event_name == "1학기 기말고사"
    assert events[0].date.isoformat() == "2026-07-06"
    assert events[0].grades == ("1",)


def test_neis_client_falls_back_to_school_listing_when_name_search_errors() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/schoolInfo") and "SCHUL_NM" in request.url.params:
            calls.append("name")
            return httpx.Response(
                200,
                json={"RESULT": {"CODE": "ERROR-500", "MESSAGE": "server error"}},
            )

        calls.append("listing")
        return httpx.Response(
            200,
            json={
                "schoolInfo": [
                    {"head": [{"list_total_count": 2}, {"RESULT": {"CODE": "INFO-000"}}]},
                    {
                        "row": [
                            {
                                "SCHUL_NM": "Beta Middle School",
                                "ATPT_OFCDC_SC_CODE": "B10",
                                "SD_SCHUL_CODE": "7010001",
                            },
                            {
                                "SCHUL_NM": "Alpha High School",
                                "ATPT_OFCDC_SC_CODE": "B10",
                                "SD_SCHUL_CODE": "7010000",
                            },
                        ]
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = NeisClient(api_key="key", http_client=http_client)
        schools = client.search_schools("Alpha High")

    assert calls == ["name", "listing"]
    assert [school.name for school in schools] == ["Alpha High School"]
    assert schools[0].school_code == "7010000"


def test_fetch_relevant_school_schedules_filters_exam_and_break_events(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def search_schools(self, name, *, limit=20):
            return [NeisSchool(name=name, office_code="B10", school_code="7010000")]

        def school_schedule(self, *, school, start, end):
            return [
                NeisScheduleEvent(
                    school_name=school.name,
                    office_code="B10",
                    school_code="7010000",
                    date=date(2026, 7, 6),
                    event_name="1학기 기말고사",
                ),
                NeisScheduleEvent(
                    school_name=school.name,
                    office_code="B10",
                    school_code="7010000",
                    date=date(2026, 7, 24),
                    event_name="여름방학식",
                ),
                NeisScheduleEvent(
                    school_name=school.name,
                    office_code="B10",
                    school_code="7010000",
                    date=date(2026, 7, 25),
                    event_name="여름방학",
                ),
            ]

    monkeypatch.setattr(neis_schedule, "NeisClient", FakeClient)

    result = neis_schedule.fetch_relevant_school_schedules(
        _cfg(),
        schools="서울고등학교",
        start_date="2026-07-01",
        end_date="2026-07-31",
    )

    assert result["summary"]["event_count"] == 2
    assert [item["category"] for item in result["items"]] == ["exam", "break_ceremony"]
    assert result["items"][0]["time"] == "2026-07-06"
    assert result["items"][0]["school_name"] == "서울고등학교"


def test_fetch_relevant_school_schedules_derives_vacation_start_without_ceremony(
    monkeypatch,
) -> None:
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def search_schools(self, name, *, limit=20):
            return [NeisSchool(name=name, office_code="J10", school_code="7530178")]

        def school_schedule(self, *, school, start, end):
            return [
                NeisScheduleEvent(
                    school_name=school.name,
                    office_code="J10",
                    school_code="7530178",
                    date=date(2026, 7, 18),
                    event_name="여름방학",
                ),
                NeisScheduleEvent(
                    school_name=school.name,
                    office_code="J10",
                    school_code="7530178",
                    date=date(2026, 7, 19),
                    event_name="여름방학",
                ),
                NeisScheduleEvent(
                    school_name=school.name,
                    office_code="J10",
                    school_code="7530178",
                    date=date(2026, 11, 19),
                    event_name="대학수학능력시험",
                ),
            ]

    monkeypatch.setattr(neis_schedule, "NeisClient", FakeClient)

    result = neis_schedule.fetch_relevant_school_schedules(
        _cfg(),
        schools="신성고등학교|J10|7530178",
        start_date="2026-07-01",
        end_date="2026-12-31",
    )

    assert [item["event_name"] for item in result["items"]] == [
        "여름방학 시작",
        "대학수학능력시험",
    ]
    assert result["items"][0]["category"] == "break_ceremony"
