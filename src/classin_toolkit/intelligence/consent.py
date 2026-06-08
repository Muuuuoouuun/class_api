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
        return cls.NONE


def ensure_can_analyze(status: ConsentStatus) -> None:
    if status is ConsentStatus.NONE:
        raise ConsentError("미동의 상태에서는 생기부 분석을 수행할 수 없습니다.")


def can_add_to_corpus(status: ConsentStatus) -> bool:
    return status is ConsentStatus.CORPUS
