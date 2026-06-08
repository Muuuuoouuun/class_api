from __future__ import annotations

from .saenggibu_schema import StructuredSaenggibu


def _scrub(text: str | None, names: list[str], schools: list[str]) -> str | None:
    if text is None:
        return None
    out = text
    for n in names:
        if n:
            out = out.replace(n, "[학생]")
    for s in schools:
        if s:
            out = out.replace(s, "[학교]")
    return out


def deidentify(
    sg: StructuredSaenggibu, *, names: list[str], schools: list[str]
) -> StructuredSaenggibu:
    """식별자를 토큰으로 치환한 새 객체 반환 (원본 불변)."""
    clean = sg.model_copy(deep=True)
    for g in clean.grade_records:
        for d in g.subject_details:
            d.text = _scrub(d.text, names, schools)
        g.creative_activities = [
            _scrub(a, names, schools) or "" for a in g.creative_activities
        ]
        g.behavior_notes = _scrub(g.behavior_notes, names, schools)
    return clean
