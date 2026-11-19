"""Thin provider-neutral chat client with tool calling.

Two backends: Anthropic (default bot model) and any OpenAI-compatible endpoint
(OpenAI for the judge, Groq for the open-weight bake-off). The bot's prompt and
tool schemas are provider-neutral JSON, converted here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient:
    """messages: list of {"role": "user"|"assistant"|"tool", ...} in a neutral shape:
    - user:      {"role":"user","content": str}
    - assistant: {"role":"assistant","content": str, "tool_calls":[ToolCall...]}
    - tool:      {"role":"tool","tool_call_id": str, "content": dict}
    """

    def __init__(self, provider: str, model: str, api_key: str, base_url: str | None = None, temperature: float = 0.3, max_tokens: int = 350):
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        if provider == "anthropic":
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        elif provider in ("openai", "groq", "openai_compatible"):
            import openai

            if provider == "groq" and not base_url:
                base_url = "https://api.groq.com/openai/v1"
            self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        else:
            raise ValueError(f"unknown provider {provider}")

    # ------------------------------------------------------------------ #
    async def chat(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, json_only: bool = False) -> ChatReply:
        if self.provider == "anthropic":
            return await self._anthropic(system, messages, tools or [])
        return await self._openai(system, messages, tools or [], json_only)

    # ------------------------------------------------------------------ #
    async def _anthropic(self, system: str, messages: list[dict], tools: list[dict]) -> ChatReply:
        conv: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                if conv and conv[-1]["role"] == "user" and isinstance(conv[-1]["content"], list):
                    conv[-1]["content"].append({"type": "text", "text": m["content"]})
                else:
                    conv.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
                conv.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "..."}]})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": json.dumps(m["content"], default=str)}
                if conv and conv[-1]["role"] == "user" and isinstance(conv[-1]["content"], list):
                    conv[-1]["content"].append(block)
                else:
                    conv.append({"role": "user", "content": [block]})
        # Anthropic SDK >= 1.17 no longer accepts sampling params (temperature/top_p); send only what it takes.
        kwargs: dict[str, Any] = dict(model=self.model, system=system, messages=conv, max_tokens=self.max_tokens)
        if tools:
            kwargs["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
        resp = await self._client.messages.create(**kwargs)
        text_parts, calls = [], []
        for b in resp.content:
            if b.type == "text":
                text_parts.append(b.text)
            elif b.type == "tool_use":
                calls.append(ToolCall(b.id, b.name, dict(b.input or {})))
        usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
        return ChatReply(" ".join(p.strip() for p in text_parts if p.strip()), calls, resp, usage)

    async def _openai(self, system: str, messages: list[dict], tools: list[dict], json_only: bool) -> ChatReply:
        conv: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                conv.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [
                        {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}} for tc in m["tool_calls"]
                    ]
                conv.append(entry)
            elif m["role"] == "tool":
                conv.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": json.dumps(m["content"], default=str)})
        kwargs: dict[str, Any] = dict(model=self.model, messages=conv)
        # Newer OpenAI models reject temperature/max_tokens in places; keep it minimal for the judge.
        if self.provider != "openai":
            kwargs.update(temperature=self.temperature, max_tokens=self.max_tokens)
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]
        if json_only:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        usage = {"input": getattr(resp.usage, "prompt_tokens", 0), "output": getattr(resp.usage, "completion_tokens", 0)}
        return ChatReply((msg.content or "").strip(), calls, resp, usage)


def client_from_settings(settings, role: str = "bot") -> LLMClient:
    """role = "bot" | "judge" | "customer". Provider inferred from the model name."""
    if role == "judge":
        model = settings.judge_model
    elif role == "customer":
        model = getattr(settings, "customer_model", None) or settings.llm_model
    else:
        model = settings.llm_model
    # The judge returns a long JSON verdict; the bot and customer speak in short turns.
    max_tokens = 1500 if role == "judge" else 350
    if model.startswith("claude"):
        return LLMClient("anthropic", model, settings.anthropic_api_key, temperature=0.3 if role == "bot" else 0.7, max_tokens=max_tokens)
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return LLMClient("openai", model, settings.openai_api_key, temperature=0.0, max_tokens=max_tokens)
    # anything else: assume Groq-hosted open-weight (qwen/..., openai/gpt-oss-..., llama...)
    return LLMClient("groq", model, settings.groq_api_key, temperature=0.3 if role == "bot" else 0.7, max_tokens=max_tokens)
