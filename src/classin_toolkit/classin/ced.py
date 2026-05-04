"""CED API 래퍼 — 실제 ClassIn action 이름 기반.

출처:
- user/register.html, user/addSchoolStudent.html, user/addTeacher.html
- classroom/addCourse.html, classroom/addCourseClass.html,
  classroom/addCourseStudentMultiple.html, classroom/addClassStudentMultiple.html
- LMS/releaseActivity.html

응답 `data` 는 대부분 생성된 엔티티의 ID (정수) 또는 ID 리스트.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from .client import ClassInClient
from .schemas import Course, Homework, Lesson, Student

log = logging.getLogger(__name__)


class CEDClient:
    def __init__(self, client: ClassInClient):
        self._c = client

    # --- User --------------------------------------------------------

    def register_user(
        self,
        *,
        telephone: str | None = None,
        email: str | None = None,
        password: str | None = None,
        nickname: str | None = None,
        add_to_school_member: int = 1,
    ) -> int:
        """register — 학원 소속 유저 계정 생성. `data` 필드에 UID 반환."""
        body: dict = {"addToSchoolMember": add_to_school_member}
        if telephone:
            body["telephone"] = telephone
        if email:
            body["email"] = email
        if password:
            body["password"] = password
        if nickname:
            body["nickname"] = nickname
        # 이미 가입된 전화/이메일이면 135/461 과 함께 UID 가 내려오므로 idempotent 처리.
        data = self._c.call_v1("register", body, success_codes=(1, 135, 461))
        return int(data)

    def add_school_student(
        self,
        *,
        uid: int | None = None,
        account: str | None = None,
        nickname: str | None = None,
        student_number: str | None = None,
    ) -> None:
        body: dict = {}
        if uid is not None:
            body["studentUid"] = uid
        if account:
            body["studentAccount"] = account
        if nickname:
            body["studentName"] = nickname
        elif account:
            body["studentName"] = account
        elif uid is not None:
            body["studentName"] = str(uid)
        if student_number:
            body["studentNumber"] = student_number
        self._c.call_v1("addSchoolStudent", body, success_codes=(1, 133))

    def add_teacher(
        self,
        *,
        uid: int | None = None,
        account: str | None = None,
        nickname: str | None = None,
        job_number: str | None = None,
    ) -> None:
        body: dict = {}
        if account:
            body["teacherAccount"] = account
        elif uid is not None:
            body["teacherUid"] = uid
        if nickname:
            body["teacherName"] = nickname
        elif account:
            body["teacherName"] = account
        elif uid is not None:
            body["teacherName"] = str(uid)
        if job_number:
            body["jobNumber"] = job_number
        self._c.call_v1("addTeacher", body, success_codes=(1, 133))

    def create_student(self, student: Student) -> Student:
        """편의 래퍼: register → addSchoolStudent. UID 를 student.classin_id 에 저장."""
        uid = self.register_user(
            telephone=student.phone,
            password=student.password,
            nickname=student.name,
        )
        self.add_school_student(uid=uid, account=student.phone, nickname=student.name)
        student.classin_id = str(uid)
        return student

    # --- Course / Class ----------------------------------------------

    def add_course(self, course: Course) -> Course:
        """addCourse — Course (반) 생성. data 에 courseId."""
        body: dict = {"courseName": course.name}
        if course.teacher_ids:
            # v1 addCourse 는 담임/관리 교사를 mainTeacherUid 로 받는다.
            body["mainTeacherUid"] = int(course.teacher_ids[0])
        data = self._c.call_v1("addCourse", body)
        course.classin_id = str(data)
        return course

    def add_course_students(self, course_id: str, uids: Iterable[int]) -> None:
        """addCourseStudentMultiple — 반에 학생 여러 명 추가."""
        self._c.call_v1(
            "addCourseStudentMultiple",
            {
                "courseId": int(course_id),
                "identity": 1,
                "studentJson": [{"uid": str(uid)} for uid in uids],
            },
        )

    def add_class_students(
        self,
        *,
        course_id: str,
        class_id: str,
        uids: Iterable[int],
    ) -> None:
        """addClassStudentMultiple — 특정 수업(Class)에만 학생 다수 추가."""
        self._c.call_v1(
            "addClassStudentMultiple",
            {
                "courseId": int(course_id),
                "classId": int(class_id),
                "identity": 1,
                "studentJson": [{"uid": str(uid)} for uid in uids],
            },
        )

    def add_course_class(self, lesson: Lesson) -> Lesson:
        """addCourseClass — Course 아래 개별 수업(Class) 생성. data 에 classId."""
        body: dict = {
            "courseId": int(lesson.course_id),
            "className": lesson.title,
            "beginTime": int(lesson.start_at.timestamp()),
            "endTime": int(lesson.end_at.timestamp()),
        }
        if lesson.teacher_id:
            body["teacherUid"] = int(lesson.teacher_id)
        data = self._c.call_v1("addCourseClass", body)
        lesson.classin_id = str(data)
        return lesson

    # --- LMS v2 -------------------------------------------------------

    def create_unit(
        self,
        *,
        course_id: str | int,
        name: str,
        publish_flag: int = 2,
        content: str = "",
    ) -> int:
        """LMS createUnit — Course 아래 단원 생성. data.unitId 반환."""
        data = self._c.call_v2(
            "/lms/unit/create",
            {
                "courseId": int(course_id),
                "name": name,
                "content": content,
                "publishFlag": publish_flag,
            },
        )
        return int(data["unitId"])

    def create_classroom(
        self,
        lesson: Lesson,
        *,
        unit_id: str | int | None = None,
        teacher_uid: str | int | None = None,
        assistant_uids: Iterable[int] | None = None,
        record_state: int = 0,
        live_state: int = 0,
        open_state: int = 0,
        record_type: int = 0,
    ) -> dict[str, Any]:
        """LMS createClass — 새 권장 방식의课堂 활동 생성.

        반환 data 에 `activityId`, `classId`, `live_url` 등이 포함된다.
        """
        resolved_teacher = teacher_uid or lesson.teacher_id
        if not resolved_teacher:
            raise ValueError("teacher_uid or lesson.teacher_id is required")

        body: dict[str, Any] = {
            "courseId": int(lesson.course_id),
            "name": lesson.title,
            "teacherUid": int(resolved_teacher),
            "startTime": int(lesson.start_at.timestamp()),
            "endTime": int(lesson.end_at.timestamp()),
        }
        if unit_id is not None:
            body["unitId"] = int(unit_id)
        if assistant_uids:
            body["assistantUids"] = [int(uid) for uid in assistant_uids]
        if any((record_state, live_state, open_state, record_type)):
            body.update(
                {
                    "recordState": record_state,
                    "liveState": live_state,
                    "openState": open_state,
                    "recordType": record_type,
                }
            )

        data = self._c.call_v2("/lms/activity/createClass", body)
        if isinstance(data, dict) and data.get("classId"):
            lesson.classin_id = str(data["classId"])
        return dict(data or {})

    def create_non_class_activity(
        self,
        *,
        course_id: str | int,
        unit_id: str | int,
        name: str,
        teacher_uid: str | int,
        activity_type: int = 2,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> int:
        """LMS createActivityNoClass — 숙제/퀴즈 등 비课堂 활동 초안 생성."""
        body: dict[str, Any] = {
            "courseId": int(course_id),
            "unitId": int(unit_id),
            "activityType": activity_type,
            "name": name,
            "teacherUid": int(teacher_uid),
        }
        if start_time is not None:
            body["startTime"] = start_time
        if end_time is not None:
            body["endTime"] = end_time

        data = self._c.call_v2("/lms/activity/createActivityNoClass", body)
        activity_id = data.get("activityId")
        if activity_id is None:
            raise ValueError(f"createActivityNoClass response missing activityId: {data}")
        return int(activity_id)

    def release_activity(
        self,
        *,
        course_id: str | int,
        activity_ids: Iterable[int] | int,
    ) -> Any:
        """LMS releaseActivity — 활동 초안을 게시한다."""
        if isinstance(activity_ids, int):
            ids = [activity_ids]
        else:
            ids = [int(activity_id) for activity_id in activity_ids]
        results = [
            self._c.call_v2(
                "/lms/activity/release",
                {"courseId": int(course_id), "activityId": activity_id},
            )
            for activity_id in ids
        ]
        return results[0] if len(results) == 1 else results

    def add_activity_students(
        self,
        *,
        course_id: str | int,
        activity_id: str | int,
        student_uids: Iterable[int],
    ) -> Any:
        """LMS addStudent — 특정 활동에 학생을 추가한다."""
        return self._c.call_v2(
            "/lms/activity/addStudent",
            {
                "courseId": int(course_id),
                "activityId": int(activity_id),
                "studentUids": [int(uid) for uid in student_uids],
            },
        )

    # --- Homework convenience ----------------------------------------

    def release_homework(self, hw: Homework, *, course_id: str | int) -> Homework:
        """LMS releaseActivity — 이미 생성된 Activity 를 수업에 release.

        NOTE: ClassIn LMS 구조상 Unit → Classroom → Activity 를 미리 만든 뒤
        releaseActivity 로 배포한다. activity_id 는 Homework.classin_id 에 기대.
        스펙 확인 필요 — 현재는 기본 형태만 제공.
        """
        if not hw.classin_id:
            raise ValueError("homework.classin_id (activityId) is required before release")
        self.release_activity(course_id=course_id, activity_ids=int(hw.classin_id))
        return hw
