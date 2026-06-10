from classin_toolkit.intelligence.career_report import CareerReport


def test_parse_splits_summary_and_parent_message():
    text = "## 진단 요약\n강점: 탐구역량\n\n## 학부모 카톡 문구\n안녕하세요, ..."
    r = CareerReport.parse(text)
    assert "탐구역량" in r.summary_markdown
    assert r.parent_message.startswith("안녕하세요")


def test_parse_missing_parent_section_yields_empty():
    r = CareerReport.parse("## 진단 요약\n내용만 있음")
    assert r.parent_message == ""
