"""Claude API 래퍼.

지침:
- Claude는 "분석·리포트·문구 생성·OCR" 역할에 집중.
- 프롬프트는 intelligence/prompts/ 디렉터리에서 파일로 관리.
- Prompt caching: 학원 컨텍스트(페르소나 목록·톤 가이드)는 캐시 타깃.
"""
from __future__ import annotations

import base64
import json
import logging
from functools import lru_cache
from pathlib import Path

from anthropic import Anthropic

from ..config import AppConfig

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=1)
def get_claude(api_key: str) -> Anthropic:
    return Anthropic(api_key=api_key)


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def run_structured(
    cfg: AppConfig,
    *,
    system: str,
    user: str,
    model: str | None = None,
    max_tokens: int = 2048,
    cache_system: bool = True,
) -> str:
    client = get_claude(cfg.anthropic.api_key)
    system_blocks = [{"type": "text", "text": system}]
    if cache_system:
        system_blocks[0]["cache_control"] = {"type": "ephemeral"}

    resp = client.messages.create(
        model=model or cfg.anthropic.model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return text.strip()


def run_json(cfg: AppConfig, *, system: str, user: str, model: str | None = None) -> dict | list:
    raw = run_structured(cfg, system=system, user=user, model=model)
    raw = _strip_fence(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.error("claude returned non-json: %s", raw[:500])
        raise


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def run_json_with_pdf(
    cfg: AppConfig,
    *,
    system: str,
    user: str,
    pdf_bytes: bytes,
    model: str | None = None,
    max_tokens: int = 4096,
) -> dict | list:
    """PDF를 document 블록으로 첨부해 구조화 JSON을 받는다.

    Claude 네이티브 PDF 지원 → 스캔본(이미지 PDF)도 비전으로 처리.
    """
    client = get_claude(cfg.anthropic.api_key)
    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    content = [
        {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
        },
        {"type": "text", "text": user},
    ]
    resp = client.messages.create(
        model=model or cfg.anthropic.report_model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system}],
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return json.loads(_strip_fence(text.strip()))
