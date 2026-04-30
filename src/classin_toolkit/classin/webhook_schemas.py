"""ClassIn Datasub Webhook 이벤트 스키마 — Cmd 디스패치 유니온.

출처: docs.eeo.cn datasub/classrelated.html, datasub/coursedata.html, datasub/publicfield.html

## 공통 필드
모든 이벤트는 최상위에 다음 공통 필드를 가진다 (대소문자/존재 여부는 Cmd별 상이):
- `SID`         : School ID
- `Cmd`         : 이벤트 디스크리미네이터
- `ClassID`     : Lesson(수업) ID
- `CourseID`    : Course(반) ID — 일부 Cmd는 누락
- `ActionTime`  : 이벤트 발생 시각 (초)
- `TimeStamp`   : 전송 시각 (초)
- `SafeKey`     : 서명 (수신자 검증)
- `Data`        : Cmd별 페이로드

## After-Class 주요 Cmd (MVP 범위)
- `Attendance`              : 출석 이벤트 (수업 종료 요약)
- `End`                     : 수업 교실 요약 (handsupEnd/awardEnd/inoutEnd 중첩)
- `HomeworkSubmit`          : 숙제 제출 (학생별)
- `HomeworkScore`           : 숙제 채점 (학생별)
- `ExamScore`               : 시험 점수
- `Rating`                  : 교사↔학생 상호평가
- `Record`, `Upload`        : 녹화본 URL
- `ChatContent`             : 채팅 로그 zip URL

Pydantic 모델은 원본 필드명(PascalCase)을 그대로 사용하되, alias 로 camelCase도 허용.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# -------------- common base --------------


class _BaseEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    SID: int | str
    Cmd: str
    ClassID: int | None = None
    CourseID: int | None = None
    ActionTime: int | None = None
    TimeStamp: int | None = None
    SafeKey: str | None = None

    @property
    def sid(self) -> str:
        return str(self.SID)

    @property
    def class_id(self) -> str | None:
        return str(self.ClassID) if self.ClassID is not None else None

    @property
    def course_id(self) -> str | None:
        return str(self.CourseID) if self.CourseID is not None else None


# -------------- Attendance --------------


class AttendanceMember(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    Uid: int
    Name: str | None = None
    Identity: int | None = None  # 1=학생, 2=교사, etc.
    AttendanceTime: int = 0  # 초 단위 실참여 시간
    FirstInTime: int | None = None
    LastOutTime: int | None = None
    FirstInDevice: int | None = None
    LastOutReason: int | None = None


class AttendanceEvent(_BaseEvent):
    Cmd: Literal["Attendance"]
    ClassName: str | None = None
    CourseName: str | None = None
    ClassStartTime: int | None = None
    ClassEndTime: int | None = None
    ActivityID: int | None = None
    AttendanceStudentNum: int | None = None
    ClassStudentNum: int | None = None
    AttendanceFlag: int | None = None
    Data: list[AttendanceMember] = Field(default_factory=list)


# -------------- End (class summary) --------------


class EndEvent(_BaseEvent):
    Cmd: Literal["End"]
    CloseTime: int | None = None
    RealCloseTime: int | None = None
    StartTime: int | None = None
    Data: dict[str, Any] = Field(default_factory=dict)

    def hand_raise_total(self) -> int:
        by_uid = self.hand_raise_by_uid()
        if by_uid:
            return sum(by_uid.values())
        return sum(int(v) for v in _iter_numbers(self.Data.get("handsupEnd") or {}))

    def hand_raise_by_uid(self) -> dict[str, int]:
        return _numbers_by_uid(
            self.Data.get("handsupEnd"),
            map_keys=("totalByUid", "handsupByUid", "handRaiseByUid"),
            value_keys=("HandsUp", "handsup", "Count", "count", "Total", "total"),
        )

    def trophy_total(self) -> int:
        by_uid = self.trophy_by_uid()
        if by_uid:
            return sum(by_uid.values())
        return sum(int(v) for v in _iter_numbers(self.Data.get("awardEnd") or {}))

    def trophy_by_uid(self) -> dict[str, int]:
        return _numbers_by_uid(
            self.Data.get("awardEnd"),
            map_keys=("totalByUid", "awardByUid", "trophyByUid"),
            value_keys=(
                "Award",
                "award",
                "Trophy",
                "trophy",
                "Count",
                "count",
                "Total",
                "total",
            ),
        )

    def poll_by_uid(self) -> dict[str, int]:
        return _numbers_by_uid(
            self.Data.get("answerEnd"),
            map_keys=("pollResponsesByUid", "answerByUid", "totalByUid"),
            value_keys=("Poll", "poll", "Answer", "answer", "Count", "count", "Total", "total"),
        )

    def camera_minutes_by_uid(self) -> dict[str, float]:
        """inoutEnd 에서 uid -> 카메라 on 시간(분) 추출. 정확 필드 확인 전 best-effort."""
        io = self.Data.get("inoutEnd")
        if not isinstance(io, list):
            return {}
        out: dict[str, float] = {}
        for rec in io:
            uid = rec.get("Uid") or rec.get("uid")
            if not uid:
                continue
            seconds = (
                rec.get("CameraTime")
                or rec.get("cameraTime")
                or rec.get("CameraDuration")
                or 0
            )
            out[str(uid)] = out.get(str(uid), 0.0) + float(seconds) / 60.0
        return out


# -------------- HomeworkSubmit --------------


class HomeworkParty(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    Uid: int | None = None
    Name: str | None = None
    Account: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_official_lms_keys(cls, data: Any) -> Any:
        """Normalize official LMS keys like StudentUid/TeacherUid to Uid."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        out.setdefault("Uid", out.get("StudentUid") or out.get("TeacherUid"))
        out.setdefault("Name", out.get("StudentName") or out.get("TeacherName"))
        out.setdefault("Account", out.get("StudentAccount") or out.get("TeacherAccount"))
        return out


class HomeworkSubmitData(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    UnitId: int | None = None
    ActivityId: int
    ActivityName: str | None = None
    StudentInfo: HomeworkParty | None = None
    TeacherInfo: HomeworkParty | None = None
    SubmissionTime: int | None = None
    IsSubmitLate: int | bool | None = None
    IsRevision: int | bool | None = None
    Content: str | None = None
    Files: list[dict] = Field(default_factory=list)
    StudentTotal: int | None = None
    SubmitTotal: int | None = None


class HomeworkSubmitEvent(_BaseEvent):
    Cmd: Literal["HomeworkSubmit"]
    Data: HomeworkSubmitData


# -------------- HomeworkScore --------------


class HomeworkReviewDetails(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    Correct: int | None = None
    Wrong: int | None = None
    Trophy: int | None = None
    Excellent: int | None = None
    Comment: str | None = None


class HomeworkScoreData(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    UnitId: int | None = None
    ActivityId: int
    ActivityName: str | None = None
    Score: float | None = None
    StudentInfo: HomeworkParty | None = None
    TeacherInfo: HomeworkParty | None = None
    CorrectionTime: int | None = None
    StudentScoringRate: float | None = None
    ReviewDetails: HomeworkReviewDetails | None = None


class HomeworkScoreEvent(_BaseEvent):
    Cmd: Literal["HomeworkScore"]
    Data: HomeworkScoreData


# -------------- Generic (우리가 모르는 Cmd) --------------


class GenericEvent(_BaseEvent):
    Cmd: str
    Data: Any = None


# -------------- Discriminated union --------------

ClassInEvent = Annotated[
    Union[AttendanceEvent, EndEvent, HomeworkSubmitEvent, HomeworkScoreEvent, GenericEvent],
    Field(discriminator="Cmd"),
]


def parse_event(raw: dict) -> _BaseEvent:
    """Cmd 로 적절한 모델에 바인딩. 모르는 Cmd 는 GenericEvent."""
    cmd = raw.get("Cmd") or raw.get("cmd")
    model = _KNOWN.get(cmd, GenericEvent)
    return model.model_validate(raw)


_KNOWN: dict[str, type[_BaseEvent]] = {
    "Attendance": AttendanceEvent,
    "End": EndEvent,
    "HomeworkSubmit": HomeworkSubmitEvent,
    "HomeworkScore": HomeworkScoreEvent,
}


def _iter_numbers(obj: Any):
    if isinstance(obj, (int, float)):
        yield obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _iter_numbers(x)
    elif isinstance(obj, dict):
        for x in obj.values():
            yield from _iter_numbers(x)


def _numbers_by_uid(
    obj: Any,
    *,
    map_keys: tuple[str, ...],
    value_keys: tuple[str, ...],
) -> dict[str, int]:
    """Best-effort uid별 집계 추출.

    실제 End 세부 payload는 샘플 확보 전까지 흔들릴 수 있어, 확인된
    `totalByUid` 형태와 리스트 레코드 형태를 모두 받아준다.
    """
    out: dict[str, int] = {}
    if isinstance(obj, dict):
        for key in map_keys:
            mapped = obj.get(key)
            if isinstance(mapped, dict):
                _merge_numeric_mapping(out, mapped)
        if not out:
            _merge_numeric_mapping(out, obj)
    elif isinstance(obj, list):
        for rec in obj:
            if not isinstance(rec, dict):
                continue
            uid = (
                rec.get("Uid")
                or rec.get("uid")
                or rec.get("StudentUid")
                or rec.get("studentUid")
            )
            if not uid:
                continue
            for key in value_keys:
                val = rec.get(key)
                if isinstance(val, (int, float)):
                    out[str(uid)] = out.get(str(uid), 0) + int(val)
                    break
    return out


def _merge_numeric_mapping(out: dict[str, int], mapped: dict) -> None:
    for uid, val in mapped.items():
        if not str(uid).isdigit() or not isinstance(val, (int, float)):
            continue
        out[str(uid)] = out.get(str(uid), 0) + int(val)
