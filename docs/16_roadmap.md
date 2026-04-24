# 4주 실행 타임라인

지침(03_plan §3)을 코드 구조에 매핑한 체크리스트.

## Week 1 — 코어 엔진

- [x] FastAPI Webhook 수신기 스켈레톤 (`webhook_receiver.py`)
- [x] CED API 래퍼 초안 (`classin/ced.py`)
- [x] Claude 스케줄 파싱 프롬프트 (`intelligence/prompts/schedule_parse.md`)
- [ ] 실제 ClassIn 샘플 Webhook 페이로드 확보 → alias 확정
- [ ] Notion DB 3종 실제 생성 + ID 확보
- [ ] Cloudflare Tunnel 연결 테스트

## Week 2 — MVP1 (미제출 알림)

- [x] Notion 적재 로직 (`storage/notion_repo.py::upsert_lesson_record`)
- [x] 미제출 추출 + 학생별 Claude 문구 (`intelligence/missing_homework.py`)
- [x] 카톡 dry_run 덤프 (`notify/dispatcher.py`)
- [ ] 실제 After-Class 페이로드로 end-to-end 검증
- [ ] 수업 종료 후 30분 내 자동 플로우 타이밍 측정

## Week 3 — MVP2 (주간 리포트)

- [x] 주간 집계 쿼리 (`storage/notion_repo.py::weekly_student_stats`)
- [x] 학생별 개인화 리포트 (`intelligence/weekly_report.py`)
- [x] Notion 페이지 저장 + 학부모 문구 컬럼
- [ ] 5명 페르소나 샘플 데이터 세팅
- [ ] 리포트 차별화 검증 (수동 읽어보기)

## Week 4 — 데모·영업

- [ ] 3~5분 Loom 영상 촬영
- [ ] 제안서 PDF 작성
- [ ] 아는 원장님 1분에게 영상 공유 → 반응 수집
