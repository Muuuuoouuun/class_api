"""로컬 운영 UI (FastAPI).

원장/컨설턴트가 반복 실행하는 CLI 작업을 브라우저 버튼으로 감싼 얇은 계층이다.
비즈니스 로직은 pipelines/와 intelligence/의 기존 함수를 그대로 호출한다.
"""
from __future__ import annotations

import html
import json
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import AppConfig, DEFAULT_CONFIG_PATH, load_config
from .notify.dispatcher import load_notification_history, notification_history_path
from .pipelines.daily import render_daily
from .pipelines.missing_homework import query_missing_homework, sweep_missing_homework
from .pipelines.weekly import approve_all, generate_drafts
from .storage.notion_repo import NotionRepo


def create_app(
    config: AppConfig | None = None,
    *,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> FastAPI:
    app = FastAPI(title="ClassIn Toolkit UI", version="0.1.0")
    state = _ConfigState(config=config, config_path=Path(config_path))

    @app.get("/health")
    async def health() -> dict:
        cfg, error = state.load()
        return {
            "ok": error is None,
            "academy": cfg.academy.name if cfg else None,
            "error": error,
        }

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        cfg, error = state.load()
        return HTMLResponse(_render_shell(_status_payload(cfg, error, state.config_path)))

    @app.get("/api/status")
    async def api_status() -> dict:
        cfg, error = state.load()
        return _status_payload(cfg, error, state.config_path)

    @app.get("/api/missing-homework")
    async def api_missing_homework(
        window_hours: int = 24,
        lesson_id: str | None = None,
    ) -> dict:
        cfg = _require_config(state)
        rows = query_missing_homework(
            cfg,
            window_hours=window_hours,
            lesson_id=(lesson_id or "").strip() or None,
        )
        history = load_notification_history(cfg, limit=300)
        return _missing_homework_payload(rows, history)

    @app.get("/api/notifications")
    async def api_notifications(limit: int = 80) -> dict:
        cfg = _require_config(state)
        rows = load_notification_history(cfg, limit=limit)
        return {"ok": True, "items": rows, "summary": _notification_summary(rows)}

    @app.post("/api/render-daily")
    async def api_render_daily(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        payload = await _json_payload(request)
        raw_date = (payload.get("date") or "").strip()
        target = date_cls.fromisoformat(raw_date) if raw_date else None
        result = render_daily(cfg, target=target)
        if not result:
            return _ok("daily mode가 notion이라 HTML 생성을 건너뛰었습니다.")
        return _ok(
            "일일 현황 HTML을 생성했습니다.",
            path=str(result.path) if result.path else None,
            public_url=result.public_url,
        )

    @app.post("/api/generate-weekly-drafts")
    async def api_generate_weekly_drafts() -> JSONResponse:
        cfg = _require_config(state)
        count = generate_drafts(cfg)
        return _ok(f"주간 드래프트 {count}건을 생성했습니다.", count=count)

    @app.post("/api/approve-weekly")
    async def api_approve_weekly(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        payload = await _json_payload(request)
        week = (payload.get("week") or "").strip()
        if not week:
            raise HTTPException(status_code=400, detail="week 값이 필요합니다.")
        period_start = datetime.fromisoformat(week).replace(tzinfo=timezone.utc)
        count = approve_all(cfg, period_start=period_start)
        return _ok(f"주간 리포트 {count}건을 승인 처리했습니다.", count=count)

    @app.post("/api/sweep-missing-homework")
    async def api_sweep_missing_homework(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        payload = await _json_payload(request)
        window_hours = int(payload.get("window_hours") or 24)
        lesson_id = (payload.get("lesson_id") or "").strip() or None
        count = sweep_missing_homework(cfg, window_hours=window_hours, lesson_id=lesson_id)
        return _ok(f"미제출 알림 {count}건을 처리했습니다.", count=count)

    @app.post("/api/write-memo")
    async def api_write_memo(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        payload = await _json_payload(request)
        classin_id = (payload.get("classin_id") or "").strip()
        text = (payload.get("text") or "").strip()
        tag = (payload.get("tag") or "").strip() or None
        if not classin_id or not text:
            raise HTTPException(status_code=400, detail="classin_id와 text가 필요합니다.")
        if cfg.output.memo.mode == "off":
            return _ok("memo mode가 off라 기록을 건너뛰었습니다.")
        page_id = NotionRepo.from_config(cfg).write_memo(
            student_classin_id=classin_id,
            text=text,
            tag=tag,
        )
        return _ok("메모를 저장했습니다.", page_id=page_id)

    @app.post("/api/agent")
    async def api_agent(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        payload = await _json_payload(request)
        question = (payload.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question 값이 필요합니다.")
        from .intelligence.agent import run_agent_turn

        answer, _messages = run_agent_turn(cfg, [{"role": "user", "content": question}])
        return _ok("AI 응답을 생성했습니다.", answer=answer)

    @app.get("/reports/{kind}/{filename}")
    async def serve_report(kind: str, filename: str) -> FileResponse:
        cfg = _require_config(state)
        if kind not in ("daily", "weekly"):
            raise HTTPException(status_code=404)
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(status_code=400)
        base = Path(cfg.output.daily.path) if kind == "daily" else Path(cfg.output.weekly.path)
        path = base / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="text/html; charset=utf-8")

    return app


class _ConfigState:
    def __init__(self, *, config: AppConfig | None, config_path: Path) -> None:
        self._config = config
        self.config_path = config_path

    def load(self) -> tuple[AppConfig | None, str | None]:
        if self._config:
            return self._config, None
        try:
            return load_config(self.config_path), None
        except Exception as exc:
            return None, str(exc)


def _require_config(state: _ConfigState) -> AppConfig:
    cfg, error = state.load()
    if cfg:
        return cfg
    raise HTTPException(status_code=400, detail=error or "config.yaml을 읽을 수 없습니다.")


async def _json_payload(request: Request) -> dict[str, Any]:
    try:
        body = await request.body()
        if not body:
            return {}
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="JSON body를 읽을 수 없습니다.") from exc


def _ok(message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, **extra})


def _status_payload(
    cfg: AppConfig | None,
    error: str | None,
    config_path: Path,
) -> dict[str, Any]:
    if not cfg:
        return {
            "ok": False,
            "config_path": str(config_path),
            "error": error,
            "academy": None,
            "webhook": None,
            "output": None,
            "counts": {},
        }

    daily_dir = Path(cfg.output.daily.path)
    weekly_dir = Path(cfg.output.weekly.path)
    incoming_dir = Path(cfg.webhook.dump_dir)
    return {
        "ok": True,
        "config_path": str(config_path),
        "error": None,
        "academy": cfg.academy.name,
        "webhook": {
            "host": cfg.webhook.host,
            "port": cfg.webhook.port,
            "dump_dir": cfg.webhook.dump_dir,
        },
        "output": {
            "daily_mode": cfg.output.daily.mode,
            "daily_path": cfg.output.daily.path,
            "weekly_mode": cfg.output.weekly.mode,
            "weekly_path": cfg.output.weekly.path,
            "memo_mode": cfg.output.memo.mode,
            "notify_mode": cfg.notify.mode,
        },
        "counts": {
            "incoming_json": _count_files(incoming_dir, "*.json"),
            "daily_html": _count_files(daily_dir, "*.html"),
            "weekly_html": _count_files(weekly_dir, "*.html"),
            "weekly_indexes": _count_files(weekly_dir, "*_drafts.json"),
            "notification_history": _count_history_rows(notification_history_path(cfg)),
        },
    }


def _count_files(path: Path, pattern: str) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.glob(pattern) if p.is_file())


def _count_history_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _missing_homework_payload(rows: list[dict], history: list[dict[str, Any]]) -> dict[str, Any]:
    latest_by_student = _latest_notification_by_student(history)
    items = []
    for row in rows:
        student_id = row.get("student_classin_id") or ""
        latest = latest_by_student.get(student_id)
        items.append(
            {
                "student_classin_id": student_id,
                "student_name": row.get("student_name") or "미등록",
                "class_name": row.get("student_class_name") or "",
                "parent_phone": row.get("parent_phone") or "",
                "has_parent_phone": bool(row.get("parent_phone")),
                "lesson_classin_id": row.get("lesson_classin_id") or "",
                "course_classin_id": row.get("course_classin_id") or "",
                "date": row.get("date") or "",
                "attendance": row.get("attendance") or "",
                "homework_late": row.get("homework_late"),
                "homework_score": row.get("homework_score"),
                "notification_status": latest.get("status") if latest else "pending",
                "notification_at": latest.get("created_at") if latest else "",
                "notification_provider": latest.get("provider") if latest else "",
            }
        )

    summary = {
        "total_missing": len(items),
        "with_parent_phone": sum(1 for item in items if item["has_parent_phone"]),
        "no_parent_phone": sum(1 for item in items if not item["has_parent_phone"]),
        "pending": sum(1 for item in items if item["notification_status"] == "pending"),
        "dry_run": sum(1 for item in items if item["notification_status"] == "dry_run"),
        "sent": sum(1 for item in items if item["notification_status"] == "sent"),
        "failed": sum(1 for item in items if item["notification_status"] == "failed"),
    }
    return {"ok": True, "summary": summary, "items": items}


def _latest_notification_by_student(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in history:
        student_id = row.get("student_classin_id")
        if not student_id or student_id in latest:
            continue
        latest[student_id] = row
    return latest


def _notification_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(rows),
        "dry_run": sum(1 for row in rows if row.get("status") == "dry_run"),
        "sent": sum(1 for row in rows if row.get("status") == "sent"),
        "failed": sum(1 for row in rows if row.get("status") == "failed"),
    }


def _render_shell(status: dict[str, Any]) -> str:
    status_json = _json_for_script(status)
    today = date_cls.today().isoformat()
    week_start = _week_start(date_cls.today()).isoformat()
    title = html.escape(status.get("academy") or "ClassIn Toolkit")
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ClassIn Toolkit UI</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #172033;
      --muted: #657085;
      --primary: #0f766e;
      --primary-strong: #0b5f59;
      --accent: #b45309;
      --danger: #b42318;
      --ok: #16784b;
      --shadow: 0 1px 2px rgba(16, 24, 40, .07);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      letter-spacing: 0;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      font-weight: 700;
    }}
    main {{
      width: min(1180px, calc(100vw - 32px));
      margin: 18px auto 32px;
    }}
    .status-strip {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .metric, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    .metric {{
      padding: 12px 14px;
      min-height: 74px;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .metric strong {{
      display: block;
      margin-top: 6px;
      font-size: 22px;
      line-height: 1.1;
    }}
    .metric.warn strong {{ color: var(--accent); }}
    .metric.alert strong {{ color: var(--danger); }}
    .grid {{
      display: grid;
      grid-template-columns: 1.2fr .8fr;
      gap: 14px;
      align-items: start;
    }}
    .panel {{
      padding: 16px;
    }}
    .panel + .panel {{
      margin-top: 14px;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 15px;
      line-height: 1.2;
    }}
    .actions {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.3;
    }}
    input, textarea {{
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      color: var(--text);
      background: #fff;
      font: inherit;
      font-size: 14px;
    }}
    textarea {{
      min-height: 92px;
      resize: vertical;
    }}
    button {{
      min-height: 38px;
      border: 1px solid var(--primary);
      border-radius: 6px;
      padding: 8px 12px;
      background: var(--primary);
      color: #fff;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
    }}
    button.secondary {{
      border-color: var(--line);
      background: #fff;
      color: var(--text);
    }}
    button:hover {{
      background: var(--primary-strong);
    }}
    button.secondary:hover {{
      background: #eef2f6;
    }}
    .row {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: end;
    }}
    .stack {{
      display: grid;
      gap: 10px;
    }}
    .log {{
      min-height: 220px;
      max-height: 420px;
      overflow: auto;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #111827;
      color: #e5e7eb;
      padding: 12px;
      font: 13px/1.5 Consolas, "Cascadia Mono", monospace;
      white-space: pre-wrap;
    }}
    .table-wrap {{
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
      font-size: 13px;
    }}
    th, td {{
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #f8fafc;
      color: var(--muted);
      font-weight: 650;
      white-space: nowrap;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .empty {{
      padding: 18px;
      color: var(--muted);
      text-align: center;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #fbfcfd;
    }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      white-space: nowrap;
      font-size: 12px;
    }}
    .status-pill.pending {{ color: var(--accent); background: #fff8eb; border-color: #f3d2a1; }}
    .status-pill.dry_run {{ color: #0f5f8c; background: #edf7ff; border-color: #b7dcf3; }}
    .status-pill.sent {{ color: var(--ok); background: #effaf4; border-color: rgba(22, 120, 75, .25); }}
    .status-pill.failed {{ color: var(--danger); background: #fff1f0; border-color: rgba(180, 35, 24, .25); }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      background: #fff;
    }}
    .badge.ok {{
      color: var(--ok);
      border-color: rgba(22, 120, 75, .25);
      background: #effaf4;
    }}
    .badge.error {{
      color: var(--danger);
      border-color: rgba(180, 35, 24, .25);
      background: #fff1f0;
    }}
    dl {{
      display: grid;
      grid-template-columns: 120px 1fr;
      gap: 8px 12px;
      margin: 0;
      font-size: 13px;
      line-height: 1.45;
    }}
    dt {{ color: var(--muted); }}
    dd {{
      margin: 0;
      overflow-wrap: anywhere;
    }}
    @media (max-width: 860px) {{
      header {{
        align-items: flex-start;
        flex-direction: column;
      }}
      .status-strip, .grid, .actions {{
        grid-template-columns: 1fr;
      }}
      main {{
        width: min(100vw - 20px, 720px);
        margin-top: 10px;
      }}
      dl {{
        grid-template-columns: 1fr;
        gap: 3px;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <span id="configBadge" class="badge">config</span>
  </header>
  <main>
    <section class="status-strip">
      <div class="metric"><span>Webhook 원본</span><strong id="incomingCount">0</strong></div>
      <div class="metric warn"><span>숙제 미제출</span><strong id="missingCount">0</strong></div>
      <div class="metric alert"><span>연락처 없음</span><strong id="noPhoneCount">0</strong></div>
      <div class="metric"><span>문구 생성</span><strong id="dryRunCount">0</strong></div>
      <div class="metric"><span>발송 완료</span><strong id="sentCount">0</strong></div>
      <div class="metric alert"><span>발송 실패</span><strong id="failedCount">0</strong></div>
    </section>

    <section class="grid">
      <div>
        <div class="panel">
          <h2>오늘 미제출 상황</h2>
          <div class="actions">
            <label>조회 시간<input id="windowHours" type="number" min="1" step="1" value="24"></label>
            <label>수업 ID<input id="lessonId" type="text"></label>
            <button data-action="refreshMissing" class="secondary">목록 새로고침</button>
            <button data-action="sweepMissing">미제출 sweep</button>
          </div>
          <div id="missingTable" style="margin-top:12px"></div>
        </div>

        <div class="panel">
          <h2>알림 발송 현황</h2>
          <div id="notificationTable"></div>
        </div>

        <div class="panel">
          <h2>리포트</h2>
          <div class="actions">
            <div class="row">
              <label>일자<input id="dailyDate" type="date" value="{today}"></label>
              <button data-action="renderDaily">생성</button>
            </div>
            <div class="row">
              <label>주 시작일<input id="weekDate" type="date" value="{week_start}"></label>
              <button data-action="approveWeekly" class="secondary">승인</button>
            </div>
            <button data-action="generateWeekly">주간 드래프트 생성</button>
            <button data-action="refreshStatus" class="secondary">상태 새로고침</button>
          </div>
        </div>

        <div class="panel">
          <h2>메모</h2>
          <div class="stack">
            <div class="actions">
              <label>ClassIn ID<input id="memoClassinId" type="text"></label>
              <label>태그<input id="memoTag" type="text"></label>
            </div>
            <label>내용<textarea id="memoText"></textarea></label>
            <button data-action="writeMemo">메모 저장</button>
          </div>
        </div>

        <div class="panel">
          <h2>AI 질문</h2>
          <div class="stack">
            <label>질문<textarea id="agentQuestion"></textarea></label>
            <button data-action="askAgent">질문 보내기</button>
          </div>
        </div>
      </div>

      <aside>
        <div class="panel">
          <h2>설정</h2>
          <dl id="settings"></dl>
        </div>
        <div class="panel">
          <h2>실행 로그</h2>
          <div id="log" class="log"></div>
        </div>
      </aside>
    </section>
  </main>

  <script id="initial-status" type="application/json">{status_json}</script>
  <script>
    const log = document.querySelector("#log");
    const statusNode = document.querySelector("#initial-status");
    let status = JSON.parse(statusNode.textContent);

    function writeLog(message, data) {{
      const now = new Date().toLocaleTimeString();
      const detail = data ? "\\n" + JSON.stringify(data, null, 2) : "";
      log.textContent = `[${{now}}] ${{message}}${{detail}}\\n\\n` + log.textContent;
    }}

    function renderStatus(next) {{
      status = next;
      const counts = status.counts || {{}};
      document.querySelector("#incomingCount").textContent = counts.incoming_json || 0;

      const badge = document.querySelector("#configBadge");
      badge.className = status.ok ? "badge ok" : "badge error";
      badge.textContent = status.ok ? "config loaded" : "config needed";

      const output = status.output || {{}};
      const webhook = status.webhook || {{}};
      const rows = [
        ["config", status.config_path || ""],
        ["notify", output.notify_mode || ""],
        ["daily", `${{output.daily_mode || ""}} · ${{output.daily_path || ""}}`],
        ["weekly", `${{output.weekly_mode || ""}} · ${{output.weekly_path || ""}}`],
        ["memo", output.memo_mode || ""],
        ["webhook", webhook.port ? `${{webhook.host}}:${{webhook.port}}` : ""],
        ["dump", webhook.dump_dir || ""],
        ["daily html", counts.daily_html || 0],
        ["weekly html", counts.weekly_html || 0],
        ["draft indexes", counts.weekly_indexes || 0],
        ["notify history", counts.notification_history || 0],
      ];
      if (status.error) rows.push(["error", status.error]);
      document.querySelector("#settings").innerHTML = rows
        .map(([k, v]) => `<dt>${{escapeHtml(k)}}</dt><dd>${{escapeHtml(v)}}</dd>`)
        .join("");
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}

    function statusLabel(status) {{
      const labels = {{
        pending: "대기",
        dry_run: "문구 생성",
        sent: "발송 완료",
        failed: "실패",
      }};
      return labels[status] || status || "-";
    }}

    function statusPill(status) {{
      const safe = escapeHtml(status || "pending");
      return `<span class="status-pill ${{safe}}">${{escapeHtml(statusLabel(status))}}</span>`;
    }}

    async function callApi(path, payload) {{
      const response = await fetch(path, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload || {{}}),
      }});
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || data.message || "request failed");
      }}
      return data;
    }}

    async function refreshStatus() {{
      const response = await fetch("/api/status");
      renderStatus(await response.json());
    }}

    async function loadSituation() {{
      if (!status.ok) {{
        renderMissing({{ summary: {{}}, items: [] }});
        renderNotifications({{ items: [] }});
        return;
      }}
      const params = new URLSearchParams();
      params.set("window_hours", document.querySelector("#windowHours").value || "24");
      const lessonId = document.querySelector("#lessonId").value.trim();
      if (lessonId) params.set("lesson_id", lessonId);

      const missingResponse = await fetch(`/api/missing-homework?${{params.toString()}}`);
      const missingData = await missingResponse.json();
      if (!missingResponse.ok || missingData.ok === false) {{
        throw new Error(missingData.detail || "missing homework load failed");
      }}
      renderMissing(missingData);

      const notifyResponse = await fetch("/api/notifications?limit=80");
      const notifyData = await notifyResponse.json();
      if (!notifyResponse.ok || notifyData.ok === false) {{
        throw new Error(notifyData.detail || "notification load failed");
      }}
      renderNotifications(notifyData);
    }}

    function renderMissing(data) {{
      const summary = data.summary || {{}};
      document.querySelector("#missingCount").textContent = summary.total_missing || 0;
      document.querySelector("#noPhoneCount").textContent = summary.no_parent_phone || 0;
      document.querySelector("#dryRunCount").textContent = summary.dry_run || 0;
      document.querySelector("#sentCount").textContent = summary.sent || 0;
      document.querySelector("#failedCount").textContent = summary.failed || 0;

      const items = data.items || [];
      const target = document.querySelector("#missingTable");
      if (!items.length) {{
        target.innerHTML = `<div class="empty">조회 범위 안의 숙제 미제출 학생이 없습니다.</div>`;
        return;
      }}
      target.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>학생</th>
                <th>반</th>
                <th>수업</th>
                <th>출석</th>
                <th>학부모 연락처</th>
                <th>알림 상태</th>
              </tr>
            </thead>
            <tbody>
              ${{items.map((item) => `
                <tr>
                  <td>${{escapeHtml(item.student_name)}}<br><span class="badge">${{escapeHtml(item.student_classin_id)}}</span></td>
                  <td>${{escapeHtml(item.class_name || "-")}}</td>
                  <td>${{escapeHtml(item.lesson_classin_id || "-")}}<br><span class="badge">${{escapeHtml(formatDate(item.date))}}</span></td>
                  <td>${{escapeHtml(item.attendance || "-")}}</td>
                  <td>${{item.has_parent_phone ? escapeHtml(item.parent_phone) : '<span class="status-pill failed">없음</span>'}}</td>
                  <td>${{statusPill(item.notification_status)}}<br><span class="badge">${{escapeHtml(formatDate(item.notification_at))}}</span></td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>`;
    }}

    function renderNotifications(data) {{
      const items = data.items || [];
      const target = document.querySelector("#notificationTable");
      if (!items.length) {{
        target.innerHTML = `<div class="empty">아직 알림 생성/발송 기록이 없습니다.</div>`;
        return;
      }}
      target.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>시간</th>
                <th>학생</th>
                <th>수신자</th>
                <th>상태</th>
                <th>문구</th>
              </tr>
            </thead>
            <tbody>
              ${{items.map((item) => `
                <tr>
                  <td>${{escapeHtml(formatDate(item.created_at))}}</td>
                  <td>${{escapeHtml(item.student_name || "미등록")}}<br><span class="badge">${{escapeHtml(item.student_classin_id || "")}}</span></td>
                  <td>${{escapeHtml(item.parent_phone || "-")}}</td>
                  <td>${{statusPill(item.status)}}<br><span class="badge">${{escapeHtml(item.provider || "-")}}</span></td>
                  <td>${{escapeHtml(shorten(item.message || "", 90))}}</td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>`;
    }}

    function formatDate(value) {{
      if (!value) return "-";
      return String(value).replace("T", " ").replace("+00:00", "").replace("Z", "");
    }}

    function shorten(value, max) {{
      const text = String(value);
      return text.length > max ? text.slice(0, max - 1) + "..." : text;
    }}

    const actions = {{
      async renderDaily() {{
        const data = await callApi("/api/render-daily", {{
          date: document.querySelector("#dailyDate").value,
        }});
        writeLog(data.message, data);
        await refreshStatus();
      }},
      async generateWeekly() {{
        const data = await callApi("/api/generate-weekly-drafts");
        writeLog(data.message, data);
        await refreshStatus();
      }},
      async approveWeekly() {{
        const data = await callApi("/api/approve-weekly", {{
          week: document.querySelector("#weekDate").value,
        }});
        writeLog(data.message, data);
        await refreshStatus();
      }},
      async sweepMissing() {{
        const data = await callApi("/api/sweep-missing-homework", {{
          window_hours: document.querySelector("#windowHours").value,
          lesson_id: document.querySelector("#lessonId").value,
        }});
        writeLog(data.message, data);
        await loadSituation();
      }},
      async refreshMissing() {{
        await loadSituation();
        writeLog("미제출/알림 상황을 갱신했습니다.");
      }},
      async writeMemo() {{
        const data = await callApi("/api/write-memo", {{
          classin_id: document.querySelector("#memoClassinId").value,
          tag: document.querySelector("#memoTag").value,
          text: document.querySelector("#memoText").value,
        }});
        writeLog(data.message, data);
      }},
      async askAgent() {{
        const data = await callApi("/api/agent", {{
          question: document.querySelector("#agentQuestion").value,
        }});
        writeLog(data.answer || data.message, {{ message: data.message }});
      }},
      async refreshStatus() {{
        await refreshStatus();
        await loadSituation();
        writeLog("상태를 갱신했습니다.");
      }},
    }};

    document.addEventListener("click", async (event) => {{
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const action = actions[button.dataset.action];
      if (!action) return;
      button.disabled = true;
      try {{
        await action();
      }} catch (error) {{
        writeLog(error.message);
      }} finally {{
        button.disabled = false;
      }}
    }});

    renderStatus(status);
    loadSituation().catch((error) => writeLog(error.message));
  </script>
</body>
</html>"""


def _week_start(value: date_cls) -> date_cls:
    return date_cls.fromordinal(value.toordinal() - value.weekday())


def _json_for_script(value: dict[str, Any]) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
