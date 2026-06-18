"""카카오 알림톡 디스패처.

지침(02_guidelines §4, feedback_mvp_scope):
- MVP 단계: 템플릿 심사 2~3주 걸리므로 실제 발송 대신 dry_run 모드로 예시 문구만.
- 실제 발송은 Standard 티어부터 포함.
- 제공자: 알리고 or 솔라피 릴레이.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ..config import AppConfig
from .message import OutgoingMessage

log = logging.getLogger(__name__)

HISTORY_FILE = "notify_history.jsonl"
ALIGO_ALIMTALK_SEND_URL = "https://kakaoapi.aligo.in/akv10/alimtalk/send/"
ALIGO_BATCH_LIMIT = 500

HttpClientFactory = Callable[[], Any]


async def dispatch_kakao(
    cfg: AppConfig,
    messages: list[OutgoingMessage],
    *,
    http_client_factory: HttpClientFactory | None = None,
) -> None:
    await dispatch_notifications(
        cfg,
        messages,
        event_type="missing_homework",
        http_client_factory=http_client_factory,
    )


def record_notification_history(
    cfg: AppConfig,
    messages: list[OutgoingMessage],
    *,
    event_type: str,
    provider: str,
    status: str,
    error: str | None = None,
) -> None:
    _append_history(
        cfg,
        messages,
        event_type=event_type,
        provider=provider,
        status=status,
        error=error,
    )


async def dispatch_notifications(
    cfg: AppConfig,
    messages: list[OutgoingMessage],
    *,
    event_type: str,
    http_client_factory: HttpClientFactory | None = None,
) -> None:
    if not messages:
        return
    if cfg.notify.mode == "dry_run":
        artifact_path = _dry_run_dump(cfg, messages)
        _append_history(
            cfg,
            messages,
            event_type=event_type,
            provider="dry_run",
            status="dry_run",
            artifact_path=artifact_path,
        )
        return
    if cfg.notify.provider == "aligo":
        try:
            result = _send_via_aligo(
                cfg,
                messages,
                event_type=event_type,
                http_client_factory=http_client_factory,
            )
        except Exception as exc:
            _append_history(
                cfg,
                messages,
                event_type=event_type,
                provider=cfg.notify.provider,
                status="failed",
                error=str(exc),
            )
            raise
        _append_history(
            cfg,
            messages,
            event_type=event_type,
            provider=cfg.notify.provider,
            status="sent",
            provider_message_id=result.get("message_id"),
            provider_response=result.get("payload"),
        )
    else:
        raise NotImplementedError(f"provider not supported yet: {cfg.notify.provider}")


def load_notification_history(cfg: AppConfig, *, limit: int = 100) -> list[dict[str, Any]]:
    path = notification_history_path(cfg)
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("skip malformed notification history line in %s", path)
    rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return rows[:limit]


def notification_history_path(cfg: AppConfig) -> Path:
    return Path(cfg.reports.output_dir) / HISTORY_FILE


def _dry_run_dump(cfg: AppConfig, messages: list[OutgoingMessage]) -> Path:
    out_dir = Path(cfg.reports.output_dir) / "notify_dry_run"
    out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{stamp}.md"
    lines = [f"# Dry-run kakao dispatch @ {stamp}", ""]
    for m in messages:
        lines += [
            f"## {m.student_name} ({m.student_classin_id})",
            f"phone: {m.parent_phone or '-'}",
            "",
            m.message or "(empty)",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("kakao dry-run dumped to %s (%d messages)", path, len(messages))
    return path


def _append_history(
    cfg: AppConfig,
    messages: list[OutgoingMessage],
    *,
    event_type: str,
    provider: str,
    status: str,
    artifact_path: Path | None = None,
    error: str | None = None,
    provider_message_id: str | None = None,
    provider_response: dict[str, Any] | None = None,
) -> None:
    from datetime import datetime, timezone

    path = notification_history_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as f:
        for message in messages:
            row = {
                "created_at": now,
                "event_type": event_type,
                "provider": provider,
                "status": status,
                "student_classin_id": message.student_classin_id,
                "student_name": message.student_name,
                "parent_phone": message.parent_phone,
                "message": message.message,
                "quality_status": message.quality_status,
                "quality_score": message.quality_score,
                "quality_warnings": message.quality_warnings,
                "artifact_path": str(artifact_path) if artifact_path else None,
                "error": error,
                "provider_message_id": provider_message_id,
                "provider_response": provider_response,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _send_via_aligo(
    cfg: AppConfig,
    messages: list[OutgoingMessage],
    *,
    event_type: str,
    http_client_factory: HttpClientFactory | None = None,
) -> dict[str, Any]:
    _validate_aligo_live_request(cfg, messages, event_type=event_type)
    factory = http_client_factory or (lambda: httpx.Client(timeout=15.0))
    payload = _aligo_alimtalk_payload(cfg, messages, event_type=event_type)
    with factory() as client:
        response = client.post(ALIGO_ALIMTALK_SEND_URL, data=payload)
    response.raise_for_status()
    body = response.json()
    _raise_for_aligo_failure(body)
    return {
        "message_id": _aligo_message_id(body),
        "payload": _aligo_response_summary(body),
    }


def _validate_aligo_live_request(
    cfg: AppConfig,
    messages: list[OutgoingMessage],
    *,
    event_type: str,
) -> None:
    if event_type != "missing_homework":
        raise NotImplementedError(f"aligo live template not configured for event_type={event_type}")
    missing_config = [
        name
        for name, value in (
            ("notify.aligo.api_key", cfg.notify.aligo.api_key),
            ("notify.aligo.user_id", cfg.notify.aligo.user_id),
            ("notify.aligo.sender", cfg.notify.aligo.sender),
            ("notify.aligo.sender_key", cfg.notify.aligo.sender_key),
            (
                "notify.aligo.template_code_missing_homework",
                cfg.notify.aligo.template_code_missing_homework,
            ),
        )
        if _is_blank(value)
    ]
    if missing_config:
        raise ValueError("aligo live config missing: " + ", ".join(missing_config))
    if len(messages) > ALIGO_BATCH_LIMIT:
        raise ValueError(f"aligo batch limit exceeded: {len(messages)} > {ALIGO_BATCH_LIMIT}")

    invalid: list[str] = []
    for message in messages:
        label = message.student_name or message.student_classin_id or "unknown"
        if _is_blank(message.parent_phone):
            invalid.append(f"{label}: parent_phone missing")
        if _is_blank(message.message):
            invalid.append(f"{label}: message missing")
        if message.quality_status != "ready":
            invalid.append(f"{label}: quality_status={message.quality_status}")
    if invalid:
        raise ValueError("aligo live message blocked: " + "; ".join(invalid[:10]))


def _aligo_alimtalk_payload(
    cfg: AppConfig,
    messages: list[OutgoingMessage],
    *,
    event_type: str,
) -> dict[str, str]:
    subject = _aligo_subject(cfg, event_type=event_type)
    payload = {
        "apikey": cfg.notify.aligo.api_key,
        "userid": cfg.notify.aligo.user_id,
        "senderkey": cfg.notify.aligo.sender_key,
        "tpl_code": cfg.notify.aligo.template_code_missing_homework,
        "sender": _digits(cfg.notify.aligo.sender),
        "failover": "N",
    }
    for index, message in enumerate(messages, start=1):
        payload[f"receiver_{index}"] = _digits(message.parent_phone or "")
        payload[f"recvname_{index}"] = message.student_name
        payload[f"subject_{index}"] = subject
        payload[f"message_{index}"] = message.message
    return payload


def _aligo_subject(cfg: AppConfig, *, event_type: str) -> str:
    labels = {"missing_homework": "숙제 안내"}
    academy = cfg.academy.name.strip() or "학원"
    return f"{academy} {labels.get(event_type, '안내')}"[:50]


def _raise_for_aligo_failure(body: dict[str, Any]) -> None:
    code = body.get("code", body.get("result_code"))
    success = str(code) in {"0", "1"} if "code" not in body else str(code) == "0"
    info = body.get("info") if isinstance(body.get("info"), dict) else {}
    failed_count = _int_value(info.get("fcnt"))
    if success and failed_count == 0:
        return
    message = body.get("message") or body.get("msg") or "unknown error"
    raise RuntimeError(f"Aligo alimtalk failed: code={code} fcnt={failed_count} {message}")


def _aligo_message_id(body: dict[str, Any]) -> str | None:
    info = body.get("info") if isinstance(body.get("info"), dict) else {}
    value = info.get("mid") or body.get("mid")
    return str(value) if value not in (None, "") else None


def _aligo_response_summary(body: dict[str, Any]) -> dict[str, Any]:
    info = body.get("info") if isinstance(body.get("info"), dict) else {}
    return {
        "code": body.get("code", body.get("result_code")),
        "message": body.get("message") or body.get("msg"),
        "type": info.get("type"),
        "mid": info.get("mid"),
        "success_count": _int_value(info.get("scnt")),
        "failed_count": _int_value(info.get("fcnt")),
        "unit": info.get("unit"),
        "total": info.get("total"),
    }


def _digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _is_blank(value: str | None) -> bool:
    text = str(value or "").strip()
    return not text or "REPLACE_ME" in text or text == "TODO"


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
