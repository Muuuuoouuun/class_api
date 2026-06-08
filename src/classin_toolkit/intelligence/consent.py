from __future__ import annotations

from enum import Enum


class ConsentError(Exception):
    pass


class ConsentStatus(Enum):
    NONE = "미동의"
    INTERNAL = "내부분석"
    CORPUS = "코퍼스활용"

    @classmethod
    def from_label(cls, label: str | None) -> "ConsentStatus":
        for s in cls:
            if s.value == (label or "").strip():
                return s
        # 안전 기본값: 알 수 없는 라벨은 무동의로 취급
        return cls.NONE


def ensure_can_analyze(status: ConsentStatus) -> None:
    """파이프라인 최상단 게이트 — 미동의면 분석 자체를 차단."""
    if status is ConsentStatus.NONE:
        raise ConsentError("미동의 상태에서는 생기부 분석을 수행할 수 없습니다.")


def can_add_to_corpus(status: ConsentStatus) -> bool:
    """코퍼스활용 동의일 때만 비식별 데이터를 코퍼스 DB에 적재 가능."""
    return status is ConsentStatus.CORPUS
