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
from typing import Iterable

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
        data = self._c.call("register", body)
        return int(data)

    def add_school_student(
        self, *, uid: int, nickname: str | None = None, student_number: str | None = None
    ) -> None:
        body: dict = {"uid": uid}
        if nickname:
            body["nickname"] = nickname
        if student_number:
            body["studentNumber"] = student_number
        self._c.call("addSchoolStudent", body)

    def add_teacher(
        self, *, uid: int, nickname: str | None = None, job_number: str | None = None
    ) -> None:
        body: dict = {"uid": uid}
        if nickname:
            body["nickname"] = nickname
        if job_number:
            body["jobNumber"] = job_number
        self._c.call("addTeacher", body)

    def create_student(self, student: Student) -> Student:
        """편의 래퍼: register → addSchoolStudent. UID 를 student.classin_id 에 저장."""
        uid = self.register_user(
            telephone=student.phone,
            password=student.password,
            nickname=student.name,
        )
        self.add_school_student(uid=uid, nickname=student.name)
        student.classin_id = str(uid)
        return student

    # --- Course / Class ----------------------------------------------

    def add_course(self, course: Course) -> Course:
        """addCourse — Course (반) 생성. data 에 courseId."""
        body: dict = {"courseName": course.name}
        if course.teacher_ids:
            # 단일 교사 가정 시 teacherUid. 다교사는 addCourseTeacher 로 추가.
            body["teacherUid"] = int(course.teacher_ids[0])
        data = self._c.call("addCourse", body)
        course.classin_id = str(data)
        return course

    def add_course_students(self, course_id: str, uids: Iterable[int]) -> None:
        """addCourseStudentMultiple — 반에 학생 여러 명 추가."""
        self._c.call(
            "addCourseStudentMultiple",
            {"courseId": int(course_id), "uidList": list(uids)},
        )

    def add_class_students(self, class_id: str, uids: Iterable[int]) -> None:
        """addClassStudentMultiple — 특정 수업(Class)에만 학생 다수 추가."""
        self._c.call(
            "addClassStudentMultiple",
            {"classId": int(class_id), "uidList": list(uids)},
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
        data = self._c.call("addCourseClass", body)
        lesson.classin_id = str(data)
        return lesson

    # --- Homework (LMS) ----------------------------------------------

    def release_homework(self, hw: Homework) -> Homework:
        """LMS releaseActivity — 이미 생성된 Activity 를 수업에 release.

        NOTE: ClassIn LMS 구조상 Unit → Classroom → Activity 를 미리 만든 뒤
        releaseActivity 로 배포한다. activity_id 는 Homework.classin_id 에 기대.
        스펙 확인 필요 — 현재는 기본 형태만 제공.
        """
        if not hw.classin_id:
            raise ValueError("homework.classin_id (activityId) is required before release")
        body: dict = {
            "activityId": int(hw.classin_id),
            "classId": int(hw.lesson_id),
        }
        if hw.due_at:
            body["deadline"] = int(hw.due_at.timestamp())
        self._c.call("releaseActivity", body)
        return hw
