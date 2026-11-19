"""A stand-in for the bank's CRM: receives outcome webhooks and shows them.

  uv run python scripts/crm_receiver.py          # listens on http://localhost:8787/outcome
  then set WEBHOOK_URL=http://localhost:8787/outcome in .env (or in front of the run command)

Every outcome is appended to logs/crm_inbox.jsonl and printed to the terminal, so the
structured output can be watched live during a demo without relying on webhook.site.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import REPO_ROOT  # noqa: E402

INBOX = REPO_ROOT / "logs" / "crm_inbox.jsonl"
app = FastAPI(title="Mumbai Bank CRM stand-in")
received: list[dict] = []


@app.post("/outcome")
async def outcome(req: Request):
    payload = await req.json()
    entry = {"received_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
    received.append(entry)
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    with INBOX.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print("\n=== OUTCOME RECEIVED ===")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("========================\n", flush=True)
    return {"status": "accepted", "count": len(received)}


@app.get("/")
async def index():
    return {"service": "crm-stand-in", "received": len(received), "latest": received[-1] if received else None}


if __name__ == "__main__":
    print(f"CRM stand-in listening on http://localhost:8787/outcome  (inbox: {INBOX})")
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
