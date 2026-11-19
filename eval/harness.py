"""Text-level simulation harness.

Runs the SAME prompt, tools, guardrails and CallSession as the voice pipeline,
minus STT/TTS, so a scenario can be replayed against every prompt version in
seconds. A simulated customer (an LLM playing a persona, or a fixed script)
talks to the bot until an outcome is recorded or the turn cap is hit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from bot.config import REPO_ROOT, Settings, get_settings
from bot.prompts import KICKOFF_INSTRUCTION, TURN_BUDGET_NUDGE
from bot.session import CallSession
from eval.llm_client import LLMClient, client_from_settings

log = logging.getLogger(__name__)

HANGUP = "[HANGUP]"
_GOODBYE_RE = re.compile(r"\b(goodbye|good bye|bye)\b", re.I)
MAX_EXCHANGES = 14  # customer utterances per call


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


@dataclass
class Scenario:
    id: str
    title: str
    customer_id: str
    difficulty: str  # easy | tricky | trap
    language: str  # english | hinglish
    persona: str
    script: str
    expected_outcome: str
    expected_transfer_reason: str | None = None
    expected_callback_reason: str | None = None
    knows_otp: bool = True
    gives_wrong_otp_first: bool = False
    turns: list[str] | None = None  # fixed script instead of an LLM customer
    checks: list[str] = field(default_factory=list)  # judge checks, plain English
    buckets: list[str] = field(default_factory=list)  # factual | compliance | scope | adherence | outcome
    source: str = "brief"  # brief | inferred
    failure_case: str = ""  # which named failure case this probes

    @staticmethod
    def load_all(path: Path) -> list["Scenario"]:
        data = yaml.safe_load(path.read_text())
        return [Scenario(**d) for d in data["scenarios"]]


# --------------------------------------------------------------------------- #
# Simulated customer
# --------------------------------------------------------------------------- #

CUSTOMER_SYSTEM = """You are role-playing a credit card customer of Mumbai Bank receiving a collections phone call. Stay in character. Speak the way a real Indian customer would on the phone: short, natural, sometimes interrupting with a question.

Today's date: {today}. When you give a date, say it the way a real customer would ("the 21st", "this Friday", "next week") — do not read out a full date with the year.
Persona: {persona}
Language style: {language_note}
Your OTP, if the bot asks you to read out a code: {otp_note}

What you are trying to do in this call (follow these beats in order, one beat per reply, adapting to what the bot says):
{script}

Rules for you:
- Reply with ONLY what the customer says out loud. No narration, no quotes, no stage directions.
- One to two sentences per reply.
- Do not volunteer information the bot has not asked for unless the script says so.
- Never hang up while the bot is asking you a question: answer it first (a short "yes", "no" or the detail it asked for). Reply with exactly {hangup} only after the bot has said goodbye, or if you have delivered all your beats AND the bot's last message was not a question.
- Never break character, never mention you are an AI.
"""

LANGUAGE_NOTES = {
    "english": "Indian English, plain.",
    "hinglish": "A natural Hindi-English mix in Roman script, e.g. 'haan ji, maine kal UPI se pay kar diya tha'. Use Hindi words freely, keep numbers in digits.",
}


class SimulatedCustomer:
    def __init__(self, scenario: Scenario, otp: str, client: LLMClient | None, today: date | None = None):
        self.sc = scenario
        today = today or date.today()
        self.client = client
        self.turn_index = 0
        self.history: list[dict[str, Any]] = []
        if scenario.gives_wrong_otp_first:
            otp_note = f"first read out the WRONG code 1234; if asked again, read out the correct code {otp}"
        elif scenario.knows_otp:
            otp_note = f"the code is {otp}"
        else:
            otp_note = "you did not receive any code; say so, and do not make one up"
        self.system = CUSTOMER_SYSTEM.format(
            persona=scenario.persona, language_note=LANGUAGE_NOTES.get(scenario.language, scenario.language), otp_note=otp_note, script=scenario.script, hangup=HANGUP,
            today=today.strftime("%A, %d %B %Y"),
        )

    async def reply(self, bot_text: str) -> str:
        if self.sc.turns is not None:
            if self.turn_index >= len(self.sc.turns):
                return HANGUP
            t = self.sc.turns[self.turn_index]
            self.turn_index += 1
            return t
        assert self.client is not None
        self.history.append({"role": "user", "content": f"Bot said: {bot_text}"})
        r = await self.client.chat(self.system, self.history)
        text = r.text.strip().strip('"')
        self.history.append({"role": "assistant", "content": text})
        self.turn_index += 1
        return text


# --------------------------------------------------------------------------- #
# Bot runner (mirrors voice_bot.py's control flow without audio)
# --------------------------------------------------------------------------- #


@dataclass
class RunResult:
    scenario_id: str
    prompt_version: str
    llm_model: str
    call_id: str
    outcome: str | None
    transfer_reason: str | None
    callback_reason: str | None
    transcript: list[dict[str, Any]]
    guardrail_events: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    bot_turns: int
    customer_turns: int
    otp_verified: bool
    forced_transfer: bool
    outcome_missing_at_end: bool
    wall_seconds: float
    llm_latencies: list[float]
    usage_input: int
    usage_output: int
    error: str | None = None


async def run_scenario(scenario: Scenario, settings: Settings, bot_client: LLMClient, customer_client: LLMClient | None, today: date | None = None) -> RunResult:
    s = get_settings(**{k: getattr(settings, k) for k in ("prompt_version", "llm_model", "webhook_url", "anthropic_api_key", "openai_api_key", "groq_api_key", "sarvam_api_key")})
    s.customer_id = scenario.customer_id
    session = CallSession(s)
    session.channel = "simulated"
    if today:
        session.today = today
    customer = SimulatedCustomer(scenario, session.account.otp, customer_client, session.today)

    system = session.system_prompt()
    tools = session.tool_schemas()
    messages: list[dict[str, Any]] = [{"role": "user", "content": KICKOFF_INSTRUCTION}]
    latencies: list[float] = []
    usage_in = usage_out = 0
    t0 = time.time()
    closing_response_done = False
    error = None

    async def bot_turn() -> str:
        """One bot turn = LLM call(s) until no tool calls remain. Returns spoken text."""
        nonlocal usage_in, usage_out, closing_response_done
        spoken: list[str] = []
        for _ in range(6):  # tool-call loop guard
            t1 = time.time()
            reply = await bot_client.chat(system, messages, tools)
            latencies.append(round(time.time() - t1, 3))
            usage_in += reply.usage.get("input", 0)
            usage_out += reply.usage.get("output", 0)
            messages.append({"role": "assistant", "content": reply.text, "tool_calls": reply.tool_calls})
            if reply.text:
                filtered = session.filter_bot_text(reply.text)  # same transform the TTS applies
                session.log_bot(filtered)
                spoken.append(filtered)
            if not reply.tool_calls:
                break
            for tc in reply.tool_calls:
                result = await session.handle_tool(tc.name, tc.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        session.note_bot_turn_end()
        if session.closing:
            closing_response_done = True
        return " ".join(spoken)

    try:
        bot_text = await bot_turn()
        for _ in range(MAX_EXCHANGES):
            # --- turn-level guardrails, same order as BotTurnMonitor
            if session.needs_forced_transfer():
                line = await session.force_transfer()
                session.log_bot(line, forced=True)
                break
            # Mirrors the voice call controller: the call ends once the outcome is recorded AND the bot
            # has said goodbye (or the outcome is one whose scripted line is the whole close).
            if session.closing and closing_response_done and (_GOODBYE_RE.search(bot_text or "") or getattr(session, "end_after_speech", False)):
                break
            if session.over_budget():
                messages.append({"role": "user", "content": f"[Call controller] {TURN_BUDGET_NUDGE}"})
                bot_text = await bot_turn()
                continue
            cust = await customer.reply(bot_text)
            if cust.strip() == HANGUP:
                break
            session.log_customer(cust)
            messages.append({"role": "user", "content": cust})
            bot_text = await bot_turn()
        # one more guardrail pass after the loop (e.g. trigger on the last customer line)
        if session.needs_forced_transfer():
            line = await session.force_transfer()
            session.log_bot(line, forced=True)
    except Exception as e:  # noqa: BLE001
        log.exception("scenario %s failed", scenario.id)
        error = f"{type(e).__name__}: {e}"

    outcome_missing = session.outcome is None
    await session.finalize()
    d = session.to_dict()
    oc = session.outcome
    return RunResult(
        scenario_id=scenario.id,
        prompt_version=s.prompt_version,
        llm_model=s.llm_model,
        call_id=session.call_id,
        outcome=oc.outcome.value if oc else None,
        transfer_reason=oc.transfer_reason.value if oc and oc.transfer_reason else None,
        callback_reason=oc.callback_reason.value if oc and oc.callback_reason else None,
        transcript=d["transcript"],
        guardrail_events=d["guardrail_events"],
        tool_calls=d["tool_calls"],
        bot_turns=session.bot_turns,
        customer_turns=len(session.customer_turns()),
        otp_verified=session.otp_verified,
        forced_transfer=any(e["kind"] == "forced_transfer" for e in session.guardrail_events),
        outcome_missing_at_end=outcome_missing,
        wall_seconds=round(time.time() - t0, 1),
        llm_latencies=latencies,
        usage_input=usage_in,
        usage_output=usage_out,
        error=error,
    )


def transcript_text(result: RunResult) -> str:
    lines = []
    for t in result.transcript:
        role = {"bot": "BOT", "customer": "CUSTOMER", "tool": "  [tool]", "system": "  [system]"}.get(t["role"], t["role"])
        lines.append(f"{role}: {t['text']}")
    return "\n".join(lines)
