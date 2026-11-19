"""Ping every provider with the keys in .env. Spends nothing (list-models calls, one tiny POST).

  uv run python scripts/check_keys.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `bot` importable when run as a script

import httpx  # noqa: E402

from bot.config import get_settings  # noqa: E402


async def check(name: str, coro) -> bool:
    try:
        detail = await coro
        print(f"  OK    {name:<10} {detail}")
        return True
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace("\n", " ")[:160]
        print(f"  FAIL  {name:<10} {type(e).__name__}: {msg}")
        return False


async def sarvam(s):
    async with httpx.AsyncClient(timeout=15) as c:
        # Smallest paid call that proves the key: 1 short TTS request (~₹0.03).
        r = await c.post(
            "https://api.sarvam.ai/text-to-speech",
            headers={"api-subscription-key": s.sarvam_api_key},
            json={"text": "Hello.", "target_language_code": "en-IN", "speaker": s.tts_voice, "model": s.tts_model},
        )
        r.raise_for_status()
        return f"TTS {s.tts_model}/{s.tts_voice} responded"


async def anthropic_(s):
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
    models = [m.id for m in (await client.models.list(limit=100)).data]
    hit = [m for m in models if "haiku" in m]
    if not any(s.llm_model in m for m in models) and s.llm_model not in models:
        return f"key ok; NOTE model {s.llm_model!r} not in list, haiku models: {hit}"
    return f"model {s.llm_model} available; haiku models: {hit}"


async def openai_(s):
    import openai

    client = openai.AsyncOpenAI(api_key=s.openai_api_key)
    models = [m.id for m in (await client.models.list()).data]
    ok = s.judge_model in models
    return f"judge {s.judge_model} {'available' if ok else 'NOT FOUND'}; mini models: {[m for m in models if 'mini' in m][:6]}"


async def groq(s):
    import openai

    client = openai.AsyncOpenAI(api_key=s.groq_api_key, base_url="https://api.groq.com/openai/v1")
    models = [m.id for m in (await client.models.list()).data]
    return f"open-weight candidates: {[m for m in models if 'qwen' in m or 'gpt-oss' in m][:6]}"


async def deepgram(s):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://api.deepgram.com/v1/projects", headers={"Authorization": f"Token {s.deepgram_api_key}"})
        r.raise_for_status()
        return "key ok"


async def webhook(s):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(s.webhook_url, json={"ping": "collections-voicebot check_keys"})
        r.raise_for_status()
        return f"POST {r.status_code} — check webhook.site for the ping"


async def main():
    s = get_settings()
    print(f"Prompt version {s.prompt_version} | LLM {s.llm_model} | judge {s.judge_model} | STT {s.stt_language}/{s.stt_mode} | TTS {s.tts_model}/{s.tts_voice}\n")
    results = await asyncio.gather(
        check("Sarvam", sarvam(s)),
        check("Anthropic", anthropic_(s)),
        check("OpenAI", openai_(s)),
        check("Groq", groq(s)),
        check("Deepgram", deepgram(s)),
        check("Webhook", webhook(s)),
    )
    print("\nAll good." if all(results) else "\nSome checks failed — fix the key or the model name in .env and rerun.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
