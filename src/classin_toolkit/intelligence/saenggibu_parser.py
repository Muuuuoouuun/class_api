from __future__ import annotations

from ..config import AppConfig
from .claude_client import load_prompt, run_json_with_pdf
from .saenggibu_schema import StructuredSaenggibu


def parse_saenggibu(cfg: AppConfig, *, pdf_bytes: bytes) -> StructuredSaenggibu:
    system = load_prompt("saenggibu_parse")
    raw = run_json_with_pdf(
        cfg,
        system=system,
        user="이 생기부를 스키마대로 구조화해줘.",
        pdf_bytes=pdf_bytes,
        tier="report",
    )
    return StructuredSaenggibu.model_validate(raw)  # 검증 실패 시 raise → 경계에서 catch
