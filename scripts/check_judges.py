"""Ping each judge model with a one-line JSON request. `uv run python scripts/check_judges.py`"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import get_settings  # noqa: E402
from eval.llm_client import client_from_settings  # noqa: E402
from eval.run import FALLBACK_JUDGES  # noqa: E402


async def one(model: str):
    c = client_from_settings(get_settings(judge_model=model), "judge")
    try:
        r = await c.chat('Reply with JSON only: {"ok": true, "severity": 0}', [{"role": "user", "content": "go"}], json_only=True)
        print(f"  OK    {model:<22} -> {r.text[:80]}")
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL  {model:<22} -> {type(e).__name__}: {str(e)[:260]}")


async def main():
    s = get_settings()
    for m in [s.judge_model] + FALLBACK_JUDGES:
        await one(m)


asyncio.run(main())
