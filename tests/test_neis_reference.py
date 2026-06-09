import pytest
from classin_toolkit.intelligence.neis_reference import extract_rows, NeisError


def test_extract_rows_success():
    payload = {
        "schoolMajorinfo": [
            {"head": [{"list_total_count": 2}, {"RESULT": {"CODE": "INFO-000", "MESSAGE": "정상"}}]},
            {"row": [{"DDDEP_NM": "소프트웨어과"}, {"DDDEP_NM": "정보보호과"}]},
        ]
    }
    rows = extract_rows("schoolMajorinfo", payload)
    assert [r["DDDEP_NM"] for r in rows] == ["소프트웨어과", "정보보호과"]


def test_extract_rows_no_data_returns_empty():
    payload = {"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}}
    assert extract_rows("schoolMajorinfo", payload) == []


def test_extract_rows_error_raises():
    payload = {"RESULT": {"CODE": "ERROR-300", "MESSAGE": "필수 값 누락"}}
    with pytest.raises(NeisError):
        extract_rows("schoolMajorinfo", payload)
