# 13. 운영 매뉴얼 (Runbook)

학원 파일럿 설치부터 일상 운영·장애 대응까지. MOON이 컨설턴트 역할로 사용.

## 1. 학원 현장 설치 체크리스트

### 1.1 사전 준비 (MOON 측)
- [ ] `config.yaml` 학원별 복사본 생성 (`config.yaml.example` 기반)
- [ ] ClassIn SID / secret_key / webhook_secret 확인 (학원 측과 공유 전 확인)
- [ ] Notion 통합(Integration) 생성 → 토큰 발급
- [ ] Notion DB 5종 세팅 (12_notion_schema.md 기준) → 학생·수업·리포트·메모·시험 DB ID 확보
- [ ] Anthropic API 키 발급 (학원 고유 키 vs MOON 공용 키 정책 결정)
- [ ] 알리고 알림톡 계정·발신프로필·숙제 미제출 템플릿 코드 확보 (live 전환 시)
- [ ] Cloudflare 계정 + `cloudflared` 바이너리 확보

### 1.2 학원 PC 설치 단계
- [ ] Python 3.11+ 설치
- [ ] 프로젝트 체크아웃 (USB 설치 패키지 or git)
- [ ] `pip install -e .` 실행
- [ ] `config.yaml` 배치
- [ ] `cloudflared tunnel login` → 학원 계정으로 로그인
- [ ] `cloudflared tunnel create <academy-slug>` → 터널 ID 확보
- [ ] DNS CNAME 등록 (cloudflared가 안내)
- [ ] 터널 설정 파일 작성 (기본 `~/.cloudflared/config.yml` or `%USERPROFILE%\.cloudflared\config.yml`):
  ```yaml
  tunnel: <TUNNEL_ID>
  credentials-file: C:\Users\원장\.cloudflared\<TUNNEL_ID>.json
  ingress:
    - hostname: webhook.<academy>.example.com
      service: http://localhost:8787
    - service: http_status:404
  ```
- [ ] `classin-webhook` 실행 테스트 → `/health` 로컬 호출 확인
- [ ] `cloudflared tunnel run <name>` → 공개 URL에서 `/health` 확인
- [ ] ClassIn 지사에 Webhook URL 등록 요청 (1기관 1개)
- [ ] Postman 등으로 `Attendance` 샘플 페이로드 수신 테스트

### 1.3 Windows 자동 기동
- 권장: 관리자 PowerShell에서 아래 스크립트로 작업 스케줄러 2개를 등록:
  ```powershell
  .\scripts\install-windows-tasks.ps1 -TunnelName <academy-slug>
  ```
  등록되는 작업:
  1. **ClassIn Toolkit Webhook Receiver** — 로그인 시 `scripts/windows-start-webhook.ps1`
  2. **ClassIn Toolkit Cloudflare Tunnel** — 로그인 시 `scripts/windows-start-tunnel.ps1 -TunnelName <academy-slug>`
- **절전 모드 해제 필수**. 제어판 → 전원옵션에서 "디스플레이 끄기"만 허용, "컴퓨터 절전 상태로 전환" = "사용 안 함".
- 대안: 라즈베리파이 4 (5~10만원)에서 상시 구동 → 학원 PC 독립.

### 1.4 스케줄러 작업 (Windows 작업 스케줄러)
- `classin-toolkit sweep-missing-homework --window-hours 4` — 매 시 정각
- `classin-toolkit render-daily` — 매일 22:00 (일일 HTML 현황)
- `classin-toolkit generate-weekly-drafts` — 매주 금 17:00 (드래프트 생성)
- `classin-toolkit approve-weekly --week YYYY-MM-DD` — 원장 리뷰 후 수동 실행 (컨설턴트 가이드 따라)

### 1.5 Webhook 공개 노출 (로컬 → 인터넷)

ClassIn 은 Push 전용이라 수신기(:8787)를 공개 HTTPS 로 노출해야 등록·수신이 된다.

- **빠른 테스트** (계정/DNS 불필요, URL 매번 바뀜 → 1회 Cmd:Test 용):
  ```bash
  brew install cloudflared          # 최초 1회
  scripts/serve-webhook.sh          # 터미널 A: 로컬 수신기 :8787
  scripts/tunnel-quick.sh           # 터미널 B: https://<랜덤>.trycloudflare.com 발급
  ```
  외부 도달 확인: 발급 URL `/health` → `{"ok":true,...}`. 등록 엔드포인트: 발급 URL `/classin/webhook`.
- **운영 (고정 URL)**: `scripts/cloudflared-config.example.yml` 의 named tunnel 절차 사용 (§1.2 와 동일 흐름).
- **제약**: ClassIn 은 "1기관 1엔드포인트" — 등록 후 URL 변경 시 ClassIn 재등록 필요.

## 2. 학원 고지 사항 (계약 시 필수 전달)

> 지침 02 §3.3 + §3.4 기반

1. **API와 ClassIn 대시보드 동시 조작 금지** — 데이터 충돌 발생 가능. 반/수업/학생 정보는 둘 중 한 쪽에서만 수정.
2. **학원 PC 상시 가동 필요** — 절전 모드 해제. 끄면 Webhook 유실.
3. **데이터 주권** — ClassIn·Notion·카톡 알림에 쌓이는 데이터는 전부 학원 소유. MOON은 수탁자.
4. **API 키·Notion 토큰 관리 책임** — 학원이 직접 보관. MOON은 세팅·유지보수 목적으로만 일시 접근.
5. **계약서 명시**:
   - SLA (서비스 가용성 범위, ClassIn 장애 제외)
   - 데이터 삭제 정책 (계약 종료 시 Notion DB·토큰 회수)
   - 업데이트 범위 (ClassIn API 변경 시 긴급 패치 범위)

## 3. 장애 대응 플레이북

### 3.1 "Webhook이 안 들어옵니다"
증상: Notion 수업 기록 DB에 오늘자 row가 없음.

1. **tunnel 확인**: `cloudflared tunnel info <name>` → connection active?
2. **로컬 서버**: `curl http://localhost:8787/health` → `ok:true`?
3. **ClassIn 등록 URL**: 브라우저에서 공개 URL `/health` → 200?
4. **dump_dir 확인**: `samples/incoming/` 에 오늘자 파일? 있으면 파싱 실패; 없으면 수신 자체 안 됨.
5. **ClassIn 담당자 연락** — 재전송 정책 확인 + Webhook 에러 이메일 확인.
6. 재전송은 ClassIn 정책에 따름 (미확인 항목). 복구 후에도 과거 이벤트는 수동 재수집 필요할 수 있음.

### 3.2 "서명 에러"
증상: ClassIn API 호출 시 `ClassInAPIError errno=<서명 관련 코드>`

- 시스템 시간 확인 (± 5분 이내여야 함) → Windows 시간 동기화 실행
- `config.yaml` `secret_key` 오타 확인
- `X-EEO-SIGN` 문자열이 ASCII 정렬인지 로그 확인 (`--verbose`)

### 3.3 "카톡 문구에 엉뚱한 학생 이름이"
- Claude가 프롬프트 밖 환각. `intelligence/prompts/missing_homework.md` 에 "환각 금지" 재강조.
- 입력 payload에 들어간 학생 목록 로그로 확인 — Notion ↔ ClassIn UID 매핑 오류일 가능성.

### 3.4 "Notion DB 적재가 일부만 됨"
- 학생 Master DB에 해당 `ClassIn ID` 가 없는 경우 row 생성 스킵 (로그: `no student row for classin_id=...`).
- 조치: `classin-toolkit` 계정 동기화 재실행 (추후 CLI 예정) 또는 수동으로 Notion에 학생 row 추가.

## 4. 백업 정책

- **Notion DB**: 학원 워크스페이스 자체 백업 기능 + 월 1회 CSV export 권장.
- **Webhook 원본**: `samples/incoming/` 는 디스크 여유 확인 후 90일 이상 보관. 이슈 추적·재처리용.
- **config.yaml**: 학원 관리자 비밀 보관함(USB/보안 저장소)에 복사본 1부. 유출 시 ClassIn secret 재발급.

## 5. 정기 점검 (월 1회)

- [ ] Webhook 수신 건수와 ClassIn 대시보드 수업 수 대조
- [ ] Notion 학생 Master DB 와 ClassIn 등록 학생 수 대조 (재원 상태 동기화)
- [ ] `reports_out/notify_dry_run/` 누적 확인 — 발송 대상 학부모 누락 없나
- [ ] 주간 리포트 샘플 1~2건 원장과 리뷰
- [ ] Cloudflare Tunnel 인증서/DNS 상태 확인
- [ ] Claude API 사용량 및 비용 확인

## 6. 개인정보 처리 체크 (계약·운영)

- 학원 = 개인정보 **처리자**, MOON = **수탁자**.
- 처리 항목: 학생 이름/전화번호, 학부모 전화번호, 학습 지표(참여도·성적).
- 제3자 제공: 없음 (ClassIn → Notion → 카톡 릴레이, 모두 학원 계정).
- 보관 기간: 학원 정책에 따름. 계약 종료 시 30일 내 MOON 접근권 회수 + 수탁 데이터 삭제.
- 유출 시 대응: 학원이 주 통지 책임. MOON은 원인 조사·기술적 조치 협력.

## 7. 해제(Offboarding) 절차

학원 계약 종료 시:
- [ ] Cloudflare Tunnel 제거
- [ ] Webhook URL ClassIn에서 삭제 요청
- [ ] Notion Integration 권한 회수 (학원 관리자가 실행)
- [ ] MOON 접근용 토큰·Notion Integration 삭제
- [ ] 학원 PC에서 프로젝트 폴더 삭제 (또는 config.yaml만 보존)
- [ ] 가동 이력 요약본 학원에 전달 (월별 수신 건수, 발송 건수 등)
