from classin_toolkit.intelligence.deidentify import deidentify
from classin_toolkit.intelligence.saenggibu_schema import (
    GradeRecord, StructuredSaenggibu, SubjectDetail,
)


def test_removes_student_and_school_names():
    sg = StructuredSaenggibu(grade_records=[
        GradeRecord(
            grade=3,
            subject_details=[SubjectDetail(subject="국어", text="홍길동 학생은 한빛고에서 우수함")],
            behavior_notes="홍길동은 성실",
        )
    ])
    clean = deidentify(sg, names=["홍길동"], schools=["한빛고"])
    joined = " ".join(clean.all_evidence())
    assert "홍길동" not in joined
    assert "한빛고" not in joined
    assert "[학생]" in joined or "성실" in joined


def test_original_object_not_mutated():
    sg = StructuredSaenggibu(grade_records=[
        GradeRecord(grade=1, behavior_notes="김철수 우수")
    ])
    deidentify(sg, names=["김철수"], schools=[])
    assert "김철수" in (sg.grade_records[0].behavior_notes or "")
