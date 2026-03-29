#!/usr/bin/env python3
"""Scripted integration smoke: in-process API health and sync/send error paths.
Run from repo root after uv sync (or pip install). Exit 0 iff all checks pass.
"""
from __future__ import annotations

import logging
import sys


logger = logging.getLogger(__name__)


def main() -> int:
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from libs.core.logging_config import setup_logging
    setup_logging(log_dir="-")  # console-only for the smoke script

    from fastapi.testclient import TestClient
    from apps.api.main import app

    client = TestClient(app)
    r = client.get("/health")
    if r.status_code != 200 or not r.json().get("ok"):
        logger.error("FAIL /health: status=%s body=%s", r.status_code, r.json())
        return 1
    r = client.post("/sync", json={"account_id": 99999, "limit_per_thread": 50})
    if r.status_code != 404:
        logger.error("FAIL /sync unknown account expected 404, got %s", r.status_code)
        return 1
    r = client.post(
        "/send",
        json={"account_id": 99999, "recipient": "x", "text": "hi", "idempotency_key": None},
    )
    if r.status_code != 404:
        logger.error("FAIL /send unknown account expected 404, got %s", r.status_code)
        return 1
    logger.info("integration_smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
