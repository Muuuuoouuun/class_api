# 17. 테스트 버전 준비 점검

`check-ready` 는 API 키를 실제 호출하지 않고, `config.yaml` 이 테스트 단계별로 충분히 채워졌는지 확인한다.
키 값은 화면에 마스킹되어 출력되며, 실제 토큰을 채팅이나 Git 에 붙여넣지 않는다.

`diagnose-apis` 는 그 다음 단계다. 기본 실행은 offline 표만 보여주고, `--live` 를 붙였을 때만
ClassIn/Notion/Claude/Aligo 에 비파괴 probe를 보낸다. 수업·Notion row·카톡 메시지는 생성하지 않는다.

## 모드

| 모드 | 목적 | 필요한 외부 API |
|---|---|---|
| `local-demo` | 샘플 Webhook JSON 재생 + Notion/Claude/HTML 검증 | Notion, Claude |
| `classin-live` | 실제 ClassIn Webhook/CED 연동 검증 | Notion, Claude, ClassIn, Cloudflare Tunnel |
| `kakao-live` | 실제 알림톡 발송 전 최종 점검 | 위 전부 + 알리고/솔라피 |

## 실행

```bash
classin-toolkit check-ready --mode local-demo --config config.yaml
classin-toolkit check-ready --mode classin-live --config config.yaml
classin-toolkit check-ready --mode kakao-live --config config.yaml
classin-toolkit diagnose-apis --config config.yaml
classin-toolkit diagnose-apis --live --config config.yaml
```

`MISSING` 또는 `BLOCKED` 가 있으면 해당 모드는 아직 준비되지 않은 상태다.
`WARN` 은 바로 막히지는 않지만 운영 전에 확인해야 하는 항목이다.

`diagnose-apis --live` 의 판정 기준:

- ClassIn v1: 더미 SSO 요청이 파라미터 오류로 거절되면 서버 도달은 성공으로 본다. 실제 인증 확정은 실제 `uid/course_id/class_id/telephone` 로 `sso-link` 를 실행해야 한다.
- ClassIn v2 LMS: 빈 `createUnit` payload가 서명 오류가 아닌 검증 오류로 거절되면 signing path가 통과한 것으로 본다. `签名异常`/signature 오류는 v2 signing key 또는 LMS API 권한 확인 대상이다.
- Notion: 각 DB ID를 `retrieve` 로 읽어 Integration 공유 권한을 확인한다.
- Claude: 아주 짧은 `messages.create` 로 API key, billing, model 접근을 확인한다.
- Aligo: 카카오 `heartinfo` 잔여건수 조회만 실행한다. 실제 발송은 하지 않는다.

## local-demo 최소 준비물

- Notion Integration token
- Notion DB ID 5개: 학생 Master, 수업 기록, 리포트, 메모, 시험
- Claude API key
- `samples/attendance_sample.json`
- `samples/end_summary_sample.json`
- `samples/homework_submit_sample.json`
- `notify.mode: dry_run`

ClassIn SID/secret 은 `local-demo` 에서는 없어도 된다. 샘플 JSON 을 `replay-webhook` 으로 재생하기 때문이다.

## Notion DB 자동 생성

원장님에게는 "테스트용 빈 Notion 페이지 하나"만 만들게 하고, 그 페이지를 Integration에 공유하게 한다.
그 다음 아래 명령으로 DB 5개를 만든다.

```bash
classin-toolkit setup-notion --parent-page-id <NOTION_PAGE_ID> --dry-run
classin-toolkit setup-notion --parent-page-id <NOTION_PAGE_ID> --write --config config.yaml
```

명령이 출력하는 `notion.databases.*` 값을 `config.yaml`에 붙여넣고 다시 `check-ready`를 실행한다.

## classin-live 추가 준비물

- ClassIn `SID`
- ClassIn v2 `secret_key`
- Webhook `SafeKey` 검증용 secret
- Cloudflare Tunnel 공개 URL
- ClassIn Datasub 등록 URL: `https://<host>/classin/webhook`

## kakao-live 주의

`kakao-live` 모드는 실제 발송 전 최종 게이트다. 아래 값이 모두 있어야 통과한다.

- `notify.mode: live`
- `notify.provider: aligo`
- `notify.aligo.api_key`, `user_id`, `sender`
- `notify.aligo.sender_key`
- `notify.aligo.template_code_missing_homework`

알리고 알림톡은 승인된 템플릿 서식과 `message` 개행·문구가 일치하지 않으면 전송되지 않는다.
템플릿 심사 전에는 `notify.mode: dry_run` 을 유지한다.
