"""POST the outcome to the CRM stand-in (webhook.site)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


async def post_outcome(url: str, payload: dict[str, Any], *, retries: int = 1, timeout: float = 6.0) -> dict[str, Any]:
    """Fire the webhook. Never raises: a CRM hiccup must not break the call."""
    if not url:
        log.warning("WEBHOOK_URL not set; outcome not posted: %s", json.dumps(payload)[:200])
        return {"status": "skipped", "reason": "no_url"}
    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, json=payload, headers={"Content-Type": "application/json", "X-Source": "mumbai-bank-collections-bot"})
            if r.status_code < 400:
                return {"status": "ok", "http": r.status_code, "attempt": attempt + 1}
            last_err = f"http {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            await asyncio.sleep(0.5)
    log.error("webhook failed after %d attempts: %s", retries + 1, last_err)
    return {"status": "failed", "error": last_err}
