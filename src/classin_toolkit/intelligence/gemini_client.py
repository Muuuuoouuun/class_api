"""Gemini (google-genai) LLM 백엔드.

claude_client 의 provider 라우팅에서 `cfg.llm.provider == "gemini"` 일 때만 호출된다.
google-genai 는 선택 의존성 → import 를 함수 내부에서 지연 로드한다(anthropic-only 설치 보호).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from ..config import AppConfig

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client(api_key: str) -> Any:
    try:
        from google import genai
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "Gemini provider 를 쓰려면 google-genai 설치 필요: pip install google-genai"
        ) from exc
    return genai.Client(api_key=api_key)


def _config(system: str, max_tokens: int, json_mode: bool) -> Any:
    from google.genai import types

    kwargs: dict[str, Any] = {
        "system_instruction": system,
        "max_output_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_mime_type"] = "application/json"
    return types.GenerateContentConfig(**kwargs)


def gemini_text(
    cfg: AppConfig,
    *,
    system: str,
    user: str,
    model: str,
    max_tokens: int = 2048,
    json_mode: bool = False,
) -> str:
    client = _client(cfg.gemini.api_key)
    resp = client.models.generate_content(
        model=model,
        contents=user,
        config=_config(system, max_tokens, json_mode),
    )
    return (resp.text or "").strip()


def gemini_text_with_pdf(
    cfg: AppConfig,
    *,
    system: str,
    user: str,
    pdf_bytes: bytes,
    model: str,
    max_tokens: int = 4096,
    json_mode: bool = True,
) -> str:
    from google.genai import types

    client = _client(cfg.gemini.api_key)
    contents = [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        user,
    ]
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=_config(system, max_tokens, json_mode),
    )
    return (resp.text or "").strip()
