#!/usr/bin/env bash
# 로컬 Webhook 수신기(:8787)를 공개 HTTPS 로 노출 — 빠른 테스트용 Quick Tunnel.
#
# 사전:
#   - cloudflared 설치 (brew install cloudflared)
#   - 다른 터미널에서 로컬 수신기 기동: scripts/serve-webhook.sh  (또는 classin-webhook)
#
# 동작:
#   cloudflared 가 https://<랜덤>.trycloudflare.com 을 발급해 localhost:8787 로 프록시한다.
#   ClassIn Datasub 등록용 엔드포인트는:   <발급 URL>/classin/webhook
#   헬스체크(외부 도달 확인):              <발급 URL>/health   → {"ok":true,...}
#
# 주의:
#   - 계정/DNS 불필요하지만 URL 이 매번 바뀌고 프로세스를 끄면 사라진다 → 1회 테스트용.
#   - 고정 URL(운영)이 필요하면 scripts/cloudflared-config.example.yml 의 named tunnel 사용.
#   - ClassIn 은 "1기관 1엔드포인트"라 등록 후 URL 을 바꾸면 ClassIn 재등록이 필요하다.
set -euo pipefail
PORT="${WEBHOOK_PORT:-8787}"
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared 가 없습니다. 먼저: brew install cloudflared" >&2
  exit 1
fi
echo "[tunnel-quick] http://localhost:${PORT} 공개 노출 시작…"
echo "[tunnel-quick] 발급 URL 확인 후 → <URL>/health 로 외부 도달 검증, <URL>/classin/webhook 을 ClassIn 에 등록"
exec cloudflared tunnel --url "http://localhost:${PORT}"
