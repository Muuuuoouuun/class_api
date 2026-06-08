from classin_toolkit.intelligence.saenggibu_schema import StructuredSaenggibu, SubjectDetail


def test_valid_saenggibu_parses():
    data = {
        "grade_records": [
            {
                "grade": 2,
                "subject_details": [
                    {"subject": "물리학I", "text": "역학 탐구 보고서 작성", "confidence": 0.9}
                ],
                "creative_activities": ["과학 동아리 부장"],
                "behavior_notes": "성실함",
                "attendance": {"absent": 0, "late": 1},
            }
        ],
    }
    sg = StructuredSaenggibu.model_validate(data)
    assert sg.grade_records[0].grade == 2
    assert sg.grade_records[0].subject_details[0].subject == "물리학I"


def test_failed_parse_item_marked_null():
    detail = SubjectDetail(subject="수학", text=None, confidence=0.0)
    assert detail.text is None
    assert detail.is_low_confidence() is True
