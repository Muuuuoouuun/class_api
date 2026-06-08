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
