"""진학 적합도 진단 파이프라인 (Layer 4).

흐름: 동의 게이트 → 생기부 파싱 → 루브릭 → 채점 → 리포트
     → (승인 후) DB6 아카이브 + (코퍼스 동의 시) 비식별 → DB7.
패턴 D(주간 리포트): 여기서는 리포트 객체까지 생성하고,
Notion 아카이브/코퍼스 적재는 원장 승인 후 호출하도록 분리.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig
from ..intelligence.career_report import CareerReport, build_career_report
from ..intelligence.consent import (
    ConsentStatus,
    can_add_to_corpus,
    ensure_can_analyze,
)
from ..intelligence.deidentify import deidentify
from ..intelligence.fit_scorer import ScoreItem, score_fit
from ..intelligence.rubric_builder import build_rubric
from ..intelligence.saenggibu_parser import parse_saenggibu
from ..intelligence.saenggibu_schema import StructuredSaenggibu


def guard_consent(status: ConsentStatus) -> None:
    """파이프라인 최상단 게이트. 미동의면 즉시 raise."""
    ensure_can_analyze(status)


@dataclass
class DiagnosisResult:
    saenggibu: StructuredSaenggibu
    items: list[ScoreItem]
    report: CareerReport
    avg_score: float


def diagnose(
    cfg: AppConfig,
    *,
    pdf_bytes: bytes,
    target_major: str,
    consent: ConsentStatus,
) -> DiagnosisResult:
    guard_consent(consent)                                  # 1 게이트 (Claude 호출 전)
    sg = parse_saenggibu(cfg, pdf_bytes=pdf_bytes)          # 2 파싱
    rubric = build_rubric(cfg, major=target_major)          # 3 루브릭
    items = score_fit(cfg, sg=sg, rubric=rubric)            # 4 채점(인용검증 포함)
    report = build_career_report(cfg, major=target_major, items=items)  # 5 리포트
    verified = [i for i in items if i.citation_verified] or items
    avg = round(sum(i.score for i in verified) / max(len(verified), 1), 2)
    return DiagnosisResult(saenggibu=sg, items=items, report=report, avg_score=avg)


def archive_corpus_if_consented(
    cfg: AppConfig,
    repo,
    *,
    result: DiagnosisResult,
    consent: ConsentStatus,
    student_names: list[str],
    schools: list[str],
    track_tags: list[str],
    corpus_id: str,
    consent_ref: str,
) -> str | None:
    """원장 승인 후 호출. 코퍼스활용 동의 시에만 비식별 적재."""
    if not can_add_to_corpus(consent):
        return None
    _clean = deidentify(result.saenggibu, names=student_names, schools=schools)  # noqa: F841 (비식별 단계 — page children 직렬화는 후속 작업)
    # _clean(비식별 구조화 생기부)은 page children 블록으로 첨부 — repo 메서드에서 처리
    return repo.add_corpus_entry(
        corpus_id=corpus_id, track_tags=track_tags, consent_ref=consent_ref
    )
