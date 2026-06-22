# 데모 시나리오 (Week 4)

지침(03_plan §3.1): 가상 "○○수학학원" + 5 페르소나. 각 페르소나별 리포트가 반드시 다르게 나와야 함.

## 페르소나

| ID | 이름 | 특징 | 기대 리포트 톤 |
|---|---|---|---|
| S_001 | 박성실 | 출석률 100%, 숙제 항상 제출, 참여도 높음 | 칭찬 + 다음 단계 제안 |
| S_002 | 김지각 | 지각 잦음, 숙제 누락 | 부드러운 경고 + 등원 시간 제안 |
| S_003 | 이하락 | 최근 참여도·집중도 하락 | 상담 제안 |
| S_004 | 정활발 | 참여도 최고, 리더형 | 강점 강화 + 심화 과제 제안 |
| S_005 | 최결석 | 3주 연속 결석 경고 | 상담 필수 / 원장 자동 알림 |

## 3~5분 데모 영상 플롯

0. `classin-toolkit check-ready --mode local-demo` 로 설정 누락 확인
1. `classin-toolkit seed-demo-data --write` 로 5명 페르소나 학생·수업 기록 생성
2. 스케줄 CSV 를 원장이 드롭 → `classin-toolkit parse-schedule` 실행
3. ClassIn 대시보드에 수업·숙제가 일괄 생성됨
4. 수업 종료 직후 `replay-webhook` 으로 After-Class 페이로드 재생
5. Notion 수업 기록 DB 에 5명 데이터 자동 적재 (화면 분할)
6. `notify_dry_run/*.md` 에 학생마다 다른 카톡 문구 생성된 것 보여주기
7. 로컬 공유 자료 예시(CSV/XLSX 상담 메모)를 학생별 맥락으로 붙여 상황판에서 확인
8. 성과 대시보드에서 코스/학생을 검색하고 출결률·평균점수·미제출 트렌드, 위험도 분류를 확인
9. 매주 금요일 `weekly-reports` → Notion 리포트 페이지 5개 (각기 다른 내용)
10. 보고서 미리보기에서 ClassIn 기록 + 오프라인 데이터가 함께 반영된 코멘트 확인

## 데모 데이터 생성

기본은 dry-run 이라 Notion 에 쓰지 않는다.

```bash
classin-toolkit ui --demo
classin-toolkit seed-demo-data --dry-run
classin-toolkit seed-demo-data --write --base-date 2026-04-24 --weeks 3
```

`ui --demo` 는 `config.yaml` 과 Notion 없이 5명 페르소나 상황판을 바로 띄우는 영업/설명용 모드다.
실제 Notion DB에 페르소나 데이터를 쓰려면 `seed-demo-data --write` 를 별도로 실행한다.

`--base-date` 는 최신 리포트 주간 기준일이다. 입력한 날짜가 속한 주의 월요일을 최신 주차로 삼고,
그 이전 주차까지 생성해 지난 주 대비 변화가 나오게 한다.

## 원장 반응 목표

> "이거 얼마면 살게요"

## 제안서 연동 (03_plan §3.2)

1. 문제 공감 (이런 거 귀찮으시죠?)
2. 데모 영상 Loom 링크
3. 기존 ClassIn 사용 방식은 그대로
4. 가격: 초기 설치비 + 월 유지비
5. 14일 무료 체험 제안
