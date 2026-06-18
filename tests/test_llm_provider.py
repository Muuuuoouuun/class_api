"""LLM provider 라우팅 (anthropic|gemini) + tier 모델 해석 테스트.

실제 API 호출 없이 provider 경계(gemini_client / get_claude)를 목으로 가로채 검증한다.
"""

from __future__ import annotations

from classin_toolkit.config import AppConfig
from classin_toolkit.intelligence import claude_client, gemini_client


def _cfg(provider: str) -> AppConfig:
    return AppConfig.model_validate(
        {
            "academy": {"name": "t", "timezone": "Asia/Seoul"},
            "classin": {"school_id": "s", "secret_key": "k"},
            "notion": {
                "token": "t",
                "databases": {"students": "a", "lessons": "b", "reports": "c"},
            },
            "anthropic": {"api_key": "sk-ant-x"},
            "llm": {"provider": provider},
            "gemini": {"api_key": "g-key"},
        }
    )


def test_resolve_model_matrix():
    a, g = _cfg("anthropic"), _cfg("gemini")
    assert claude_client._resolve_model(a, "default", None) == "claude-sonnet-4-6"
    assert claude_client._resolve_model(a, "report", None) == "claude-opus-4-7"
    assert claude_client._resolve_model(g, "default", None) == "gemini-2.5-flash"
    assert claude_client._resolve_model(g, "report", None) == "gemini-2.5-pro"
    assert claude_client._resolve_model(g, "report", "override-x") == "override-x"


def test_run_json_routes_to_gemini_with_json_mode(monkeypatch):
    seen: dict = {}

    def fake(cfg, *, system, user, model, max_tokens=2048, json_mode=False):
        seen.update(model=model, json_mode=json_mode)
        return '{"ok": true}'

    monkeypatch.setattr(gemini_client, "gemini_text", fake)
    out = claude_client.run_json(_cfg("gemini"), system="s", user="u")
    assert out == {"ok": True}
    assert seen == {"model": "gemini-2.5-flash", "json_mode": True}


def test_run_structured_report_tier_to_gemini(monkeypatch):
    seen: dict = {}

    def fake(cfg, *, system, user, model, max_tokens=2048, json_mode=False):
        seen["model"] = model
        return "안녕"

    monkeypatch.setattr(gemini_client, "gemini_text", fake)
    out = claude_client.run_structured(_cfg("gemini"), system="s", user="u", tier="report")
    assert out == "안녕"
    assert seen["model"] == "gemini-2.5-pro"


def test_run_json_with_pdf_routes_to_gemini(monkeypatch):
    seen: dict = {}

    def fake(cfg, *, system, user, pdf_bytes, model, max_tokens=4096, json_mode=True):
        seen.update(model=model, pdf=pdf_bytes)
        return '{"parsed": 1}'

    monkeypatch.setattr(gemini_client, "gemini_text_with_pdf", fake)
    out = claude_client.run_json_with_pdf(_cfg("gemini"), system="s", user="u", pdf_bytes=b"%PDF")
    assert out == {"parsed": 1}
    assert seen["model"] == "gemini-2.5-pro"  # 기본 tier=report
    assert seen["pdf"] == b"%PDF"


def test_run_structured_anthropic_uses_claude(monkeypatch):
    seen: dict = {}

    class _Block:
        type = "text"
        text = "hi-claude"

    class _Resp:
        content = [_Block()]

    class _Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                seen["model"] = kwargs["model"]
                return _Resp()

    monkeypatch.setattr(claude_client, "get_claude", lambda key: _Client())
    out = claude_client.run_structured(_cfg("anthropic"), system="s", user="u")
    assert out == "hi-claude"
    assert seen["model"] == "claude-sonnet-4-6"
