# ClassIn Toolkit 문서 지도

이 디렉터리의 문서는 크게 **정책 → 스펙 → 운영**의 3층 구조다.
정책이 바뀌면 스펙이 따라가고, 스펙이 바뀌면 운영 매뉴얼이 따라가야 한다.

## 정책 (Policy) — 무엇을 왜 만드는가

| 번호 | 문서 | 요지 |
|---|---|---|
| 01 | [identity_overview](01_identity_overview.md) | 프로젝트 정의·포지셔닝·기능 모듈 지도·비즈니스 모델 |
| 02 | [guidelines](02_guidelines.md) | API·개발·영업·카톡·Notion 운영 원칙 |
| 03 | [plan](03_plan.md) | 4주 실행 계획 (코어 엔진 → MVP1 → MVP2 → 데모) |

## 스펙 (Spec) — 어떻게 조립되어 있는가

| 번호 | 문서 | 요지 |
|---|---|---|
| 10 | [architecture](10_architecture.md) | Layer 분리 구조 + 실 코드 파일 매핑 + 데이터 흐름 |
| 11 | [api_integration](11_api_integration.md) | ClassIn API v2 서명·엔드포인트·Webhook Cmd + 미확정 항목 |
| 12 | [notion_schema](12_notion_schema.md) | Notion DB 3종 컬럼 정의 (학생·수업 기록·리포트) |

## 운영 (Ops) — 어떻게 굴리는가

| 번호 | 문서 | 요지 |
|---|---|---|
| 13 | [operations_runbook](13_operations_runbook.md) | 학원 PC 설치·Tunnel 구동·장애 대응·백업·학원 고지사항 |
| 14 | [developer_guide](14_developer_guide.md) | 로컬 개발 셋업·CLI·테스트·신규 Cmd 추가 절차 |
| 15 | [demo_scenario](15_demo_scenario.md) | 가상 학원 + 5 페르소나 + 3~5분 데모 플롯 |
| 16 | [roadmap](16_roadmap.md) | Week 단위 실행 체크리스트 (진행 상태 추적) |
| 17 | [test_readiness](17_test_readiness.md) | 테스트 버전 준비물·API 키·config 누락 점검 |
| 18 | [teacher_dashboard_data_merge](18_teacher_dashboard_data_merge.md) | 선생님 상황판 UX + 보고서·로컬/오프라인 데이터 병합 기준 |

## 읽는 순서 (역할별)

- **MOON (본인)**: 01 → 03 → 10 → 11 → 16
- **새로 합류한 개발자**: 00 → 10 → 14 → 11 → 13
- **상황판/보고서 병합 작업자**: 00 → 18 → 10 → 14 → 15
- **학원 원장에게 전달할 때**: 01(요약만) + 13 (고지사항 섹션) + 15
- **ClassIn 한국 지사 이해충돌 확인**: 01 §1 + 02 §3.4

## 갱신 원칙

- 정책 문서(01~03)는 사용자(MOON) 판단으로만 변경. 코드 관점 제안은 스펙/운영 문서에서.
- 스펙 문서(10~12)는 코드 변경과 **같은 PR**에서 갱신 — 드리프트 금지.
- 운영 문서(13~16)는 파일럿 학원 피드백마다 갱신.
