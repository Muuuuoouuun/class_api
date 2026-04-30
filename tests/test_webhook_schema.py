import json
from pathlib import Path

from classin_toolkit.classin.webhook_schemas import (
    AttendanceEvent,
    EndEvent,
    HomeworkSubmitEvent,
    parse_event,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _load(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def test_parse_attendance() -> None:
    event = parse_event(_load("attendance_sample.json"))
    assert isinstance(event, AttendanceEvent)
    assert event.class_id == "2362301"
    assert event.course_id == "132323"
    assert event.ClassName == "수1 - 지수함수 1"
    assert len(event.Data) == 5
    absent = [m for m in event.Data if m.AttendanceTime == 0]
    assert [m.Uid for m in absent] == [10005]


def test_parse_homework_submit() -> None:
    event = parse_event(_load("homework_submit_sample.json"))
    assert isinstance(event, HomeworkSubmitEvent)
    assert event.Data.ActivityId == 99001
    assert event.Data.StudentInfo is not None
    assert event.Data.StudentInfo.Uid == 10001


def test_parse_homework_submit_official_lms_student_keys() -> None:
    event = parse_event(
        {
            "SID": 1,
            "Cmd": "HomeworkSubmit",
            "CourseID": 132323,
            "Data": {
                "ActivityId": 99001,
                "StudentInfo": {
                    "StudentUid": 10001,
                    "StudentName": "박성실",
                    "StudentAccount": "010-0000-0001",
                },
                "TeacherInfo": {
                    "TeacherUid": 20001,
                    "TeacherName": "김선생",
                    "TeacherAccount": "teacher@demo.kr",
                },
            },
        }
    )
    assert isinstance(event, HomeworkSubmitEvent)
    assert event.class_id is None
    assert event.Data.StudentInfo is not None
    assert event.Data.StudentInfo.Uid == 10001
    assert event.Data.StudentInfo.Name == "박성실"
    assert event.Data.TeacherInfo is not None
    assert event.Data.TeacherInfo.Uid == 20001


def test_parse_end_summary_aggregations() -> None:
    event = parse_event(_load("end_summary_sample.json"))
    assert isinstance(event, EndEvent)
    # handsupEnd totals across all uids: 6+1+0+9+0 = 16
    assert event.hand_raise_total() == 16
    assert event.hand_raise_by_uid()["10004"] == 9
    # awardEnd totals: 2+0+0+4+0 = 6
    assert event.trophy_total() == 6
    assert event.trophy_by_uid()["10001"] == 2
    assert event.poll_by_uid()["10003"] == 1
    cam = event.camera_minutes_by_uid()
    assert cam["10001"] == 7110 / 60
    assert cam["10005"] == 0.0


def test_unknown_cmd_is_generic() -> None:
    event = parse_event({"SID": 1, "Cmd": "ChatContent", "ClassID": 1, "Data": {}})
    assert event.Cmd == "ChatContent"
