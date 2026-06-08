"""루브릭 빌더: 목표 학과 → 채점 루브릭 생성.

프로세스 캐시로 동일 학과 재요청 최적화. 공식 입시 기준이 아닌 참고용 루브릭.
"""
from __future__ import annotations

import json

from ..config import AppConfig
from .claude_client import load_prompt, run_json

_RUBRIC_CACHE: dict[str, dict] = {}


def build_rubric(cfg: AppConfig, *, major: str) -> dict:
    """목표 학과 → 채점 루브릭 dict. 동일 학과 재요청은 프로세스 캐시.

    Args:
        cfg: 애플리케이션 설정 (anthropic.model 포함)
        major: 목표 학과/계열 (예: "컴퓨터공학과", "경영학과")

    Returns:
        루브릭 dict:
        {
            "major": str,
            "competencies": [
                {
                    "key": str,
                    "label": str,
                    "description": str,
                    "weight": float (0~1)
                }
            ]
        }

    Raises:
        ValueError: 루브릭 형식이 올바르지 않은 경우
    """
    key = f"{cfg.anthropic.model}::{major}"
    if key in _RUBRIC_CACHE:
        return _RUBRIC_CACHE[key]

    system = load_prompt("rubric_build")
    rubric = run_json(cfg, system=system, user=json.dumps({"major": major}, ensure_ascii=False))

    if not isinstance(rubric, dict) or "competencies" not in rubric:
        raise ValueError(f"루브릭 형식 오류: {rubric!r}")

    _RUBRIC_CACHE[key] = rubric
    return rubric
