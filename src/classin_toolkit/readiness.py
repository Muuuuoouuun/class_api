"""Test-version readiness checks.

This module is intentionally offline-only: it checks whether config values look
usable before the operator spends time on Notion/ClassIn/Claude calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import AppConfig

ReadinessMode = Literal["local-demo", "classin-live", "kakao-live"]
ReadinessStatus = Literal["ok", "warn", "missing", "blocked"]

_PLACEHOLDER_MARKERS = (
    "REPLACE_ME",
    "CHANGE_ME",
    "TODO",
    "YOUR_",
    "INSERT_",
    "LOCAL_DEMO",
)
_SAMPLE_PAYLOADS = (
    "attendance_sample.json",
    "end_summary_sample.json",
    "homework_submit_sample.json",
)


@dataclass(frozen=True)
class ReadinessItem:
    stage: str
    label: str
    status: ReadinessStatus
    detail: str
    fix: str = ""


@dataclass(frozen=True)
class ReadinessReport:
    mode: ReadinessMode
    items: list[ReadinessItem]

    @property
    def blockers(self) -> list[ReadinessItem]:
        return [i for i in self.items if i.status in ("missing", "blocked")]

    @property
    def warnings(self) -> list[ReadinessItem]:
        return [i for i in self.items if i.status == "warn"]

    @property
    def ready(self) -> bool:
        return not self.blockers


def check_readiness(
    cfg: AppConfig,
    *,
    mode: ReadinessMode = "local-demo",
    project_root: Path | None = None,
) -> ReadinessReport:
    if mode not in ("local-demo", "classin-live", "kakao-live"):
        raise ValueError("mode must be one of: local-demo, classin-live, kakao-live")

    root = project_root or Path.cwd()
    items: list[ReadinessItem] = []
    items.extend(_local_demo_items(cfg, root))
    if mode in ("classin-live", "kakao-live"):
        items.extend(_classin_live_items(cfg))
    if mode == "kakao-live":
        items.extend(_kakao_live_items(cfg))
    return ReadinessReport(mode=mode, items=items)


def _local_demo_items(cfg: AppConfig, project_root: Path) -> list[ReadinessItem]:
    items = [
        _required("local-demo", "학원 이름", cfg.academy.name, "academy.name 을 입력하세요."),
        _required(
            "local-demo",
            "Notion 토큰",
            cfg.notion.token,
            "notion.token 에 학원 Notion Integration 토큰을 넣으세요.",
        ),
        _required(
            "local-demo",
            "학생 Master DB ID",
            cfg.notion.databases.students,
            "notion.databases.students 를 채우세요.",
        ),
        _required(
            "local-demo",
            "수업 기록 DB ID",
            cfg.notion.databases.lessons,
            "notion.databases.lessons 를 채우세요.",
        ),
        _required(
            "local-demo",
            "리포트 DB ID",
            cfg.notion.databases.reports,
            "notion.databases.reports 를 채우세요.",
        ),
        _required(
            "local-demo",
            "Claude API 키",
            cfg.anthropic.api_key,
            "anthropic.api_key 를 채우세요.",
        ),
        _path_value("local-demo", "일일 HTML 출력 경로", cfg.output.daily.path),
        _path_value("local-demo", "주간 HTML 출력 경로", cfg.output.weekly.path),
        _path_value("local-demo", "Webhook 원본 덤프 경로", cfg.webhook.dump_dir),
    ]

    if _is_missing(cfg.notion.databases.exams):
        items.append(
            ReadinessItem(
                "local-demo",
                "시험 DB ID",
                "warn",
                "exam import/sweep 기능은 notion.databases.exams 설정 후 사용할 수 있습니다.",
                "시험 기능을 쓸 계획이면 setup-notion 결과의 exams DB ID를 채우세요.",
            )
        )
    else:
        items.append(
            ReadinessItem("local-demo", "시험 DB ID", "ok", _mask(cfg.notion.databases.exams))
        )

    if cfg.output.memo.mode == "notion":
        items.append(
            _required(
                "local-demo",
                "메모 DB ID",
                cfg.notion.databases.memos,
                "메모 기능을 쓸 거면 notion.databases.memos 를 채우세요.",
            )
        )
    else:
        items.append(
            ReadinessItem("local-demo", "메모 DB ID", "warn", "memo.mode=off 라서 생략됨")
        )

    if cfg.notify.mode == "dry_run":
        items.append(ReadinessItem("local-demo", "카톡 발송 모드", "ok", "dry_run"))
    else:
        items.append(
            ReadinessItem(
                "local-demo",
                "카톡 발송 모드",
                "warn",
                "local-demo 에서는 dry_run 권장",
                "notify.mode 를 dry_run 으로 두세요.",
            )
        )

    for name in _SAMPLE_PAYLOADS:
        path = project_root / "samples" / name
        items.append(
            ReadinessItem(
                "local-demo",
                f"샘플 페이로드 {name}",
                "ok" if path.exists() else "missing",
                str(path),
                "samples 디렉터리에 샘플 Webhook JSON 을 준비하세요.",
            )
        )

    if _is_missing(cfg.classin.school_id) or _is_missing(cfg.classin.secret_key):
        items.append(
            ReadinessItem(
                "local-demo",
                "ClassIn API 키",
                "warn",
                "로컬 샘플 재생만 할 때는 없어도 됨",
            )
        )
    else:
        items.append(ReadinessItem("local-demo", "ClassIn API 키", "ok", "입력됨"))

    return items


def _classin_live_items(cfg: AppConfig) -> list[ReadinessItem]:
    items = [
        _required(
            "classin-live",
            "ClassIn SID",
            cfg.classin.school_id,
            "classin.school_id 에 실제 SID 를 넣으세요.",
        ),
        _required(
            "classin-live",
            "ClassIn secret_key",
            cfg.classin.secret_key,
            "classin.secret_key 에 v2 signing key 를 넣으세요.",
        ),
        _required(
            "classin-live",
            "Webhook SafeKey secret",
            cfg.classin.webhook_secret,
            "classin.webhook_secret 값을 확정해 넣으세요.",
        ),
    ]
    if cfg.output.daily.public_url_base:
        items.append(
            ReadinessItem(
                "classin-live",
                "리포트 공개 URL",
                "ok",
                cfg.output.daily.public_url_base,
            )
        )
    else:
        items.append(
            ReadinessItem(
                "classin-live",
                "리포트 공개 URL",
                "warn",
                "HTML 링크 공유 전까지는 비어 있어도 됨",
                "output.daily.public_url_base 에 Cloudflare Tunnel URL 을 넣으세요.",
            )
        )
    items.append(
        ReadinessItem(
            "classin-live",
            "Webhook 공개 URL 등록",
            "warn",
            "config 만으로 확인 불가",
            "Cloudflare Tunnel URL + /classin/webhook 을 ClassIn Datasub 에 등록하세요.",
        )
    )
    return items


def _kakao_live_items(cfg: AppConfig) -> list[ReadinessItem]:
    items: list[ReadinessItem] = []
    if cfg.notify.mode != "live":
        items.append(
            ReadinessItem(
                "kakao-live",
                "카톡 live 모드",
                "missing",
                f"현재 notify.mode={cfg.notify.mode}",
                "notify.mode 를 live 로 바꾸세요.",
            )
        )
    else:
        items.append(ReadinessItem("kakao-live", "카톡 live 모드", "ok", "live"))

    if cfg.notify.provider != "aligo":
        items.append(
            ReadinessItem(
                "kakao-live",
                "카톡 provider",
                "blocked",
                f"{cfg.notify.provider} provider 는 아직 구현되지 않음",
                "현재 구현 대상은 aligo 입니다.",
            )
        )
    else:
        items.extend(
            [
                _required(
                    "kakao-live",
                    "알리고 API 키",
                    cfg.notify.aligo.api_key,
                    "notify.aligo.api_key 를 채우세요.",
                ),
                _required(
                    "kakao-live",
                    "알리고 user_id",
                    cfg.notify.aligo.user_id,
                    "notify.aligo.user_id 를 채우세요.",
                ),
                _required(
                    "kakao-live",
                    "알리고 발신번호",
                    cfg.notify.aligo.sender,
                    "notify.aligo.sender 를 채우세요.",
                ),
            ]
        )

    items.append(
        ReadinessItem(
            "kakao-live",
            "실제 알림톡 발송 구현",
            "blocked",
            "notify.dispatcher 의 aligo live mode 가 아직 미구현",
            "템플릿 승인 후 _send_via_aligo 를 구현하세요.",
        )
    )
    return items


def _required(stage: str, label: str, value: str | None, fix: str) -> ReadinessItem:
    if _is_missing(value):
        return ReadinessItem(stage, label, "missing", "비어 있거나 placeholder 값", fix)
    return ReadinessItem(stage, label, "ok", _mask(value))


def _path_value(stage: str, label: str, value: str | None) -> ReadinessItem:
    if _is_missing(value):
        return ReadinessItem(
            stage,
            label,
            "missing",
            "경로가 비어 있음",
            "config 경로를 채우세요.",
        )
    return ReadinessItem(stage, label, "ok", str(value))


def _is_missing(value: str | None) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    upper = text.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def _mask(value: str | None) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "입력됨"
    return f"{text[:4]}...{text[-4:]}"
