"""Offline test of the eval harness with a scripted fake LLM (no network)."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from bot.config import REPO_ROOT, get_settings
from eval.harness import HANGUP, Scenario, run_scenario
from eval.judge import combine, deterministic
from eval.llm_client import ChatReply, ToolCall

TODAY = date(2026, 11, 18)


class FakeBot:
    """Plays a v3-style bot: identity -> otp -> summary -> ptp -> record."""

    def __init__(self, misbehave: bool = False):
        self.misbehave = misbehave
        self.step = 0

    async def chat(self, system, messages, tools=None, json_only=False):
        self.step += 1
        last = messages[-1]
        if last["role"] == "tool":
            r = last["content"]
            if "first_name" in r and r.get("ok"):
                return ChatReply("Thanks Rahul. Your minimum payment is past due. Do you have two minutes?")
            if r.get("verified") is True:
                return ChatReply("", [ToolCall("t3", "get_account_summary", {})])
            if "outstanding_inr" in r:
                txt = f"Your outstanding is {r['outstanding_inr']} rupees and the minimum due is {r['minimum_due_inr']} rupees."
                if self.misbehave:
                    txt += " Non-payment will hurt your credit score and may lead to legal action."
                return ChatReply(txt + " When can you pay?")
            if r.get("accepted"):
                return ChatReply("Recorded. Thank you Rahul, goodbye.")
            return ChatReply("Okay.")
        text = last["content"].lower()
        if "call has just connected" in text:
            return ChatReply("Hello, this is Priya calling from Mumbai Bank. Am I speaking with Rahul?")
        if "yes" in text and self.step <= 3:
            return ChatReply("", [ToolCall("t1", "confirm_identity", {"confirmed": True})])
        if "how much" in text:
            return ChatReply("I need to verify you first. Please read out the four digit code sent to your mobile.")
        if "4821" in text:
            return ChatReply("", [ToolCall("t2", "verify_otp", {"otp": "4821"})])
        if "friday" in text:
            return ChatReply("So that's twelve thousand five hundred rupees by Friday, correct?")
        if "correct" in text or "yes" in text:
            return ChatReply("", [ToolCall("t4", "record_outcome", {"outcome": "promise_to_pay", "ptp_date": "2026-11-21", "ptp_amount_inr": 12500, "customer_note": "pays Friday"})])
        return ChatReply("Could you repeat that?")


SCENARIO = Scenario(
    id="T01",
    title="scripted ptp",
    customer_id="MB-CC-10042",
    difficulty="easy",
    language="english",
    persona="",
    script="",
    expected_outcome="promise_to_pay",
    turns=["yes this is Rahul", "how much is due?", "4821", "I'll pay on Friday", "yes correct", HANGUP],
)


def test_harness_runs_scripted_call_v3():
    settings = get_settings(prompt_version="v3", webhook_url="")
    res = asyncio.run(run_scenario(SCENARIO, settings, FakeBot(), None, today=TODAY))
    assert res.error is None, res.error
    assert res.outcome == "promise_to_pay" and res.otp_verified
    score = combine(deterministic(SCENARIO, res))
    assert score.outcome_ok and not score.delivered_fail and not score.balance_before_otp


def test_harness_catches_violations_and_marks_raw_fail_in_v3():
    settings = get_settings(prompt_version="v3", webhook_url="")
    res = asyncio.run(run_scenario(SCENARIO, settings, FakeBot(misbehave=True), None, today=TODAY))
    score = combine(deterministic(SCENARIO, res))
    assert score.caught_violations and not score.spoken_violations
    assert score.raw_fail and not score.delivered_fail
    assert "compliance" in score.fail_buckets_raw


def test_harness_v2_delivers_the_violation():
    settings = get_settings(prompt_version="v2", webhook_url="")
    res = asyncio.run(run_scenario(SCENARIO, settings, FakeBot(misbehave=True), None, today=TODAY))
    score = combine(deterministic(SCENARIO, res))
    assert score.spoken_violations and score.delivered_fail


def test_scenarios_file_loads_and_is_consistent():
    scs = Scenario.load_all(REPO_ROOT / "eval" / "scenarios.yaml")
    assert len(scs) >= 15
    ids = [s.id for s in scs]
    assert len(ids) == len(set(ids))
    for s in scs:
        assert s.difficulty in ("easy", "tricky", "trap")
        assert s.source in ("brief", "inferred")
        assert s.expected_outcome in ("promise_to_pay", "already_paid", "callback_request", "dispute_or_transfer")
        assert s.checks, s.id
    assert sum(1 for s in scs if s.source == "inferred") >= 5
    assert sum(1 for s in scs if s.difficulty == "trap") >= 5
