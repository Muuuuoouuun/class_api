from classin_toolkit.intelligence.fit_scorer import validate_citations, ScoreItem
from classin_toolkit.intelligence.saenggibu_schema import (
    GradeRecord, StructuredSaenggibu, SubjectDetail,
)


def _sg():
    return StructuredSaenggibu(grade_records=[
        GradeRecord(grade=2, subject_details=[
            SubjectDetail(subject="물리", text="역학 탐구 보고서 작성"),
        ])
    ])


def test_hallucinated_citation_flagged():
    items = [
        ScoreItem(key="탐구역량", score=4, citation="역학 탐구 보고서 작성"),  # 실재
        ScoreItem(key="리더십", score=5, citation="전교회장 활동"),            # 환각
    ]
    checked = validate_citations(items, _sg())
    by_key = {i.key: i for i in checked}
    assert by_key["탐구역량"].citation_verified is True
    assert by_key["리더십"].citation_verified is False


def test_short_or_empty_citation_not_verified():
    items = [
        ScoreItem(key="A", score=1, citation="작성"),   # 2글자 — 의미 없는 단편
        ScoreItem(key="B", score=0, citation=""),       # 빈 인용 (모델이 근거 없음을 인정)
    ]
    checked = validate_citations(items, _sg())
    by = {i.key: i for i in checked}
    assert by["A"].citation_verified is False
    assert by["B"].citation_verified is False
