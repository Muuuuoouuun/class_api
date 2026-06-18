"""Non-mutating API diagnostics for operator setup.

`check-ready` is intentionally offline-only. This module adds an opt-in live
probe layer that verifies credentials and network reachability without creating
ClassIn lessons, Notion rows, Claude artifacts, or Kakao messages.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
from anthropic import Anthropic
from notion_client import Client as NotionClient

from .classin.client import ClassInAPIError, ClassInClient
from .config import AppConfig

DiagnosticStatus = Literal["ok", "warn", "missing", "failed", "skipped"]

_PLACEHOLDER_MARKERS = ("REPLACE_ME", "CHANGE_ME", "TODO", "YOUR_", "INSERT_")
_SIGNATURE_ERROR_CODES = {101002005}
_SIGNATURE_ERROR_MARKERS = ("sign", "signature", "签名", "서명")


class ContextClient(Protocol):
    def __enter__(self) -> Any: ...

    def __exit__(self, *exc: object) -> None: ...


@dataclass(frozen=True)
class DiagnosticItem:
    service: str
    check: str
    status: DiagnosticStatus
    detail: str
    next_step: str = ""


@dataclass(frozen=True)
class DiagnosticReport:
    live: bool
    items: list[DiagnosticItem]

    @property
    def blockers(self) -> list[DiagnosticItem]:
        return [i for i in self.items if i.status in ("missing", "failed")]

    @property
    def warnings(self) -> list[DiagnosticItem]:
        return [i for i in self.items if i.status == "warn"]

    @property
    def ready(self) -> bool:
        return not self.blockers


ClassInFactory = Callable[[], ContextClient]
NotionFactory = Callable[[str], Any]
AnthropicFactory = Callable[[str], Any]
GeminiFactory = Callable[[str], Any]
HttpClientFactory = Callable[[], ContextClient]


def diagnose_apis(
    cfg: AppConfig,
    *,
    live: bool = False,
    timeout: float = 15.0,
    classin_client_factory: ClassInFactory | None = None,
    notion_client_factory: NotionFactory | None = None,
    anthropic_client_factory: AnthropicFactory | None = None,
    gemini_client_factory: GeminiFactory | None = None,
    http_client_factory: HttpClientFactory | None = None,
) -> DiagnosticReport:
    """Return setup diagnostics for all external services.

    Live probes are deliberately chosen to be read-only or validation-error
    calls. They prove network/auth enough to unblock setup, but do not create
    lessons, pages, reports, or notifications.
    """
    items: list[DiagnosticItem] = []
    items.extend(_classin_items(cfg, live, timeout, classin_client_factory))
    items.extend(_notion_items(cfg, live, notion_client_factory))
    if cfg.llm.provider == "gemini":
        items.extend(_gemini_items(cfg, live, gemini_client_factory))
    else:
        items.extend(_anthropic_items(cfg, live, anthropic_client_factory))
    items.extend(_aligo_items(cfg, live, timeout, http_client_factory))
    return DiagnosticReport(live=live, items=items)


def _classin_items(
    cfg: AppConfig,
    live: bool,
    timeout: float,
    classin_client_factory: ClassInFactory | None,
) -> list[DiagnosticItem]:
    items: list[DiagnosticItem] = []
    sid_missing = _is_missing(cfg.classin.school_id)
    secret_missing = _is_missing(cfg.classin.secret_key)

    items.append(
        DiagnosticItem(
            "ClassIn",
            "SID / secret_key",
            "missing" if sid_missing or secret_missing else "ok",
            "비어 있거나 placeholder 값" if sid_missing or secret_missing else "입력됨",
            "classin.school_id 와 classin.secret_key 를 채우세요."
            if sid_missing or secret_missing
            else "",
        )
    )
    if sid_missing or secret_missing:
        items.append(_skipped("ClassIn", "v1 SSO probe", "ClassIn 키가 필요합니다."))
        items.append(_skipped("ClassIn", "v2 LMS signature probe", "ClassIn 키가 필요합니다."))
        return items

    if not live:
        items.append(_skipped("ClassIn", "v1 SSO probe", "--live 로 실제 서버 응답을 확인하세요."))
        items.append(
            _skipped(
                "ClassIn",
                "v2 LMS signature probe",
                "--live 로 v2 서명 수락 여부를 확인하세요.",
            )
        )
        return items

    factory = classin_client_factory or (
        lambda: ClassInClient(
            base_url=cfg.classin.base_url,
            school_id=cfg.classin.school_id,
            secret_key=cfg.classin.secret_key,
            timeout=timeout,
        )
    )
    try:
        with factory() as client:
            items.append(_probe_classin_v1(client))
            items.append(_probe_classin_v2(client))
    except Exception as exc:
        detail = _safe_error(exc)
        items.append(
            DiagnosticItem(
                "ClassIn",
                "server reachability",
                "failed",
                detail,
                "네트워크, base_url, 프록시/VPN, ClassIn API 허용 상태를 확인하세요.",
            )
        )
    return items


def _probe_classin_v1(client: Any) -> DiagnosticItem:
    body = {
        "courseId": 0,
        "classId": 0,
        "uid": "0",
        "telephone": "00000000000",
        "deviceType": 1,
        "lifeTime": 60,
    }
    try:
        client.call_v1("getLoginLinked", body)
    except ClassInAPIError as exc:
        if exc.errno == 100:
            return DiagnosticItem(
                "ClassIn",
                "v1 SSO probe",
                "ok",
                "서명된 요청이 서버까지 도달했고, 더미 값에 대해 파라미터 오류를 받았습니다.",
                "강한 인증 검증은 실제 uid/course_id/class_id/telephone 로 sso-link 를 실행하세요.",
            )
        return DiagnosticItem(
            "ClassIn",
            "v1 SSO probe",
            "warn" if exc.errno > 0 else "failed",
            f"errno={exc.errno} {exc.message}",
            "실제 SSO 값으로 재확인하거나 ClassIn v1 권한/SID를 확인하세요.",
        )
    return DiagnosticItem(
        "ClassIn",
        "v1 SSO probe",
        "warn",
        "더미 SSO 값이 성공했습니다. 응답값은 보안상 출력하지 않았습니다.",
        "실제 계정/수업 값으로 생성된 링크가 의도한 사용자용인지 확인하세요.",
    )


def _probe_classin_v2(client: Any) -> DiagnosticItem:
    try:
        client.call_v2("/lms/unit/create", {})
    except ClassInAPIError as exc:
        if _is_signature_error(exc):
            return DiagnosticItem(
                "ClassIn",
                "v2 LMS signature probe",
                "failed",
                f"errno={exc.errno} {exc.message}",
                "ClassIn v2 signing key, SID, LMS API 권한 활성화 여부를 확인하세요.",
            )
        if exc.errno > 0:
            return DiagnosticItem(
                "ClassIn",
                "v2 LMS signature probe",
                "ok",
                f"서명은 수락된 것으로 보입니다. 더미 payload 는 errno={exc.errno} 로 거절됨.",
                "실제 코스 ID와 teacherUid 를 채운 뒤 parse-schedule --live 를 제한적으로 테스트하세요.",
            )
        return DiagnosticItem(
            "ClassIn",
            "v2 LMS signature probe",
            "failed",
            f"errno={exc.errno} {exc.message}",
            "HTTP 상태, base_url, 네트워크 연결을 확인하세요.",
        )
    return DiagnosticItem(
        "ClassIn",
        "v2 LMS signature probe",
        "warn",
        "빈 LMS unit 생성 요청이 성공했습니다. 응답값은 보안상 출력하지 않았습니다.",
        "ClassIn 대시보드에 원치 않는 테스트 데이터가 없는지 확인하세요.",
    )


def _notion_items(
    cfg: AppConfig,
    live: bool,
    notion_client_factory: NotionFactory | None,
) -> list[DiagnosticItem]:
    items: list[DiagnosticItem] = []
    if _is_missing(cfg.notion.token):
        items.append(
            DiagnosticItem(
                "Notion",
                "integration token",
                "missing",
                "비어 있거나 placeholder 값",
                "notion.token 에 Integration secret 을 넣으세요.",
            )
        )
        for label, _db_id in _notion_databases(cfg):
            items.append(_skipped("Notion", f"{label} DB access", "Notion 토큰이 필요합니다."))
        return items

    items.append(DiagnosticItem("Notion", "integration token", "ok", "입력됨"))
    factory = notion_client_factory or (lambda token: NotionClient(auth=token))
    client = factory(cfg.notion.token) if live else None

    for label, db_id in _notion_databases(cfg):
        if _is_missing(db_id):
            items.append(
                DiagnosticItem(
                    "Notion",
                    f"{label} DB access",
                    "missing",
                    "DB ID가 비어 있거나 placeholder 값",
                    f"notion.databases.{label} 값을 채우고 Integration 공유 권한을 주세요.",
                )
            )
            continue
        if not live:
            items.append(
                _skipped("Notion", f"{label} DB access", "--live 로 DB 접근 권한을 확인하세요.")
            )
            continue
        try:
            # config 의 ID 는 신규 Notion API 의 data_source_id (setup-notion 산출).
            # notion_repo 와 동일하게 data_sources.query 로 접근 권한을 확인한다.
            client.data_sources.query(data_source_id=db_id, page_size=1)
        except Exception as exc:
            items.append(
                DiagnosticItem(
                    "Notion",
                    f"{label} DB access",
                    "failed",
                    _safe_error(exc),
                    "DB ID가 맞는지, 해당 DB가 Integration 에 공유됐는지 확인하세요.",
                )
            )
            continue
        items.append(DiagnosticItem("Notion", f"{label} DB access", "ok", "retrieve 성공"))
    return items


def _anthropic_items(
    cfg: AppConfig,
    live: bool,
    anthropic_client_factory: AnthropicFactory | None,
) -> list[DiagnosticItem]:
    if _is_missing(cfg.anthropic.api_key):
        return [
            DiagnosticItem(
                "Claude",
                "API key",
                "missing",
                "비어 있거나 placeholder 값",
                "anthropic.api_key 를 채우세요.",
            )
        ]
    if not live:
        return [
            DiagnosticItem("Claude", "API key", "ok", "입력됨"),
            _skipped("Claude", "messages probe", "--live 로 인증과 모델 접근을 확인하세요."),
        ]

    factory = anthropic_client_factory or (lambda key: Anthropic(api_key=key))
    client = factory(cfg.anthropic.api_key)
    try:
        response = client.messages.create(
            model=cfg.anthropic.model,
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply exactly: OK"}],
        )
    except Exception as exc:
        return [
            DiagnosticItem("Claude", "API key", "ok", "입력됨"),
            DiagnosticItem(
                "Claude",
                "messages probe",
                "failed",
                _safe_error(exc),
                "API key, billing/usage limit, model 이름을 확인하세요.",
            ),
        ]

    text = _anthropic_text(response)
    if text.strip().upper() == "OK":
        detail = f"messages.create 성공 ({cfg.anthropic.model})"
        status: DiagnosticStatus = "ok"
    else:
        detail = f"messages.create 응답 확인 필요 ({cfg.anthropic.model})"
        status = "warn"
    return [
        DiagnosticItem("Claude", "API key", "ok", "입력됨"),
        DiagnosticItem("Claude", "messages probe", status, detail),
    ]


def _gemini_items(
    cfg: AppConfig,
    live: bool,
    gemini_client_factory: GeminiFactory | None,
) -> list[DiagnosticItem]:
    if _is_missing(cfg.gemini.api_key):
        return [
            DiagnosticItem(
                "Gemini",
                "API key",
                "missing",
                "비어 있거나 placeholder 값",
                "gemini.api_key 를 채우세요.",
            )
        ]
    if not live:
        return [
            DiagnosticItem("Gemini", "API key", "ok", "입력됨"),
            _skipped("Gemini", "generate probe", "--live 로 인증과 모델 접근을 확인하세요."),
        ]

    try:
        if gemini_client_factory:
            client = gemini_client_factory(cfg.gemini.api_key)
        else:
            from google import genai

            client = genai.Client(api_key=cfg.gemini.api_key)
    except Exception as exc:
        return [
            DiagnosticItem("Gemini", "API key", "ok", "입력됨"),
            DiagnosticItem(
                "Gemini",
                "SDK",
                "failed",
                _safe_error(exc),
                "google-genai 설치 확인: pip install google-genai",
            ),
        ]

    try:
        response = client.models.generate_content(
            model=cfg.gemini.model,
            contents="Reply exactly: OK",
            # thinking 모델(2.5+/3.x)은 reasoning 토큰을 먼저 소비하므로 8 토큰이면 .text 가
            # 비어 WARN 오판이 난다. 짧은 답을 확실히 받도록 여유를 둔다.
            config={"max_output_tokens": 512},
        )
        text = getattr(response, "text", "") or ""
    except Exception as exc:
        return [
            DiagnosticItem("Gemini", "API key", "ok", "입력됨"),
            DiagnosticItem(
                "Gemini",
                "generate probe",
                "failed",
                _safe_error(exc),
                "API key, 사용량/billing, model 이름을 확인하세요.",
            ),
        ]

    if text.strip().upper().startswith("OK"):
        detail = f"generate_content 성공 ({cfg.gemini.model})"
        status: DiagnosticStatus = "ok"
    else:
        detail = f"generate_content 응답 확인 필요 ({cfg.gemini.model})"
        status = "warn"
    return [
        DiagnosticItem("Gemini", "API key", "ok", "입력됨"),
        DiagnosticItem("Gemini", "generate probe", status, detail),
    ]


def _aligo_items(
    cfg: AppConfig,
    live: bool,
    timeout: float,
    http_client_factory: HttpClientFactory | None,
) -> list[DiagnosticItem]:
    items: list[DiagnosticItem] = []
    if cfg.notify.provider != "aligo":
        return [
            DiagnosticItem(
                "Aligo",
                "provider",
                "warn",
                f"현재 provider={cfg.notify.provider}",
                "현재 구현/진단 대상은 aligo 입니다.",
            )
        ]

    missing = [
        name
        for name, value in (
            ("api_key", cfg.notify.aligo.api_key),
            ("user_id", cfg.notify.aligo.user_id),
            ("sender", cfg.notify.aligo.sender),
        )
        if _is_missing(value)
    ]
    items.append(
        DiagnosticItem(
            "Aligo",
            "credentials",
            "missing" if missing else "ok",
            "누락: " + ", ".join(missing) if missing else "입력됨",
            "notify.aligo.api_key/user_id/sender 를 채우세요." if missing else "",
        )
    )
    if cfg.notify.mode == "dry_run":
        items.append(
            DiagnosticItem(
                "Aligo",
                "dispatch mode",
                "ok",
                "dry_run - 실제 카톡/문자는 발송하지 않습니다.",
            )
        )
    else:
        items.append(DiagnosticItem("Aligo", "dispatch mode", "warn", "live"))
        template_missing = [
            name
            for name, value in (
                ("sender_key", cfg.notify.aligo.sender_key),
                (
                    "template_code_missing_homework",
                    cfg.notify.aligo.template_code_missing_homework,
                ),
            )
            if _is_missing(value)
        ]
        items.append(
            DiagnosticItem(
                "Aligo",
                "Kakao template config",
                "missing" if template_missing else "ok",
                "누락: " + ", ".join(template_missing) if template_missing else "입력됨",
                "notify.aligo.sender_key/template_code_missing_homework 를 채우세요."
                if template_missing
                else "",
            )
        )

    if "api_key" in missing or "user_id" in missing:
        items.append(
            _skipped("Aligo", "Kakao balance probe", "Aligo API 키와 user_id가 필요합니다.")
        )
        return items
    if not live:
        items.append(
            _skipped("Aligo", "Kakao balance probe", "--live 로 잔여건수 조회를 확인하세요.")
        )
        return items

    factory = http_client_factory or (lambda: httpx.Client(timeout=timeout))
    try:
        with factory() as client:
            response = client.post(
                "https://kakaoapi.aligo.in/akv10/heartinfo/",
                data={
                    "apikey": cfg.notify.aligo.api_key,
                    "userid": cfg.notify.aligo.user_id,
                },
            )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        items.append(
            DiagnosticItem(
                "Aligo",
                "Kakao balance probe",
                "failed",
                _safe_error(exc),
                "Aligo 카카오 API 키/user_id 또는 방화벽을 확인하세요.",
            )
        )
        return items

    code = _int_value(payload.get("result_code", payload.get("code")))
    if code == 1:
        counts = _count_summary(payload)
        items.append(
            DiagnosticItem(
                "Aligo",
                "Kakao balance probe",
                "ok",
                counts or "잔여건수 조회 성공",
            )
        )
    else:
        items.append(
            DiagnosticItem(
                "Aligo",
                "Kakao balance probe",
                "failed",
                f"result_code={code} {payload.get('message') or payload.get('msg') or ''}".strip(),
                "Aligo 카카오 API 사용 신청/인증 상태를 확인하세요.",
            )
        )
    return items


def _notion_databases(cfg: AppConfig) -> list[tuple[str, str | None]]:
    return [
        ("students", cfg.notion.databases.students),
        ("lessons", cfg.notion.databases.lessons),
        ("reports", cfg.notion.databases.reports),
        ("memos", cfg.notion.databases.memos),
        ("exams", cfg.notion.databases.exams),
    ]


def _is_signature_error(exc: ClassInAPIError) -> bool:
    text = f"{exc.errno} {exc.message}".lower()
    return exc.errno in _SIGNATURE_ERROR_CODES or any(
        marker.lower() in text for marker in _SIGNATURE_ERROR_MARKERS
    )


def _is_missing(value: str | None) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    upper = text.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def _skipped(service: str, check: str, next_step: str) -> DiagnosticItem:
    return DiagnosticItem(service, check, "skipped", "실행하지 않음", next_step)


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    text = " ".join(text.split())
    return text[:240]


def _anthropic_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []):
        if getattr(block, "type", "") == "text":
            parts.append(str(getattr(block, "text", "")))
    return "".join(parts)


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _count_summary(payload: dict[str, Any]) -> str:
    counts = [
        f"{key}={value}"
        for key, value in sorted(payload.items())
        if key.endswith("_CNT") or key in {"ALT_CNT", "FTS_CNT", "FTI_CNT"}
    ]
    return ", ".join(counts)
