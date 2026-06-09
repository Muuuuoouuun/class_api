"""NEIS 교육정보 개방 포털 OpenAPI 참조 클라이언트 (read-only, Layer 3 보조).

학교 단위 공개 데이터만 — 개인 생기부/성적은 이 API로 받을 수 없다.
진학 적합도 진단의 보조 메타데이터(학교/계열/학과/학사일정)에 사용.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import NeisConfig

log = logging.getLogger(__name__)

# no-data 코드: 에러가 아니라 빈 결과로 취급
_NO_DATA = "INFO-200"


class NeisError(Exception):
    pass


@dataclass
class School:
    office_code: str   # ATPT_OFCDC_SC_CODE
    school_code: str   # SD_SCHUL_CODE
    name: str          # SCHUL_NM
    office_name: str    # ATPT_OFCDC_SC_NM


def extract_rows(service: str, payload: dict) -> list[dict]:
    """NEIS 응답 봉투에서 row 리스트 추출. no-data면 [], 에러면 NeisError."""
    if service in payload:
        blocks = payload[service]
        # blocks[0].head[1].RESULT.CODE / blocks[1].row
        for block in blocks:
            if isinstance(block, dict) and "row" in block:
                return block["row"]
        # row 블록이 없으면 head의 RESULT를 확인 — 봉투 내부 에러를 [] 로 삼키지 않음
        for block in blocks:
            if isinstance(block, dict) and "head" in block:
                entry = next((e for e in block["head"] if isinstance(e, dict) and "RESULT" in e), {})
                result = entry.get("RESULT", {})
                code = result.get("CODE", "")
                if code and code not in ("INFO-000", _NO_DATA):
                    raise NeisError(f"NEIS {service} 오류: {code} {result.get('MESSAGE', '')}")
        return []
    result = payload.get("RESULT", {})
    code = result.get("CODE", "")
    if code == _NO_DATA:
        return []
    raise NeisError(f"NEIS {service} 오류: {code} {result.get('MESSAGE', '')}")


def _get(cfg: NeisConfig, service: str, params: dict) -> list[dict]:
    # NOTE: 단일 페이지. NEIS 학교 단위 데이터셋(학과/계열/일정)은 실무상 100행 미만.
    q = {"KEY": cfg.api_key, "Type": "json", "pIndex": 1, "pSize": 100, **params}
    url = f"{cfg.base_url}/{service}"
    resp = httpx.get(url, params=q, timeout=10.0)
    resp.raise_for_status()
    return extract_rows(service, resp.json())


def find_schools(cfg: NeisConfig, *, name: str) -> list[School]:
    rows = _get(cfg, "schoolInfo", {"SCHUL_NM": name})
    return [
        School(
            office_code=r["ATPT_OFCDC_SC_CODE"],
            school_code=r["SD_SCHUL_CODE"],
            name=r["SCHUL_NM"],
            office_name=r.get("ATPT_OFCDC_SC_NM", ""),
        )
        for r in rows
    ]


def school_majors(cfg: NeisConfig, *, office_code: str, school_code: str) -> list[str]:
    """학교학과정보 → 학과명 리스트 (DDDEP_NM)."""
    rows = _get(cfg, "schoolMajorinfo",
                {"ATPT_OFCDC_SC_CODE": office_code, "SD_SCHUL_CODE": school_code})
    return [r["DDDEP_NM"] for r in rows if r.get("DDDEP_NM")]


def school_tracks(cfg: NeisConfig, *, office_code: str, school_code: str) -> list[str]:
    """학교계열정보 → 계열명 리스트 (ORD_SC_NM)."""
    rows = _get(cfg, "schulAflcoinfo",
                {"ATPT_OFCDC_SC_CODE": office_code, "SD_SCHUL_CODE": school_code})
    return sorted({r["ORD_SC_NM"] for r in rows if r.get("ORD_SC_NM")})
