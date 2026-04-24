"""uvicorn 기반 Webhook 서버 실행."""
from __future__ import annotations

import uvicorn

from ..config import load_config


def run() -> None:
    cfg = load_config()
    uvicorn.run(
        "classin_toolkit.webhook_receiver:app",
        host=cfg.webhook.host,
        port=cfg.webhook.port,
        factory=True,
        reload=False,
    )


if __name__ == "__main__":
    run()
