"""Offline tests: no API keys, no network. Run with `pytest`."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from bot.config import get_settings
from bot.guardrails import check_sentence, classify_language, detect_trigger, filter_text
from bot.mock_data import ACCOUNTS, get_account
from bot.outcome import Outcome, validate_outcome
from bot.prompts import V1_PROMPT_VERBATIM, build_system_prompt
from bot.session import CallSession

TODAY = date(2026, 11, 18)


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- mock data
def test_late_fee_threshold_is_strict():
    assert get_account("MB-CC-10042").late_fee_inr == 750  # 48,200
    assert get_account("MB-CC-10077").late_fee_inr == 0  # 8,400
    assert get_account("MB-CC-10201").late_fee_inr == 0  # exactly 10,000 -> no fee


def test_summary_facts_come_from_code():
    s = get_account("MB-CC-10042").summary(TODAY)
    assert s["interest_rate_monthly_pct"] == 3.5
    assert s["due_date"] == (TODAY - timedelta(days=12)).isoformat()


# ----------------------------------------------------------------- prompts
def test_v1_prompt_is_verbatim_plus_account():
    p = build_system_prompt("v1", get_account("MB-CC-10042"))
    assert p.startswith(V1_PROMPT_VERBATIM)
    assert "48,200" in p


def test_prompts_carry_todays_date():
    for v in ("v1", "v2"):
        p = build_system_prompt(v, get_account("MB-CC-10042"), today=TODAY)
        assert "Wednesday, 18 November 2026" in p and "2026-11-18" in p


def test_v2_prompt_has_brief_facts_and_nevers():
    p = build_system_prompt("v2", get_account("MB-CC-10042"))
    for must in ["three point five percent", "seven hundred and fifty", "two working days", "CIBIL", "legal", "I'm only here for your pending payment"]:
        assert must in p
    assert "12%" not in p and "1500" not in p and "festive" not in p


# ----------------------------------------------------------------- output filter
@pytest.mark.parametrize(
    "sentence,rule",
    [
        ("Non-payment will affect your credit score badly.", "R8"),
        ("Your CIBIL score will drop.", "R8"),
        ("This may lead to legal action.", "R9"),
        ("We will send a legal notice.", "R9"),
        ("I can waive the late fee for you.", "R3"),
        ("I can offer you a 50% discount.", "R3"),
        ("The interest rate is around 12%.", "R5"),
        ("Interest is twelve percent per month.", "R5"),
        ("The late fee is 1500 rupees.", "R6"),
        ("There is a late fee of ₹1,500 on your account.", "R6"),
        ("We have a festive loan at 9.5%.", "R10"),
        ("Would you like our pre-approved personal loan?", "R10"),
        ("आपका बैलेंस बारह हज़ार है।", "R1"),
        ("Aap kal payment kar dijiye please.", "R1"),
    ],
)
def test_filter_blocks(sentence, rule):
    v = check_sentence(sentence)
    assert v is not None and v.rule == rule, (sentence, v)


@pytest.mark.parametrize(
    "sentence",
    [
        "Hello, this is Priya calling from Mumbai Bank.",
        "The interest on the outstanding balance is 3.5% per month.",
        "Interest is three point five percent per month.",
        "A late fee of ₹750 applies because the balance is over ten thousand rupees.",
        "A late fee of seven hundred and fifty rupees applies.",
        "No late fee applies on your account.",
        "I've noted your waiver request and a human agent will call you back.",
        "I'm only here for your pending payment. For other queries, please visit a branch or call the helpline.",
        "So that's twelve thousand five hundred rupees by Friday the 21st, correct?",
        "Thank you, Rahul. It will reflect within two working days.",
        "Am I speaking with Mohammed Irfan?",
    ],
)
def test_filter_allows(sentence):
    assert check_sentence(sentence) is None, sentence


def test_filter_text_replaces_only_bad_sentences():
    text = "Thanks for confirming. Non-payment will hurt your credit score. Can you pay by Friday?"
    out, viol = filter_text(text, "FALLBACK")
    assert [v.rule for v in viol] == ["R8"]
    assert out == "Thanks for confirming. FALLBACK Can you pay by Friday?"


# ----------------------------------------------------------------- input triggers
@pytest.mark.parametrize(
    "text,reason",
    [
        ("My father is in the hospital, I can't deal with this now", "medical_emergency"),
        ("mere papa ICU mein admitted hain", "medical_emergency"),
        ("I lost my job last month", "job_loss"),
        ("meri naukri chali gayi", "job_loss"),
        ("company ne nikaal diya mujhe", "job_loss"),
        ("you people are bloody idiots, stop calling", "abusive"),
        ("chup kar aur phone rakh", "abusive"),
        ("bakwaas band karo", "abusive"),
        ("just don't call me yaar, I don't want to talk to a bot", "human_requested"),
        ("can I speak to a real person please", "human_requested"),
        ("mujhe kisi insaan se baat karni hai", "human_requested"),
        ("transfer me to the branch", "human_requested"),
    ],
)
def test_triggers(text, reason):
    t = detect_trigger(text)
    assert t is not None and t.reason.value == reason, (text, t)


@pytest.mark.parametrize("text", ["I already paid yesterday via UPI", "maine kal pay kar diya", "can you call me tomorrow at 6", "mujhe naukri mil gayi, next week pay karunga", "I am busy right now, don't call me", "are you a bot? okay fine, I'll pay on Friday"])
def test_no_false_triggers(text):
    assert detect_trigger(text) is None, text


def test_language_classifier():
    assert classify_language(["yes this is Rahul", "I paid on Monday"]) == "english"
    assert classify_language(["haan ji, maine kal UPI se pay kar diya hai"]) in ("hinglish", "hindi")
    assert classify_language(["मैंने कल भुगतान कर दिया"]) == "hindi"


# ----------------------------------------------------------------- outcome schema
def test_ptp_requires_date_and_amount_within_30_days():
    base = {"call_id": "c", "customer_id": "MB-CC-10042", "prompt_version": "v3", "outcome": "promise_to_pay"}
    oc, err = validate_outcome(base, today=TODAY)
    assert oc is None and "ptp_date" in err
    oc, err = validate_outcome({**base, "ptp_date": "2026-11-21", "ptp_amount_inr": 12500}, today=TODAY)
    assert err is None and oc.outcome == Outcome.PROMISE_TO_PAY
    oc, err = validate_outcome({**base, "ptp_date": "2027-01-30", "ptp_amount_inr": 12500}, today=TODAY)
    assert oc is None and "30 days" in err


def test_lenient_mode_coerces_to_unresolved_callback():
    raw = {"call_id": "c", "customer_id": "MB-CC-10042", "prompt_version": "v1", "outcome": "promise_to_pay"}
    oc, err = validate_outcome(raw, today=TODAY, strict=False)
    assert oc is not None and oc.outcome == Outcome.CALLBACK_REQUEST and err


# ----------------------------------------------------------------- session + tools
def make_session(version: str, customer="MB-CC-10042") -> CallSession:
    s = CallSession(get_settings(prompt_version=version, customer_id=customer, webhook_url=""))
    s.today = TODAY
    return s


def test_v3_gates_balance_behind_identity_and_otp():
    s = make_session("v3")
    r = run(s.handle_tool("get_account_summary", {}))
    assert r["error"] == "identity_not_confirmed"
    run(s.handle_tool("confirm_identity", {"confirmed": True}))
    r = run(s.handle_tool("get_account_summary", {}))
    assert r["error"] == "otp_not_verified"
    r = run(s.handle_tool("verify_otp", {"otp": "0000"}))
    assert r["verified"] is False and r["attempts_left"] == 1
    r = run(s.handle_tool("verify_otp", {"otp": "4-8-2-1"}))
    assert r["verified"] is True
    r = run(s.handle_tool("get_account_summary", {}))
    assert r["outstanding_inr"] == 48200 and r["late_fee_inr"] == 750


def test_v3_otp_locks_after_two_failures():
    s = make_session("v3")
    run(s.handle_tool("confirm_identity", {"confirmed": True}))
    run(s.handle_tool("verify_otp", {"otp": "1111"}))
    r = run(s.handle_tool("verify_otp", {"otp": "2222"}))
    assert r["verified"] is False and s.otp_locked
    assert run(s.handle_tool("get_account_summary", {}))["error"] == "otp_not_verified"


def test_v1_has_no_gates():
    s = make_session("v1")
    r = run(s.handle_tool("get_account_summary", {}))
    assert r["outstanding_inr"] == 48200


def test_record_outcome_strict_vs_lenient():
    s3 = make_session("v3")
    r = run(s3.handle_tool("record_outcome", {"outcome": "promise_to_pay", "customer_note": "x"}))
    assert r["accepted"] is False and s3.outcome is None
    r = run(s3.handle_tool("record_outcome", {"outcome": "promise_to_pay", "ptp_date": "2026-11-21", "ptp_amount_inr": 12500, "customer_note": "x"}))
    assert r["accepted"] is True and s3.outcome.outcome == Outcome.PROMISE_TO_PAY and s3.closing
    r = run(s3.handle_tool("record_outcome", {"outcome": "already_paid", "customer_note": "again"}))
    assert r["error"] == "outcome_already_recorded"

    s1 = make_session("v1")
    r = run(s1.handle_tool("record_outcome", {"outcome": "promise_to_pay", "customer_note": "x"}))
    assert r["accepted"] is True and s1.outcome.outcome == Outcome.CALLBACK_REQUEST  # coerced
    assert any(e["kind"] == "outcome_coerced" for e in s1.guardrail_events)


def test_forced_transfer_when_model_misses_trigger():
    s = make_session("v3")
    run(s.handle_tool("confirm_identity", {"confirmed": True}))
    s.log_customer("I just lost my job, there is no money")
    assert s.needs_forced_transfer()
    line = run(s.force_transfer())
    assert "colleague" in line and s.outcome.outcome == Outcome.DISPUTE_OR_TRANSFER
    assert s.outcome.transfer_reason.value == "job_loss"


def test_v2_logs_trigger_but_does_not_enforce():
    s = make_session("v2")
    s.log_customer("I lost my job")
    assert s.transfer_pending is None and not s.needs_forced_transfer()


def test_output_filter_enforced_only_in_v3():
    bad = "Your CIBIL score will suffer."
    assert make_session("v3").filter_bot_text(bad) != bad
    s2 = make_session("v2")
    assert s2.filter_bot_text(bad) == bad
    assert s2.guardrail_events and s2.guardrail_events[0]["enforced"] is False


def test_finalize_guarantees_outcome_and_saves(tmp_path):
    s = make_session("v3")
    oc = run(s.finalize())
    assert oc.outcome == Outcome.CALLBACK_REQUEST and oc.callback_reason.value == "unresolved"
    p = s.save(tmp_path)
    assert p.exists() and "guardrail_events" in p.read_text()


def test_unresolved_callback_can_be_replaced_by_a_real_outcome():
    s = make_session("v3")
    run(s.handle_tool("record_outcome", {"outcome": "callback_request", "callback_reason": "unresolved", "customer_note": "nudged"}))
    r = run(s.handle_tool("record_outcome", {"outcome": "promise_to_pay", "ptp_date": "2026-11-21", "ptp_amount_inr": 30000, "customer_note": "pays now"}))
    assert r["accepted"] is True and s.outcome.outcome == Outcome.PROMISE_TO_PAY
    assert any(e["kind"] == "outcome_replaced" for e in s.guardrail_events)
    r = run(s.handle_tool("record_outcome", {"outcome": "already_paid", "paid_date": "2026-11-17", "paid_mode": "upi", "customer_note": "x"}))
    assert r["error"] == "outcome_already_recorded"
