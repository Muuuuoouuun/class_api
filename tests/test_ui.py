import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from classin_toolkit.config import AppConfig
from classin_toolkit.notify.message import OutgoingMessage
from classin_toolkit.pipelines import weekly
from classin_toolkit.storage.notion_repo import StudentRecord
from classin_toolkit.ui import create_app


def _cfg(tmp_path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "academy": {"name": "테스트학원", "timezone": "Asia/Seoul"},
            "classin": {
                "school_id": "sid",
                "secret_key": "secret",
                "webhook_secret": "webhook",
            },
            "notion": {
                "token": "secret_test",
                "databases": {
                    "students": "students",
                    "lessons": "lessons",
                    "reports": "reports",
                    "memos": "memos",
                    "exams": "exams",
                },
            },
            "anthropic": {"api_key": "sk-ant-test"},
            "notify": {
                "mode": "dry_run",
                "provider": "aligo",
                "aligo": {
                    "api_key": "aligo-key",
                    "user_id": "aligo-user",
                    "sender": "01012345678",
                },
            },
            "output": {
                "daily": {"path": str(tmp_path / "daily")},
                "weekly": {"path": str(tmp_path / "weekly")},
            },
            "webhook": {"dump_dir": str(tmp_path / "incoming")},
            "reports": {"output_dir": str(tmp_path)},
        }
    )


def test_ui_home_renders_with_config(tmp_path):
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/")

    assert res.status_code == 200
    assert "ClassIn 운영 콘솔" in res.text
    assert "Academy Ops Hub" in res.text
    assert "오늘의 운영 브리핑" in res.text
    assert "오늘 처리할 학생" in res.text
    assert "허브 새로고침" in res.text
    assert "오늘의 운영 리포트" in res.text
    assert "generateOpsReport" in res.text
    assert "/api/ops-report" in res.text
    assert "saveOpsHandoff" in res.text
    assert "refreshOpsHandoffs" in res.text
    assert "/api/ops-handoff" in res.text
    assert "/api/ops-handoffs" in res.text
    assert "opsHandoffList" in res.text
    assert "renderOpsHandoffs" in res.text
    assert "오늘의 자동화 실행계획" in res.text
    assert "generateOpsPlaybook" in res.text
    assert "/api/ops-playbook" in res.text
    assert 'data-toggle-preview="#opsPlaybookPreview"' in res.text
    assert 'data-toggle-preview="#opsReportPreview"' in res.text
    assert "setPreviewCollapsed" in res.text
    assert "pagerHtml" in res.text
    assert "data-page-kind" in res.text
    assert "개별 리포트 초안" in res.text
    assert "generateStudentReportPack" in res.text
    assert "/api/student-report-pack" in res.text
    assert 'data-toggle-preview="#studentReportPreview"' in res.text
    assert "renderReportCompositionTable" in res.text
    assert "renderWeeklyDraftTable" in res.text
    assert "테스트학원" in res.text
    assert "API 연결 점검" in res.text
    assert "운영 전환 체크리스트" in res.text
    assert "setReadinessMode" in res.text
    assert "renderReadinessList" in res.text
    assert "/api/readiness" in res.text
    assert "Notion DB 설계 미리보기" in res.text
    assert "loadNotionSchema" in res.text
    assert "copyNotionSchema" in res.text
    assert "/api/notion-schema" in res.text
    assert "notionSchemaCommand" in res.text
    assert "schema-details" in res.text
    assert "속성 목록" in res.text
    assert "파일럿 브링업" in res.text
    assert "loadPilotBrief" in res.text
    assert "copyPilotBrief" in res.text
    assert "/api/pilot-brief" in res.text
    assert "pilotBriefPreview" in res.text
    assert 'data-readiness-mode="local-demo"' in res.text
    assert 'data-readiness-mode="classin-live"' in res.text
    assert 'data-readiness-mode="kakao-live"' in res.text
    assert "data-tab=\"schedule\"" in res.text
    assert "data-tab=\"actions\"" in res.text
    assert "data-tab=\"data\"" in res.text
    assert "핵심 기능" in res.text
    assert "ClassIn Data Subscription" in res.text
    assert "refreshWebhookInbox" in res.text
    assert "/api/webhook-inbox" in res.text
    assert "학원 데이터 융합" in res.text
    assert "refreshAcademyContexts" in res.text
    assert "/api/academy-contexts" in res.text
    assert "선택 문자 발송" in res.text
    assert "스케줄표로 수업·숙제 생성" in res.text
    assert "반별 리포트 생성" in res.text
    assert "오늘 0시 이후" in res.text
    assert "반 선택" in res.text
    assert "전체 반" in res.text
    assert "반 목록 새로고침" in res.text
    assert "스케줄 표" in res.text
    assert "CSV 내보내기" in res.text
    assert "전체 주간 드래프트" in res.text
    assert "주간 드래프트 검토" in res.text
    assert "forceBlockedQuality" in res.text
    assert "OMR 답안지 생성" in res.text
    assert "answerSheetCourseId" in res.text
    assert "/api/create-answer-sheet" in res.text
    assert "ClassIn 접속 링크" in res.text
    assert "ssoUid" in res.text
    assert "/api/sso-link" in res.text
    assert "시험 결과 가져오기" in res.text
    assert "readExamFile" in res.text
    assert "examFile" in res.text
    assert "notifyModePill" in res.text
    assert 'aria-current="page"' in res.text
    assert "defaultMissingSelectionKeys" in res.text
    assert "item.has_parent_phone && isPendingMissing(item)" in res.text
    assert "문구 미리보기" in res.text
    assert "previewMissingHomeworkSms" in res.text
    assert "/api/preview-missing-homework" in res.text
    assert "message-preview-card" in res.text


def test_ui_demo_mode_runs_without_config_or_notion(monkeypatch, tmp_path):
    def fail_query(*args, **kwargs):
        raise AssertionError("live query should not be called in demo mode")

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", fail_query)
    monkeypatch.setattr("classin_toolkit.ui.load_notification_history", fail_query)
    monkeypatch.setattr(
        "classin_toolkit.ui.NotionRepo.from_config",
        staticmethod(lambda _cfg: fail_query()),
    )
    client = TestClient(create_app(config_path=tmp_path / "missing.yaml", demo=True))

    status = client.get("/api/status").json()
    readiness = client.get("/api/readiness?mode=kakao-live").json()
    notion_schema = client.get("/api/notion-schema?prefix=Demo").json()
    pilot_brief = client.get("/api/pilot-brief").json()
    diagnostics = client.get("/api/diagnostics").json()
    missing = client.get("/api/missing-homework").json()
    missing_preview = client.post("/api/preview-missing-homework", json={}).json()
    notifications = client.get("/api/notifications").json()
    webhook_inbox = client.get("/api/webhook-inbox").json()
    academy_contexts = client.get("/api/academy-contexts").json()
    hub = client.get("/api/ops-hub").json()
    ops_report = client.get("/api/ops-report").json()
    ops_handoffs = client.get("/api/ops-handoffs").json()
    ops_handoff = client.post("/api/ops-handoff", json={"window_hours": 24}).json()
    ops_playbook = client.get("/api/ops-playbook").json()
    schedule = client.get("/api/schedule?start=2026-06-15&days=7").json()
    targets = client.get("/api/report-targets").json()
    compositions = client.get("/api/report-compositions?week=2026-06-15").json()
    student_pack = client.get(
        "/api/student-report-pack?student_classin_id=10002&week=2026-06-15"
    ).json()
    weekly_drafts = client.get("/api/weekly-drafts?week=2026-06-15").json()
    class_reports = client.post(
        "/api/generate-class-reports",
        json={"student_classin_ids": ["10001"], "week": "2026-06-15"},
    ).json()
    sweep = client.post("/api/sweep-missing-homework", json={}).json()
    sso = client.post(
        "/api/sso-link",
        json={
            "uid": "10001",
            "course_id": "20001",
            "class_id": "30001",
            "telephone": "01012345678",
            "device_type": 1,
        },
    ).json()

    assert status["ok"] is True
    assert status["mode"] == "demo"
    assert status["academy"] == "ClassIn Demo Academy"
    assert readiness["demo"] is True
    assert readiness["mode"] == "kakao-live"
    assert readiness["ready"] is False
    assert readiness["summary"]["blockers"] >= 1
    assert any(item["label"] == "숙제 미제출 템플릿 코드" for item in readiness["items"])
    assert notion_schema["summary"]["databases"] == 5
    assert [item["kind"] for item in notion_schema["items"]] == [
        "학생 Master",
        "수업 기록",
        "리포트",
        "메모",
        "시험",
    ]
    assert "setup-notion" in notion_schema["commands"]["dry_run"]
    assert "<EXAMS_DB_ID>" in notion_schema["config_snippet"]
    assert pilot_brief["demo"] is True
    assert pilot_brief["academy"] == "ClassIn Demo Academy"
    assert pilot_brief["endpoint"]["local_url"].endswith(":8787/classin/webhook")
    assert "ClassIn DataSub 신청 메일" in pilot_brief["markdown"]
    assert "secret_key" in pilot_brief["markdown"]
    assert "sk-ant" not in pilot_brief["markdown"]
    assert diagnostics["ready"] is True
    assert missing["ok"] is True
    assert missing["summary"]["total_missing"] > 0
    assert missing["summary"]["needs_phone"] > 0
    assert missing["data_context"]["summary"]["students_with_context"] > 0
    assert any(item["report_context"]["has_context"] for item in missing["items"])
    assert missing_preview["demo"] is True
    assert missing_preview["summary"]["total"] > 0
    assert missing_preview["items"][0]["parent_phone_masked"]
    assert "01012345678" not in json.dumps(missing_preview, ensure_ascii=False)
    assert notifications["summary"]["total"] > 0
    assert webhook_inbox["demo"] is True
    assert webhook_inbox["summary"]["total"] >= 4
    assert {item["cmd"] for item in webhook_inbox["items"]} >= {
        "Attendance",
        "HomeworkSubmit",
        "AnswerSheetScore",
    }
    assert academy_contexts["demo"] is True
    assert academy_contexts["summary"]["students_with_context"] > 0
    assert academy_contexts["summary"]["needs_review"] == 1
    assert any(item["has_context"] for item in academy_contexts["items"])
    assert hub["ok"] is True
    assert [lane["title"] for lane in hub["lanes"]] == [
        "ClassIn API Push",
        "ClassIn Data Sub",
        "학원 데이터 융합",
        "개별 리포트",
    ]
    assert hub["summary"]["total_missing"] > 0
    assert hub["summary"]["report_blocked"] == 1
    assert hub["ops_brief"][0]["id"] in {"needs_retry", "needs_phone", "report_blocked"}
    assert any(item["id"] == "report_blocked" for item in hub["ops_brief"])
    assert hub["work_queue"]
    assert {
        "execution_state",
        "safety_gate",
        "completion_check",
        "operator_note",
    } <= set(hub["work_queue"][0])
    assert ops_report["ok"] is True
    assert ops_report["summary"]["brief_items"] > 0
    assert "# ClassIn Demo Academy 운영 리포트" in ops_report["markdown"]
    assert "## 3. ClassIn 데이터 상태" in ops_report["markdown"]
    assert "## 4. 학원 데이터 융합" in ops_report["markdown"]
    assert "## 5. 개별 리포트 품질" in ops_report["markdown"]
    assert "게이트:" in ops_report["markdown"]
    assert "완료 기준:" in ops_report["markdown"]
    assert "010-" not in ops_report["markdown"]
    assert "classin://" not in ops_report["markdown"]
    assert ops_handoffs["demo"] is True
    assert ops_handoffs["items"][0]["filename"].endswith("_ops.md")
    assert ops_handoffs["items"][0]["preview_url"] == ""
    assert ops_handoff["demo"] is True
    assert ops_handoff["item"]["path"].startswith("demo://ops/")
    assert ops_handoff["item"]["preview_url"] == ""
    assert "# ClassIn Demo Academy 운영 리포트" in ops_handoff["markdown"]
    assert ops_playbook["ok"] is True
    assert ops_playbook["summary"]["total_steps"] >= 4
    assert ops_playbook["summary"]["dry_run"] >= 1
    assert any(step["id"] == "missing_homework" for step in ops_playbook["steps"])
    assert "# ClassIn Demo Academy 자동화 실행계획" in ops_playbook["markdown"]
    assert "## 실행 순서" in ops_playbook["markdown"]
    assert "## 안전 게이트" in ops_playbook["markdown"]
    assert "010-" not in ops_playbook["markdown"]
    assert "classin://" not in ops_playbook["markdown"]
    assert schedule["summary"]["total_lessons"] > 0
    assert targets["summary"]["total"] == 5
    assert compositions["summary"]["total"] == 5
    assert compositions["summary"]["with_classin_lessons"] > 0
    assert compositions["items"][0]["sections"]
    assert student_pack["ok"] is True
    assert student_pack["student_classin_id"] == "10002"
    assert "## 2. ClassIn 근거" in student_pack["markdown"]
    assert "## 5. 학부모 전달 문안 초안" in student_pack["markdown"]
    assert "## 6. 교사 확인 체크리스트" in student_pack["markdown"]
    assert "010" not in student_pack["markdown"]
    assert "classin://" not in student_pack["markdown"]
    assert weekly_drafts["summary"]["blocked_unapproved"] == 1
    assert weekly_drafts["items"][0]["quality_status"] == "blocked"
    assert class_reports["demo"] is True
    assert class_reports["count"] == 1
    assert sweep["demo"] is True
    assert sso["demo"] is True
    assert sso["masked_link"] == "https://demo.classin.local/..."


def test_ui_shell_wires_buttons_tabs_and_api_routes(tmp_path):
    app = create_app(config=_cfg(tmp_path))
    client = TestClient(app)

    res = client.get("/")

    assert res.status_code == 200
    shell = res.text
    actions = set(re.findall(r'data-action="([^"]+)"', shell))
    action_block = shell.split("const actions = {", 1)[1].split("};", 1)[0]
    handlers = set(re.findall(r"\n\s*async\s+([A-Za-z0-9_]+)\s*\(", action_block))
    tabs = set(re.findall(r'<button[^>]+data-tab="([^"]+)"', shell))
    panels = {
        panel.replace("tab-", "")
        for panel in re.findall(r'id="(tab-[^"]+)"', shell)
    }
    frontend_paths = {
        path.split("?", 1)[0].split("${", 1)[0]
        for path in re.findall(r'(?:fetch|callApi)\((?:`|")([^`"]+)', shell)
        if path.startswith("/api/")
    }
    backend_paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/")
    }

    assert actions - handlers == set()
    assert handlers - actions == set()
    assert tabs - panels == set()
    assert panels - tabs == set()
    assert frontend_paths - backend_paths == set()
    assert backend_paths - frontend_paths == set()


def test_ui_diagnostics_endpoint_returns_offline_probe_results(tmp_path):
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/diagnostics")

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["ready"] is True
    assert body["live"] is False
    assert body["summary"]["skipped"] > 0
    assert {
        "service": "ClassIn",
        "check": "SID / secret_key",
        "status": "ok",
        "detail": "입력됨",
        "next_step": "",
    } in body["items"]


def test_ui_readiness_endpoint_returns_mode_summary(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.classin.default_teacher_uid = "teacher-1"
    cfg.notify.mode = "live"
    cfg.notify.aligo.sender_key = "sender-key"
    cfg.notify.aligo.template_code_missing_homework = "TPL_HOMEWORK"
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/readiness?mode=kakao-live")

    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "kakao-live"
    assert body["summary"]["total"] == len(body["items"])
    assert any(item["label"] == "실제 알림톡 발송 구현" for item in body["items"])
    assert not any("secret" in item["detail"] for item in body["items"])


def test_ui_readiness_endpoint_rejects_unknown_mode(tmp_path):
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/readiness?mode=prod")

    assert res.status_code == 400
    assert "mode must be one of" in res.json()["detail"]


def test_ui_notion_schema_endpoint_returns_five_db_preview(tmp_path):
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/notion-schema?prefix=테스트학원")

    assert res.status_code == 200
    body = res.json()
    assert body["prefix"] == "테스트학원"
    assert body["summary"]["databases"] == 5
    assert body["items"][0]["title"] == "테스트학원 - 학생 Master"
    assert body["items"][0]["kind"] == "학생 Master"
    assert "학부모 연락처" in body["items"][0]["properties"]
    assert "숙제 제출" in body["items"][1]["properties"]
    assert "응시 여부" in body["items"][4]["properties"]
    assert "--dry-run" in body["commands"]["dry_run"]
    assert "--write" in body["commands"]["write"]
    assert 'students: "<STUDENTS_DB_ID>"' in body["config_snippet"]


def test_ui_notion_schema_endpoint_rejects_long_prefix(tmp_path):
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/notion-schema?prefix=" + "가" * 81)

    assert res.status_code == 400
    assert res.json()["detail"] == "prefix는 80자 이하여야 합니다."


def test_ui_pilot_brief_endpoint_returns_safe_datasub_package(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.classin.school_id = "sid-hidden-123"
    cfg.classin.secret_key = "classin-secret-should-hide"
    cfg.classin.webhook_secret = "webhook-secret-should-hide"
    cfg.classin.default_teacher_uid = "teacher-1"
    cfg.notify.aligo.sender = "01099998888"
    cfg.output.daily.public_url_base = "https://webhook.test.example/reports"
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/pilot-brief")

    assert res.status_code == 200
    body = res.json()
    rendered = json.dumps(body, ensure_ascii=False)
    assert body["summary"]["total"] == 8
    assert body["summary"]["missing"] == 0
    assert body["endpoint"]["datasub_url"] == "https://webhook.test.example/classin/webhook"
    assert body["endpoint"]["health_url"] == "https://webhook.test.example/health"
    assert "an.vu@classin.com" in body["datasub"]["recipients"]
    assert "School ID (SID): <config.yaml classin.school_id" in body["datasub"]["body"]
    assert "cloudflared tunnel create" in body["commands"]["named_tunnel_create"]
    assert "install-windows-tasks.ps1" in body["markdown"]
    assert "classin-secret-should-hide" not in rendered
    assert "webhook-secret-should-hide" not in rendered
    assert "01099998888" not in rendered
    assert "sid-hidden-123" not in rendered


def test_windows_task_scripts_are_packaged():
    root = Path(__file__).parents[1]
    install_script = root / "scripts" / "install-windows-tasks.ps1"
    webhook_script = root / "scripts" / "windows-start-webhook.ps1"
    tunnel_script = root / "scripts" / "windows-start-tunnel.ps1"

    assert install_script.exists()
    assert webhook_script.exists()
    assert tunnel_script.exists()
    install_text = install_script.read_text(encoding="utf-8")
    assert "Register-ScheduledTask" in install_text
    assert "$TaskPrefix Webhook Receiver" in install_text
    assert "$TaskPrefix Cloudflare Tunnel" in install_text
    assert "windows-start-webhook.ps1" in install_text
    assert "windows-start-tunnel.ps1" in install_text


def test_ui_missing_homework_returns_service_error(monkeypatch, tmp_path):
    def broken_query(*_args, **_kwargs):
        raise RuntimeError("Notion says no")

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", broken_query)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/missing-homework?window_hours=24")

    assert res.status_code == 502
    assert res.json()["detail"] == "미제출 조회 실패: Notion says no"


def test_ui_missing_homework_requires_notion_config(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.notion.token = "secret_REPLACE_ME"

    def query_should_not_run(*_args, **_kwargs):
        raise AssertionError("missing Notion config should block before query")

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", query_should_not_run)
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/missing-homework?window_hours=24")

    assert res.status_code == 400
    assert res.json()["detail"].startswith("Notion 설정이 필요합니다")


def test_ui_missing_homework_rejects_invalid_window(monkeypatch, tmp_path):
    def query_should_not_run(*_args, **_kwargs):
        raise AssertionError("invalid window should block before query")

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", query_should_not_run)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/missing-homework?window_hours=0")

    assert res.status_code == 400
    assert res.json()["detail"] == "window_hours는 1~720 사이여야 합니다."


def test_ui_notifications_rejects_excessive_limit(monkeypatch, tmp_path):
    def history_should_not_run(*_args, **_kwargs):
        raise AssertionError("invalid limit should block before history load")

    monkeypatch.setattr("classin_toolkit.ui.load_notification_history", history_should_not_run)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/notifications?limit=999")

    assert res.status_code == 400
    assert res.json()["detail"] == "limit은 1~500 사이여야 합니다."


def test_ui_webhook_inbox_summarizes_dump_files(tmp_path):
    cfg = _cfg(tmp_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "20260618T100001.json").write_text(
        json.dumps(
            {
                "Cmd": "HomeworkSubmit",
                "CourseID": 132323,
                "ClassID": 2362301,
                "ActionTime": 1777610000,
                "Data": {
                    "ActivityName": "워크북 p.42-48",
                    "StudentInfo": {"Uid": 10001, "Name": "박성실"},
                    "StudentTotal": 5,
                },
                "SafeKey": "should-not-render",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/webhook-inbox?limit=5")

    assert res.status_code == 200
    body = res.json()
    assert body["summary"]["total"] == 1
    assert body["summary"]["by_cmd"] == {"HomeworkSubmit": 1}
    assert body["items"] == [
        {
            "file_name": "20260618T100001.json",
            "received_at": body["items"][0]["received_at"],
            "status": "parsed",
            "cmd": "HomeworkSubmit",
            "detail": "",
            "course_id": "132323",
            "class_id": "2362301",
            "class_name": "",
            "activity": "워크북 p.42-48",
            "student_count": 1,
            "students": ["박성실"],
            "action_at": "2026-05-01T04:33:20+00:00",
        }
    ]
    assert "SafeKey" not in str(body)


def test_ui_webhook_inbox_rejects_excessive_limit(tmp_path):
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/webhook-inbox?limit=999")

    assert res.status_code == 400
    assert res.json()["detail"] == "limit은 1~100 사이여야 합니다."


def test_ui_schedule_groups_lesson_records(monkeypatch, tmp_path):
    class FakeRepo:
        def lesson_records(self, *, since, until):
            assert since.date().isoformat() == "2026-04-20"
            assert until.date().isoformat() == "2026-04-27"
            return [
                {
                    "student_classin_id": "10001",
                    "student_name": "홍길동",
                    "student_class_name": "고2-A",
                    "lesson_classin_id": "lesson-1",
                    "course_classin_id": "course-1",
                    "date": "2026-04-20T10:00:00+00:00",
                    "attendance": "출석",
                    "homework_submitted": True,
                    "homework_late": False,
                    "homework_score": 95,
                },
                {
                    "student_classin_id": "10002",
                    "student_name": "김영희",
                    "student_class_name": "고2-A",
                    "lesson_classin_id": "lesson-1",
                    "course_classin_id": "course-1",
                    "date": "2026-04-20T10:00:00+00:00",
                    "attendance": "지각",
                    "homework_submitted": False,
                    "homework_late": None,
                    "homework_score": None,
                },
            ]

    monkeypatch.setattr(
        "classin_toolkit.ui.NotionRepo.from_config",
        staticmethod(lambda _cfg: FakeRepo()),
    )
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/schedule?start=2026-04-20&days=7")

    assert res.status_code == 200
    body = res.json()
    assert body["summary"] == {
        "total_lessons": 1,
        "total_student_rows": 2,
        "late": 1,
        "absent": 0,
        "homework_missing": 1,
    }
    assert body["items"][0]["student_count"] == 2
    assert body["items"][0]["attendance"]["출석"] == 1
    assert body["items"][0]["attendance"]["지각"] == 1
    assert body["items"][0]["homework_done"] == 1
    assert body["items"][0]["homework_missing"] == 1


def test_ui_schedule_requires_notion_config(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.notion.token = "secret_REPLACE_ME"

    def repo_should_not_run(_cfg):
        raise AssertionError("missing Notion config should block before repo access")

    monkeypatch.setattr(
        "classin_toolkit.ui.NotionRepo.from_config",
        staticmethod(repo_should_not_run),
    )
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/schedule?start=2026-04-20&days=7")

    assert res.status_code == 400
    assert res.json()["detail"].startswith("Notion 설정이 필요합니다")


def test_ui_parse_schedule_dry_run_returns_counts(monkeypatch, tmp_path):
    class Result:
        courses_created = 1
        lessons_created = 3
        homework_created = 2
        errors = ["course 고2: teacher UID missing"]

    captured = {}

    def fake_run_core_engine(cfg, *, schedule_text, dry_run):
        captured["academy"] = cfg.academy.name
        captured["schedule_text"] = schedule_text
        captured["dry_run"] = dry_run
        return Result()

    monkeypatch.setattr("classin_toolkit.ui.run_core_engine", fake_run_core_engine)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/parse-schedule-dry-run",
        json={"schedule_text": "course_name,teacher,date\n고2,김선생,2026-05-06"},
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "schedule_text": "course_name,teacher,date\n고2,김선생,2026-05-06",
        "dry_run": True,
    }
    body = res.json()
    assert body["message"] == "스케줄 dry-run을 완료했습니다."
    assert body["summary"] == {"courses": 1, "lessons": 3, "homework": 2, "errors": 1}
    assert body["errors"] == ["course 고2: teacher UID missing"]


def test_ui_create_schedule_can_run_live_after_review(monkeypatch, tmp_path):
    class Result:
        courses_created = 1
        lessons_created = 2
        homework_created = 1
        errors = []

    captured = {}

    def fake_run_core_engine(cfg, *, schedule_text, dry_run):
        captured["academy"] = cfg.academy.name
        captured["schedule_text"] = schedule_text
        captured["dry_run"] = dry_run
        return Result()

    monkeypatch.setattr("classin_toolkit.ui.run_core_engine", fake_run_core_engine)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/create-schedule",
        json={
            "schedule_text": "course_name,teacher,date\n고2,김선생,2026-05-06",
            "dry_run": False,
        },
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "schedule_text": "course_name,teacher,date\n고2,김선생,2026-05-06",
        "dry_run": False,
    }
    body = res.json()
    assert body["message"] == "수업과 숙제를 생성했습니다."
    assert body["summary"] == {"courses": 1, "lessons": 2, "homework": 1, "errors": 0}
    assert body["dry_run"] is False


def test_ui_sso_link_uses_classin_helper(monkeypatch, tmp_path):
    captured = {}

    def fake_get_login_linked(**kwargs):
        captured.update(kwargs)
        return "classin://open?token=secret"

    monkeypatch.setattr("classin_toolkit.ui.get_login_linked", fake_get_login_linked)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/sso-link",
        json={
            "uid": "10001",
            "course_id": "20001",
            "class_id": "30001",
            "telephone": "01012345678",
            "device_type": 2,
            "life_time": 3600,
        },
    )

    assert res.status_code == 200
    assert captured == {
        "base_url": "https://api.eeo.cn",
        "sid": "sid",
        "secret_key": "secret",
        "uid": "10001",
        "course_id": "20001",
        "class_id": "30001",
        "telephone": "01012345678",
        "device_type": 2,
        "life_time": 3600,
    }
    body = res.json()
    assert body["message"] == "ClassIn 접속 링크를 생성했습니다."
    assert body["link"] == "classin://open?token=secret"
    assert body["masked_link"] == "classin://..."
    assert body["device_label"] == "iOS"


def test_ui_sso_link_validates_required_fields(monkeypatch, tmp_path):
    def helper_should_not_run(**_kwargs):
        raise AssertionError("invalid SSO payload should not call ClassIn")

    monkeypatch.setattr("classin_toolkit.ui.get_login_linked", helper_should_not_run)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/sso-link",
        json={
            "uid": "10001",
            "course_id": "20001",
            "class_id": "30001",
            "telephone": "01012345678",
            "device_type": 9,
        },
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "device_type은 1, 2, 3 중 하나여야 합니다."


def test_ui_sweep_missing_homework_accepts_selection_keys(monkeypatch, tmp_path):
    captured = {}

    def fake_sweep_missing_homework(cfg, *, window_hours, lesson_id, selection_keys=None):
        captured["academy"] = cfg.academy.name
        captured["window_hours"] = window_hours
        captured["lesson_id"] = lesson_id
        captured["selection_keys"] = selection_keys
        return 1

    monkeypatch.setattr("classin_toolkit.ui.sweep_missing_homework", fake_sweep_missing_homework)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/sweep-missing-homework",
        json={
            "window_hours": 4,
            "lesson_id": "lesson-1",
            "selection_keys": ["10001::lesson-1::2026-04-24T10:00:00+00:00"],
        },
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "window_hours": 4,
        "lesson_id": "lesson-1",
        "selection_keys": ["10001::lesson-1::2026-04-24T10:00:00+00:00"],
    }
    assert res.json()["count"] == 1


def test_ui_sweep_missing_homework_rejects_invalid_window(monkeypatch, tmp_path):
    def sweep_should_not_run(*_args, **_kwargs):
        raise AssertionError("invalid window should block before sweep")

    monkeypatch.setattr("classin_toolkit.ui.sweep_missing_homework", sweep_should_not_run)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/sweep-missing-homework",
        json={
            "window_hours": 0,
            "selection_keys": ["10001::lesson-1::2026-04-24T10:00:00+00:00"],
        },
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "window_hours는 1~720 사이여야 합니다."


def test_ui_preview_missing_homework_returns_quality_preview(monkeypatch, tmp_path):
    captured = {}

    def fake_preview_missing_homework_messages(
        cfg,
        *,
        window_hours,
        lesson_id,
        selection_keys=None,
    ):
        captured["academy"] = cfg.academy.name
        captured["window_hours"] = window_hours
        captured["lesson_id"] = lesson_id
        captured["selection_keys"] = selection_keys
        return [
            OutgoingMessage(
                student_classin_id="10001",
                student_name="홍길동",
                parent_phone="01012345678",
                message="홍길동 학생의 숙제 제출 확인이 필요합니다.",
                quality_status="ready",
                quality_score=95,
            ),
            OutgoingMessage(
                student_classin_id="10002",
                student_name="김영희",
                parent_phone="",
                message="",
                quality_status="blocked",
                quality_score=20,
                quality_warnings=["보호자 연락처가 없습니다."],
            ),
        ]

    monkeypatch.setattr(
        "classin_toolkit.ui.preview_missing_homework_messages",
        fake_preview_missing_homework_messages,
    )
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/preview-missing-homework",
        json={
            "window_hours": 4,
            "lesson_id": "lesson-1",
            "selection_keys": ["10001::lesson-1::2026-04-24T10:00:00+00:00"],
        },
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "window_hours": 4,
        "lesson_id": "lesson-1",
        "selection_keys": ["10001::lesson-1::2026-04-24T10:00:00+00:00"],
    }
    body = res.json()
    assert body["summary"] == {
        "total": 2,
        "dispatchable": 1,
        "ready": 1,
        "review": 0,
        "blocked": 1,
        "no_parent_phone": 1,
        "live_blocked": 0,
    }
    assert body["items"][0]["parent_phone_masked"] == "010****5678"
    assert body["items"][1]["block_reason"] == "보호자 연락처가 없습니다."
    assert "01012345678" not in json.dumps(body, ensure_ascii=False)


def test_ui_preview_missing_homework_rejects_invalid_window(monkeypatch, tmp_path):
    def preview_should_not_run(*_args, **_kwargs):
        raise AssertionError("invalid window should block before preview")

    monkeypatch.setattr(
        "classin_toolkit.ui.preview_missing_homework_messages",
        preview_should_not_run,
    )
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post("/api/preview-missing-homework", json={"window_hours": 0})

    assert res.status_code == 400
    assert res.json()["detail"] == "window_hours는 1~720 사이여야 합니다."


def test_ui_generate_class_reports_wraps_weekly_drafts(monkeypatch, tmp_path):
    captured = {}

    def fake_generate_drafts(
        cfg,
        *,
        reference=None,
        class_name=None,
        student_classin_ids=None,
    ):
        captured["academy"] = cfg.academy.name
        captured["reference"] = reference.date().isoformat()
        captured["class_name"] = class_name
        captured["student_classin_ids"] = student_classin_ids
        return 7

    monkeypatch.setattr("classin_toolkit.ui.generate_drafts", fake_generate_drafts)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/generate-class-reports",
        json={
            "class_name": "고2-A",
            "week": "2026-04-20",
            "student_classin_ids": ["10001", "10002"],
        },
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "reference": "2026-04-20",
        "class_name": "고2-A",
        "student_classin_ids": ["10001", "10002"],
    }
    body = res.json()
    assert body["message"] == "고2-A 리포트 드래프트 7건을 생성했습니다."
    assert body["count"] == 7
    assert body["selected"] == 2
    assert body["includes"] == ["출결", "숙제", "시험 점수"]


def test_ui_report_targets_filters_by_class(monkeypatch, tmp_path):
    class FakeRepo:
        def list_active_students(self):
            return [
                StudentRecord("page-1", "10001", "홍길동", "01012345678", "고2-A"),
                StudentRecord("page-2", "10002", "김영희", "", "고2-B"),
            ]

    monkeypatch.setattr(
        "classin_toolkit.ui.NotionRepo.from_config",
        staticmethod(lambda _cfg: FakeRepo()),
    )
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/report-targets?class_name=고2-A")

    assert res.status_code == 200
    body = res.json()
    assert body["summary"] == {"total": 1, "classes": 1, "with_parent_phone": 1}
    assert body["items"] == [
        {
            "student_classin_id": "10001",
            "student_name": "홍길동",
            "class_name": "고2-A",
            "has_parent_phone": True,
        }
    ]


def test_ui_academy_contexts_merges_local_sources(monkeypatch, tmp_path):
    raw_source = tmp_path / "local_data" / "inbox" / "memos" / "hong.md"

    class FakeRepo:
        def list_active_students(self):
            return [
                StudentRecord("page-1", "10001", "홍길동", "01012345678", "고2-A"),
                StudentRecord("page-2", "10002", "김영희", "", "고2-B"),
            ]

    def fake_contexts(cfg, students):
        assert cfg.academy.name == "테스트학원"
        assert students == [
            {
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "student_class_name": "고2-A",
            }
        ]
        return SimpleNamespace(
            contexts={
                "10001": {
                    "has_context": True,
                    "weekly_report": {
                        "status": "draft_ready",
                        "period_start": "2026-04-20T00:00:00+00:00",
                        "period_end": "2026-04-26T23:59:00+00:00",
                        "approved": False,
                    },
                    "offline_attendance": 1,
                    "offline_scores": 2,
                    "memos": 1,
                    "badges": ["리포트 초안", "오프라인 시험 2건", "상담 메모 1건"],
                    "summary": "리포트 초안 · 오프라인 시험 2건 · 상담 메모 1건",
                    "sources": [
                        {
                            "kind": "memo",
                            "source": str(raw_source),
                            "date": "2026-04-24",
                            "detail": "학부모 상담",
                            "student": "홍길동",
                        }
                    ],
                }
            },
            summary={
                "students_with_context": 1,
                "weekly_reports": 1,
                "offline_attendance": 1,
                "offline_scores": 2,
                "memos": 1,
                "needs_review": 1,
            },
            needs_review_items=[
                {
                    "kind": "offline_score",
                    "student_name": "동명이인",
                    "class_name": "고2-A",
                    "date": "2026-04-24",
                    "detail": "월말평가 71점",
                    "source": str(tmp_path / "scores" / "april.xlsx"),
                    "reason": "학생 자동 매칭 필요",
                }
            ],
        )

    monkeypatch.setattr(
        "classin_toolkit.ui.NotionRepo.from_config",
        staticmethod(lambda _cfg: FakeRepo()),
    )
    monkeypatch.setattr("classin_toolkit.ui.build_report_contexts", fake_contexts)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/academy-contexts?class_name=고2-A")

    assert res.status_code == 200
    body = res.json()
    assert body["summary"] == {
        "total_students": 1,
        "students_with_context": 1,
        "students_without_context": 0,
        "weekly_reports": 1,
        "offline_attendance": 1,
        "offline_scores": 2,
        "memos": 1,
        "needs_review": 1,
    }
    assert body["items"][0]["student_name"] == "홍길동"
    assert body["items"][0]["sources"][0]["source_name"] == "hong.md"
    assert body["needs_review_items"][0]["source_name"] == "april.xlsx"
    assert str(tmp_path) not in str(body)


def test_ui_report_compositions_merges_lessons_exams_context_and_notifications(
    monkeypatch,
    tmp_path,
):
    class FakeRepo:
        def list_active_students(self):
            return [
                StudentRecord("page-1", "10001", "홍길동", "01012345678", "고2-A"),
                StudentRecord("page-2", "10002", "김영희", "", "고2-B"),
            ]

        def weekly_student_stats(self, *, student_page_id, since, until):
            assert since.date().isoformat() == "2026-04-20"
            assert until.date().isoformat() == "2026-04-26"
            if student_page_id == "page-1":
                return [
                    {
                        "attendance": "출석",
                        "homework_submitted": True,
                        "homework_score": 95,
                    },
                    {
                        "attendance": "지각",
                        "homework_submitted": False,
                        "homework_score": None,
                    },
                ]
            return []

        def student_exam_results(self, *, student_page_id, since, until):
            if student_page_id == "page-1":
                return [{"exam_name": "월말평가", "score": 82, "max_score": 100}]
            return []

    def fake_contexts(cfg, students):
        assert students == [
            {
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "student_class_name": "고2-A",
            }
        ]
        return SimpleNamespace(
            contexts={
                "10001": {
                    "has_context": True,
                    "summary": "상담 메모 1건",
                    "offline_attendance": 0,
                    "offline_scores": 1,
                    "memos": 1,
                    "weekly_report": {"status": "draft_ready"},
                    "sources": [{"kind": "memo", "detail": "학부모 요청"}],
                }
            },
            summary={"students_with_context": 1, "needs_review": 0},
            needs_review_items=[],
        )

    def fake_history(cfg, *, limit):
        return [
            {
                "student_classin_id": "10001",
                "status": "dry_run",
                "quality_status": "ready",
            }
        ]

    monkeypatch.setattr(
        "classin_toolkit.ui.NotionRepo.from_config",
        staticmethod(lambda _cfg: FakeRepo()),
    )
    monkeypatch.setattr("classin_toolkit.ui.build_report_contexts", fake_contexts)
    monkeypatch.setattr("classin_toolkit.ui.load_notification_history", fake_history)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/report-compositions?week=2026-04-20&class_name=고2-A")

    assert res.status_code == 200
    body = res.json()
    assert body["summary"]["total"] == 1
    assert body["summary"]["with_classin_lessons"] == 1
    assert body["summary"]["with_exam_signal"] == 1
    assert body["summary"]["with_memo_context"] == 1
    assert body["data_context"]["summary"] == {"students_with_context": 1, "needs_review": 0}
    item = body["items"][0]
    assert item["student_name"] == "홍길동"
    assert item["readiness_status"] == "ready"
    assert item["source_counts"]["notifications"] == 1
    assert {section["id"] for section in item["sections"]} >= {
        "attendance_routine",
        "homework_attitude",
        "exam_signal",
        "memo_context",
        "quality_gate",
    }

    pack = client.get(
        "/api/student-report-pack?student_classin_id=10001&week=2026-04-20&class_name=고2-A"
    )

    assert pack.status_code == 200
    pack_body = pack.json()
    assert pack_body["student_name"] == "홍길동"
    assert pack_body["readiness_status"] == "ready"
    assert pack_body["summary"]["sections"] >= 8
    assert "# 홍길동 개별 리포트 초안" in pack_body["markdown"]
    assert "## 3. 학원 데이터 맥락" in pack_body["markdown"]
    assert "## 5. 학부모 전달 문안 초안" in pack_body["markdown"]
    assert "010" not in pack_body["markdown"]
    assert "classin://" not in pack_body["markdown"]


def test_ui_weekly_drafts_endpoint_reads_quality_index(tmp_path):
    cfg = _cfg(tmp_path)
    period_start = datetime.fromisoformat("2026-04-20T00:00:00+00:00")
    index = tmp_path / "weekly" / "2026-04-20_drafts.json"
    index.parent.mkdir()
    weekly._write_index_records(
        index,
        [
            weekly.DraftRecord(
                student_classin_id="10001",
                student_name="홍길동",
                html_path=str(tmp_path / "weekly" / "hong.html"),
                public_url=None,
                period_start=period_start.isoformat(),
                period_end="2026-04-26T23:59:00+00:00",
                summary_markdown="요약",
                parent_message="문구",
                quality_status="blocked",
                quality_score=20,
                quality_warnings=["표현 안전 확인"],
            )
        ],
    )
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/weekly-drafts?week=2026-04-20")

    assert res.status_code == 200
    body = res.json()
    assert body["exists"] is True
    assert body["summary"]["blocked_unapproved"] == 1
    assert body["items"][0]["student_name"] == "홍길동"
    assert body["items"][0]["preview_url"] == "/reports/weekly/hong.html"


def test_ui_import_exam_results_accepts_csv_text(monkeypatch, tmp_path):
    class Result:
        total_rows = 2
        merged_rows = 1
        unresolved_rows = 1
        skipped_rows = 0
        errors = ["row 2: student not found or ambiguous: 김영희"]
        dry_run = True

    captured = {}

    def fake_import_exam_results(
        cfg,
        *,
        path,
        exam_name,
        exam_date,
        class_name,
        source,
        dry_run,
    ):
        captured["academy"] = cfg.academy.name
        captured["csv_text"] = path.read_text(encoding="utf-8")
        captured["exam_name"] = exam_name
        captured["exam_date"] = exam_date
        captured["class_name"] = class_name
        captured["source"] = source
        captured["dry_run"] = dry_run
        return Result()

    monkeypatch.setattr("classin_toolkit.ui.import_exam_results", fake_import_exam_results)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/import-exam-results",
        json={
            "csv_text": "student_name,score\n홍길동,92\n김영희,",
            "exam_name": "4월 월말평가",
            "exam_date": "2026-04-24",
            "class_name": "고2-A",
        },
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "csv_text": "student_name,score\n홍길동,92\n김영희,",
        "exam_name": "4월 월말평가",
        "exam_date": "2026-04-24",
        "class_name": "고2-A",
        "source": "ui-csv-import",
        "dry_run": True,
    }
    body = res.json()
    assert body["message"] == "시험 결과 dry-run을 완료했습니다."
    assert body["summary"] == {
        "total": 2,
        "merged": 1,
        "unresolved": 1,
        "skipped": 0,
        "errors": 1,
    }
    assert body["errors"] == ["row 2: student not found or ambiguous: 김영희"]


def test_ui_create_answer_sheet_dry_run(monkeypatch, tmp_path):
    class Result:
        activity_id = None
        name = "6월 OMR 답안지"
        released = False
        dry_run = True

    captured = {}

    def fake_create_answer_sheet_activity(
        cfg,
        *,
        course_id,
        unit_id,
        name,
        teacher_uid,
        start_at,
        end_at,
        release,
        dry_run,
    ):
        captured["academy"] = cfg.academy.name
        captured["course_id"] = course_id
        captured["unit_id"] = unit_id
        captured["name"] = name
        captured["teacher_uid"] = teacher_uid
        captured["start_at"] = start_at
        captured["end_at"] = end_at
        captured["release"] = release
        captured["dry_run"] = dry_run
        return Result()

    monkeypatch.setattr(
        "classin_toolkit.ui.create_answer_sheet_activity",
        fake_create_answer_sheet_activity,
    )
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/create-answer-sheet",
        json={
            "course_id": "414193",
            "unit_id": "22360790",
            "name": "6월 OMR 답안지",
            "teacher_uid": "1006368",
            "start_at": "2026-06-11T09:00:00+00:00",
            "end_at": "2026-06-12T09:00:00+00:00",
            "release": False,
            "dry_run": True,
        },
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "course_id": "414193",
        "unit_id": "22360790",
        "name": "6월 OMR 답안지",
        "teacher_uid": "1006368",
        "start_at": datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc),
        "end_at": datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc),
        "release": False,
        "dry_run": True,
    }
    body = res.json()
    assert body["message"] == "OMR 답안지 dry-run을 완료했습니다."
    assert body["activity_id"] is None
    assert body["name"] == "6월 OMR 답안지"


def test_ui_status_reports_local_counts(tmp_path):
    cfg = _cfg(tmp_path)
    daily = tmp_path / "daily"
    weekly = tmp_path / "weekly"
    incoming = tmp_path / "incoming"
    daily.mkdir()
    weekly.mkdir()
    incoming.mkdir()
    (daily / "2026-04-24.html").write_text("daily", encoding="utf-8")
    (weekly / "2026-04-20_홍길동.html").write_text("weekly", encoding="utf-8")
    (weekly / "2026-04-20_drafts.json").write_text("[]", encoding="utf-8")
    (incoming / "event.json").write_text("{}", encoding="utf-8")
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/status")

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["counts"] == {
        "incoming_json": 1,
        "daily_html": 1,
        "weekly_html": 1,
        "weekly_indexes": 1,
        "notification_history": 0,
    }


def test_ui_missing_homework_includes_notification_status(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)

    def fake_query_missing_homework(cfg, *, window_hours, lesson_id):
        return [
            {
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "student_class_name": "고2-A",
                "parent_phone": "01012345678",
                "lesson_classin_id": "lesson-1",
                "course_classin_id": "course-1",
                "date": "2026-04-24T10:00:00+00:00",
                "attendance": "출석",
                "homework_late": False,
                "homework_score": None,
            },
            {
                "student_classin_id": "10002",
                "student_name": "김영희",
                "student_class_name": "고2-A",
                "parent_phone": "",
                "lesson_classin_id": "lesson-1",
                "course_classin_id": "course-1",
                "date": "2026-04-24T10:00:00+00:00",
                "attendance": "지각",
                "homework_late": None,
                "homework_score": None,
            },
        ]

    def fake_history(cfg, *, limit):
        return [
            {
                "created_at": "2026-04-24T12:00:00+00:00",
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "parent_phone": "01012345678",
                "provider": "dry_run",
                "status": "dry_run",
                "message": "숙제 제출 안내",
                "quality_status": "ready",
                "quality_score": 95,
                "quality_warnings": [],
            }
        ]

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", fake_query_missing_homework)
    monkeypatch.setattr("classin_toolkit.ui.load_notification_history", fake_history)
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/missing-homework?window_hours=24")

    assert res.status_code == 200
    body = res.json()
    assert body["summary"] == {
        "total_missing": 2,
        "with_parent_phone": 1,
        "no_parent_phone": 1,
        "pending": 1,
        "dry_run": 1,
        "sent": 0,
        "failed": 0,
        "needs_phone": 1,
        "needs_message": 0,
        "needs_review": 1,
        "needs_retry": 0,
        "repeat_students": 0,
        "quality_ready": 1,
        "quality_review": 0,
        "quality_blocked": 0,
    }
    assert body["items"][0]["notification_status"] == "dry_run"
    assert body["items"][0]["notification_quality_status"] == "ready"
    assert body["items"][0]["notification_quality_score"] == 95
    assert body["items"][0]["selection_key"] == "10001::lesson-1::2026-04-24T10:00:00+00:00"
    assert body["items"][0]["report_context"]["has_context"] is False
    assert body["items"][1]["notification_status"] == "pending"


def test_ui_ops_hub_returns_four_lanes_and_student_queue(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    captured = {}

    def fake_query_missing_homework(cfg, *, window_hours, lesson_id):
        captured["window_hours"] = window_hours
        captured["lesson_id"] = lesson_id
        return [
            {
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "student_class_name": "고2-A",
                "parent_phone": "01012345678",
                "lesson_classin_id": "lesson-1",
                "course_classin_id": "course-1",
                "date": "2026-04-24T10:00:00+00:00",
                "attendance": "출석",
                "homework_late": False,
                "homework_score": None,
            }
        ]

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", fake_query_missing_homework)
    monkeypatch.setattr("classin_toolkit.ui.load_notification_history", lambda cfg, *, limit: [])
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/ops-hub?window_hours=24&lesson_id=lesson-1")

    assert res.status_code == 200
    body = res.json()
    assert captured == {"window_hours": 24, "lesson_id": "lesson-1"}
    assert body["config_ok"] is True
    assert [lane["id"] for lane in body["lanes"]] == [
        "api_push",
        "data_subscription",
        "academy_data",
        "individual_reports",
    ]
    assert body["summary"]["total_missing"] == 1
    assert body["ops_brief"]
    assert body["ops_brief"][0]["id"] == "needs_message"
    assert body["weekly_drafts"]["summary"]["total"] == 0
    assert body["focus"][0]["label"] == "연락 필요"
    assert body["work_queue"][0]["student_name"] == "홍길동"
    assert body["work_queue"][0]["next_action"] == "보호자에게 보내기"
    assert body["work_queue"][0]["execution_state"] == "ready"
    assert "발송 모드" in body["work_queue"][0]["safety_gate"]
    assert "알림 기록" in body["work_queue"][0]["completion_check"]


def test_ui_ops_handoff_saves_markdown_and_lists_recent(monkeypatch, tmp_path):
    class FakeRepo:
        def list_active_students(self):
            return []

    def fake_query_missing_homework(cfg, *, window_hours, lesson_id):
        assert window_hours == 24
        assert lesson_id is None
        return [
            {
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "student_class_name": "고2-A",
                "parent_phone": "01012345678",
                "lesson_classin_id": "lesson-1",
                "course_classin_id": "course-1",
                "date": "2026-04-24T10:00:00+00:00",
                "attendance": "출석",
                "homework_late": False,
                "homework_score": None,
            }
        ]

    def fake_contexts(cfg, rows):
        return SimpleNamespace(
            contexts={},
            summary={
                "students_with_context": 0,
                "offline_attendance": 0,
                "offline_scores": 0,
                "memos": 0,
                "needs_review": 0,
            },
            needs_review_items=[],
        )

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", fake_query_missing_homework)
    monkeypatch.setattr("classin_toolkit.ui.load_notification_history", lambda cfg, *, limit: [])
    monkeypatch.setattr("classin_toolkit.ui.build_report_contexts", fake_contexts)
    monkeypatch.setattr(
        "classin_toolkit.ui.NotionRepo.from_config",
        staticmethod(lambda _cfg: FakeRepo()),
    )
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post("/api/ops-handoff", json={"window_hours": 24})

    assert res.status_code == 200
    body = res.json()
    item = body["item"]
    path = Path(item["path"])
    assert body["message"] == "운영 리포트를 저장했습니다."
    assert item["filename"].endswith("_ops.md")
    assert item["preview_url"] == f"/reports/ops/{item['filename']}"
    assert path == tmp_path / "ops" / item["filename"]
    assert path.exists()
    assert "# 테스트학원 운영 리포트" in path.read_text(encoding="utf-8")
    assert "홍길동" in body["markdown"]
    assert (tmp_path / "ops" / "ops_handoffs.json").exists()

    recent = client.get("/api/ops-handoffs?limit=5")
    assert recent.status_code == 200
    recent_body = recent.json()
    assert recent_body["items"][0]["filename"] == item["filename"]

    preview = client.get(item["preview_url"])
    assert preview.status_code == 200
    assert "text/markdown" in preview.headers["content-type"]
    assert "# 테스트학원 운영 리포트" in preview.text
