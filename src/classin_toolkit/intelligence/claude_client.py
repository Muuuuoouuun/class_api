"""Claude API 래퍼.

지침:
- Claude는 "분석·리포트·문구 생성·OCR" 역할에 집중.
- 프롬프트는 intelligence/prompts/ 디렉터리에서 파일로 관리.
- Prompt caching: 학원 컨텍스트(페르소나 목록·톤 가이드)는 캐시 타깃.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from anthropic import Anthropic

from ..config import AppConfig

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
CLAUDE_DEFAULT_MODEL = "claude-sonnet-4-6"
CLAUDE_DEFAULT_REPORT_MODEL = "claude-opus-4-7"
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash"
GEMINI_DEFAULT_REPORT_MODEL = "gemini-3.5-flash"

AiProvider = Literal["anthropic", "gemini"]


@lru_cache(maxsize=1)
def get_claude(api_key: str) -> Anthropic:
    return Anthropic(api_key=api_key)


@lru_cache(maxsize=1)
def get_gemini_openai(api_key: str):
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "Gemini provider requires the openai package. Run `pip install -e .` again."
        ) from exc

    return OpenAI(api_key=api_key, base_url=GEMINI_OPENAI_BASE_URL)


def resolve_provider(cfg: AppConfig) -> AiProvider:
    provider = cfg.anthropic.provider
    if provider in ("anthropic", "gemini"):
        return provider

    api_key = (cfg.anthropic.api_key or "").strip()
    model = (cfg.anthropic.model or "").strip().lower()
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith("AIza") or model.startswith("gemini-"):
        return "gemini"
    return "anthropic"


def resolve_model(
    cfg: AppConfig,
    *,
    model: str | None = None,
    report: bool = False,
) -> str:
    selected = model or (cfg.anthropic.report_model if report else cfg.anthropic.model)
    if resolve_provider(cfg) == "gemini" and selected.startswith("claude-"):
        return GEMINI_DEFAULT_REPORT_MODEL if report else GEMINI_DEFAULT_MODEL
    if resolve_provider(cfg) == "anthropic" and selected.startswith("gemini-"):
        return CLAUDE_DEFAULT_REPORT_MODEL if report else CLAUDE_DEFAULT_MODEL
    return selected


def has_configured_ai_key(cfg: AppConfig) -> bool:
    api_key = (cfg.anthropic.api_key or "").strip()
    if not api_key:
        return False
    lowered = api_key.lower()
    return not lowered.startswith(("test", "dummy", "your-")) and "replace_me" not in lowered


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
    if resolve_provider(cfg) == "gemini":
        return _run_gemini_structured(
            cfg,
            system=system,
            user=user,
            model=model,
            max_tokens=max_tokens,
        )

    client = get_claude(cfg.anthropic.api_key)
    system_blocks = [{"type": "text", "text": system}]
    if cache_system:
        system_blocks[0]["cache_control"] = {"type": "ephemeral"}

    resp = client.messages.create(
        model=resolve_model(cfg, model=model),
        max_tokens=max_tokens,
        system=system_blocks,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return text.strip()


def _run_gemini_structured(
    cfg: AppConfig,
    *,
    system: str,
    user: str,
    model: str | None = None,
    max_tokens: int = 2048,
) -> str:
    client = get_gemini_openai(cfg.anthropic.api_key)
    resp = client.chat.completions.create(
        model=resolve_model(cfg, model=model),
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


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
