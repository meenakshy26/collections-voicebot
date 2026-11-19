"""Per-call state and the tool handlers the model can invoke.

This module is pure Python: no Pipecat, no provider SDKs. The voice pipeline
and the text eval harness both drive the same CallSession, which is what makes
the eval numbers representative of the real bot.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from bot.config import REPO_ROOT, Settings
from bot.guardrails import Trigger, Violation, classify_language, detect_trigger, filter_text
from bot.mock_data import Account, get_account
from bot.outcome import CallOutcome, CallbackReason, Outcome, TransferReason, validate_outcome
from bot.prompts import FORCED_TRANSFER_LINE, GUARDRAIL_FALLBACK_LINE, build_system_prompt
from bot.webhook import post_outcome

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Tool schemas (provider-neutral JSON schema). Pipecat and the harness convert.
# --------------------------------------------------------------------------- #

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "confirm_identity",
        "description": "Record whether the person on the call is the card holder. Call once, as soon as you know.",
        "parameters": {
            "type": "object",
            "properties": {"confirmed": {"type": "boolean", "description": "true if speaking with the card holder"}},
            "required": ["confirmed"],
        },
    },
    {
        "name": "verify_otp",
        "description": "Verify the 4-digit one-time code the customer reads out. Required before any balance can be shared.",
        "parameters": {
            "type": "object",
            "properties": {"otp": {"type": "string", "description": "the digits the customer said, e.g. 4821"}},
            "required": ["otp"],
        },
    },
    {
        "name": "get_account_summary",
        "description": "Fetch outstanding balance, minimum due, due date, late fee and interest rate for this customer.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "request_transfer",
        "description": "Hand the call to a human. Use for abuse, medical emergency, job loss, bereavement, a customer asking for a human/agent/branch, fraud claims or billing disputes. Ends the payment conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": [r.value for r in TransferReason],
                    "description": "abusive = insults or swearing; medical_emergency = illness, hospital, ICU, accident, surgery (the person is alive); job_loss = lost job or income; bereavement = someone has died; human_requested = wants a person, not a bot; fraud_claim = says the transactions are not theirs; billing_dispute = disputes a charge or fee",
                },
                "note": {"type": "string", "description": "one sentence on what the customer said"},
            },
            "required": ["reason", "note"],
        },
    },
    {
        "name": "record_outcome",
        "description": "Record the final outcome of the call. Call exactly once, right before saying goodbye.",
        "parameters": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": [o.value for o in Outcome]},
                "ptp_date": {"type": "string", "description": "YYYY-MM-DD, for promise_to_pay"},
                "ptp_amount_inr": {"type": "integer", "description": "rupees, for promise_to_pay"},
                "paid_date": {"type": "string", "description": "YYYY-MM-DD, for already_paid"},
                "paid_mode": {"type": "string", "enum": ["upi", "net_banking", "auto_debit", "cheque", "branch_cash", "other"]},
                "callback_time": {"type": "string", "description": "ISO 8601 with +05:30, for callback_request when a time was agreed"},
                "callback_reason": {"type": "string", "enum": [r.value for r in CallbackReason]},
                "transfer_reason": {"type": "string", "enum": [r.value for r in TransferReason]},
                "customer_note": {"type": "string", "description": "one sentence summarising what the customer said"},
            },
            "required": ["outcome", "customer_note"],
        },
    },
]


# Spoken digits as speech-to-text tends to hear them: English and Hindi number words,
# plus common mishearings ("A" for eight, "to" for two, "for" for four, "oh" for zero).
_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "shunya": "0", "sifar": "0",
    "one": "1", "won": "1", "ek": "1", "ik": "1",
    "two": "2", "to": "2", "too": "2", "do": "2",
    "three": "3", "tree": "3", "free": "3", "teen": "3", "tin": "3",
    "four": "4", "for": "4", "fore": "4", "chaar": "4", "char": "4", "chaar.": "4",
    "five": "5", "paanch": "5", "panch": "5", "pach": "5",
    "six": "6", "chhe": "6", "che": "6", "cheh": "6", "chhah": "6",
    "seven": "7", "saat": "7", "sat": "7",
    "eight": "8", "ate": "8", "a": "8", "eh": "8", "aath": "8", "aat": "8", "ath": "8",
    "nine": "9", "nau": "9", "no": "9", "nein": "9",
}


def normalise_otp(heard: str) -> str:
    """Turn whatever the model passed on ('4A21', 'four eight two one', 'chaar aath do ek') into digits."""
    text = heard.lower().replace("-", " ").replace(",", " ")
    out: list[str] = []
    for token in text.split():
        if token.isdigit():
            out.append(token)
            continue
        if token in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[token])
            continue
        # mixed tokens like '4a21' or 'four8two1': walk character by character
        for ch in token:
            if ch.isdigit():
                out.append(ch)
            elif ch in _DIGIT_WORDS and len(token) <= 4:
                out.append(_DIGIT_WORDS[ch])
    return "".join(out)


@dataclass
class TranscriptTurn:
    role: str  # "bot" | "customer" | "tool" | "system"
    text: str
    t: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallSession:
    settings: Settings
    account: Account = field(init=False)
    call_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.time)
    today: date = field(default_factory=date.today)

    identity_confirmed: bool | None = None
    otp_verified: bool = False
    otp_attempts: int = 0
    otp_locked: bool = False
    bot_turns: int = 0
    transfer_pending: Trigger | None = None
    outcome: CallOutcome | None = None
    outcome_error: str | None = None
    webhook_result: dict[str, Any] | None = None
    closing: bool = False
    closing_response_done: bool = False
    end_after_speech: bool = False
    ended: bool = False
    channel: str = "voice"  # "voice" (live call) or "simulated" (eval harness)

    transcript: list[TranscriptTurn] = field(default_factory=list)
    guardrail_events: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.account = get_account(self.settings.customer_id)

    # ------------------------------------------------------------------ #
    # Prompt / tools
    # ------------------------------------------------------------------ #
    @property
    def version(self) -> str:
        return self.settings.prompt_version

    @property
    def g(self):
        return self.settings.guardrails

    def system_prompt(self) -> str:
        return build_system_prompt(self.version, self.account, self.settings.bot_name, self.today)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return TOOL_SCHEMAS

    # ------------------------------------------------------------------ #
    # Transcript
    # ------------------------------------------------------------------ #
    def _autosave(self) -> None:
        try:
            self.save()
        except Exception:  # noqa: BLE001
            pass

    def log_customer(self, text: str) -> Trigger | None:
        self.transcript.append(TranscriptTurn("customer", text))
        self._autosave()
        trig = detect_trigger(text)
        if trig and self.g.input_triggers and self.outcome is None:
            if self.transfer_pending is None:
                self.transfer_pending = trig
                self._event("input_trigger", rule="R4", reason=trig.reason.value, matched=trig.matched, text=text)
        return trig

    def log_bot(self, text: str, **meta: Any) -> None:
        self.transcript.append(TranscriptTurn("bot", text, meta=meta))
        self._autosave()

    def note_bot_turn_end(self) -> None:
        self.bot_turns += 1

    def customer_turns(self) -> list[str]:
        return [t.text for t in self.transcript if t.role == "customer"]

    def _event(self, kind: str, **data: Any) -> None:
        ev = {"kind": kind, "turn": self.bot_turns, "t": round(time.time() - self.started_at, 2), **data}
        self.guardrail_events.append(ev)
        log.info("guardrail %s", json.dumps(ev, default=str))

    # ------------------------------------------------------------------ #
    # Output filter
    # ------------------------------------------------------------------ #
    def filter_bot_text(self, text: str) -> str:
        """Apply the output filter when enabled; always log violations for the eval."""
        cleaned, violations = filter_text(text, GUARDRAIL_FALLBACK_LINE)
        for v in violations:
            self._event("output_violation", rule=v.rule, detail=v.detail, text=text, enforced=self.g.output_filter)
        return cleaned if self.g.output_filter else text

    # ------------------------------------------------------------------ #
    # Turn budget / forced transfer — called by the pipeline after each bot turn
    # ------------------------------------------------------------------ #
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def over_turn_budget(self) -> bool:
        return self.g.turn_budget and self.bot_turns >= self.settings.max_bot_turns and self.outcome is None

    def over_budget(self) -> bool:
        """Turn budget OR time budget (the brief wants 2–3 minute calls)."""
        return self.over_turn_budget() or (self.g.turn_budget and self.elapsed() >= self.settings.nudge_after_seconds and self.outcome is None)

    def needs_forced_transfer(self) -> bool:
        return self.g.input_triggers and self.transfer_pending is not None and self.outcome is None

    async def force_transfer(self) -> str:
        """The model missed a trigger; code records the transfer and returns the line to speak."""
        assert self.transfer_pending is not None
        self._event("forced_transfer", rule="R4", reason=self.transfer_pending.reason.value)
        await self._record(
            {
                "outcome": Outcome.DISPUTE_OR_TRANSFER.value,
                "transfer_reason": self.transfer_pending.reason.value,
                "customer_note": f"Guardrail-forced transfer; customer said: '{self.transfer_pending.matched}'",
            },
            forced=True,
        )
        self.closing = True
        return FORCED_TRANSFER_LINE

    # ------------------------------------------------------------------ #
    # Tool dispatch
    # ------------------------------------------------------------------ #
    async def handle_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        args = args or {}
        handler = {
            "confirm_identity": self._confirm_identity,
            "verify_otp": self._verify_otp,
            "get_account_summary": self._get_account_summary,
            "request_transfer": self._request_transfer,
            "record_outcome": self._record_outcome,
        }.get(name)
        if handler is None:
            result = {"error": f"unknown tool {name}"}
        else:
            try:
                result = await handler(args)
            except Exception as e:  # noqa: BLE001
                log.exception("tool %s failed", name)
                result = {"error": f"{type(e).__name__}: {e}"}
        self.tool_calls.append({"name": name, "args": args, "result": result, "turn": self.bot_turns})
        self.transcript.append(TranscriptTurn("tool", f"{name}({json.dumps(args)}) -> {json.dumps(result, default=str)[:300]}"))
        self._autosave()
        return result

    async def _confirm_identity(self, args: dict) -> dict:
        self.identity_confirmed = bool(args.get("confirmed"))
        if self.identity_confirmed:
            return {"ok": True, "first_name": self.account.first_name, "instruction": "Proceed. State that the minimum payment is past due and ask for two minutes."}
        return {
            "ok": True,
            "first_name": None,
            "instruction": "Not the card holder. If a family member, friend or colleague answered: say Mumbai Bank will call back and record_outcome as callback_request with callback_reason third_party_answered. If it is a wrong number or nobody by that name is there: apologise briefly, do NOT say the bank will call back, and record_outcome as callback_request with callback_reason wrong_number. Then end. Reveal nothing.",
        }

    def _gate(self) -> dict | None:
        if self.g.identity_gate and self.identity_confirmed is not True:
            self._event("tool_refused", rule="R12", tool="account", reason="identity_not_confirmed")
            return {"error": "identity_not_confirmed", "instruction": "Confirm you are speaking with the card holder first (ask by name, then call confirm_identity)."}
        return None

    async def _verify_otp(self, args: dict) -> dict:
        gate = self._gate()
        if gate:
            return gate
        if self.otp_locked:
            return {"verified": False, "attempts_left": 0, "instruction": "OTP locked. Continue without stating amounts; offer a callback."}
        self.otp_attempts += 1
        given = normalise_otp(str(args.get("otp", "")))
        if len(given) != 4:
            self.otp_attempts -= 1  # a garbled read-out is not a failed attempt
            self._event("otp_unparseable", rule="R7", heard=str(args.get("otp", "")))
            return {"verified": False, "attempts_left": self.settings.otp_max_attempts - self.otp_attempts, "instruction": f"That did not sound like four digits (heard '{args.get('otp', '')}'). Ask the customer to say the code one digit at a time."}
        if given == self.account.otp:
            self.otp_verified = True
            return {"verified": True, "attempts_left": self.settings.otp_max_attempts - self.otp_attempts, "instruction": "Verified. You may now call get_account_summary."}
        left = self.settings.otp_max_attempts - self.otp_attempts
        if left <= 0:
            self.otp_locked = True
            self._event("otp_locked", rule="R7")
            return {"verified": False, "attempts_left": 0, "instruction": "Verification failed twice. Do not state any amounts. Offer a callback and record callback_request with reason otp_failed, or continue to a promise without amounts."}
        return {"verified": False, "attempts_left": left, "instruction": "That code did not match. Ask the customer to read it once more."}

    async def _get_account_summary(self, args: dict) -> dict:
        gate = self._gate()
        if gate:
            return gate
        if self.g.otp_gate and not self.otp_verified:
            self._event("tool_refused", rule="R7", tool="get_account_summary", reason="otp_not_verified")
            return {"error": "otp_not_verified", "instruction": "You must verify the customer with verify_otp before any balance can be shared. Tell them a code has been sent to their registered mobile and ask them to read it out."}
        return self.account.summary(self.today)

    async def _request_transfer(self, args: dict) -> dict:
        reason = args.get("reason")
        try:
            TransferReason(reason)
        except ValueError:
            return {"error": f"invalid reason {reason!r}", "valid": [r.value for r in TransferReason]}
        res = await self._record(
            {"outcome": Outcome.DISPUTE_OR_TRANSFER.value, "transfer_reason": reason, "customer_note": str(args.get("note", ""))[:300]},
            forced=False,
        )
        self.closing = True
        return {**res, "instruction": "Say one empathetic sentence and that a colleague will take over. Then say goodbye. Do not continue the payment conversation."}

    async def _record_outcome(self, args: dict) -> dict:
        if self.outcome is not None:
            replaceable = self.outcome.outcome == Outcome.CALLBACK_REQUEST and self.outcome.callback_reason == CallbackReason.UNRESOLVED
            if not replaceable:
                return {"accepted": False, "error": "outcome_already_recorded", "instruction": "The outcome is already recorded. Say goodbye and end the call."}
            self._event("outcome_replaced", rule="R11", previous=self.outcome.outcome.value)
            self.outcome = None
        res = await self._record(args, forced=False)
        if res.get("accepted"):
            self.closing = True
            if str(args.get("callback_reason", "")) in ("third_party_answered", "wrong_number", "do_not_call"):
                self.end_after_speech = True  # the scripted line is the whole close; hang up once it has played
            if str(args.get("callback_reason", "")) in ("waiver_request", "settlement_request"):
                res["instruction"] = "Recorded. Do NOT say goodbye yet. Continue: ask for the minimum payment as instructed, and close only after that."
            else:
                res["instruction"] = "Recorded. If you have already said goodbye in this turn, reply with nothing at all. Otherwise confirm the outcome in one sentence and end with the word goodbye, once."
        return res

    async def _record(self, args: dict, *, forced: bool) -> dict:
        raw = {
            **args,
            "call_id": self.call_id,
            "customer_id": self.account.customer_id,
            "prompt_version": self.version,
            "otp_verified": self.otp_verified,
            "language_mix": classify_language(self.customer_turns()),
            "duration_sec": int(time.time() - self.started_at),
        }
        oc, err = validate_outcome(raw, today=self.today, strict=self.g.outcome_validation)
        if oc is None:
            self._event("outcome_rejected", rule="R11", error=err, args=args)
            return {"accepted": False, "error": err, "instruction": "Fix the fields and call record_outcome again."}
        if err:
            self._event("outcome_coerced", rule="R11", error=err, args=args)
        self.outcome = oc
        self.outcome_error = err
        self.webhook_result = await post_outcome(self.settings.webhook_url, oc.to_payload())
        self._event("outcome_recorded", rule="R11", outcome=oc.outcome.value, forced=forced, webhook=self.webhook_result.get("status"))
        return {"accepted": True, "outcome": oc.outcome.value, "webhook": self.webhook_result.get("status")}

    # ------------------------------------------------------------------ #
    # Close-out
    # ------------------------------------------------------------------ #
    async def finalize(self) -> CallOutcome:
        """Guarantee an outcome exists at hang-up (R11), then persist the transcript."""
        if self.outcome is None:
            self._event("outcome_missing_at_end", rule="R11")
            await self._record(
                {"outcome": Outcome.CALLBACK_REQUEST.value, "callback_reason": CallbackReason.UNRESOLVED.value, "customer_note": "Call ended without the model recording an outcome."},
                forced=True,
            )
        self.ended = True
        self.save()
        return self.outcome  # type: ignore[return-value]

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "customer_id": self.account.customer_id,
            "prompt_version": self.version,
            "llm_model": self.settings.llm_model,
            "started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(),
            "duration_sec": int(time.time() - self.started_at),
            "identity_confirmed": self.identity_confirmed,
            "otp_verified": self.otp_verified,
            "bot_turns": self.bot_turns,
            "outcome": self.outcome.to_payload() if self.outcome else None,
            "outcome_error": self.outcome_error,
            "webhook_result": self.webhook_result,
            "ended": self.ended,
            "closing": self.closing,
            "channel": self.channel,
            "transcript": [{"role": t.role, "text": t.text, "t": round(t.t - self.started_at, 2), **({"meta": t.meta} if t.meta else {})} for t in self.transcript],
            "guardrail_events": self.guardrail_events,
            "tool_calls": self.tool_calls,
        }

    def save(self, directory: Path | None = None) -> Path:
        directory = directory or Path(os.getenv("CALL_LOG_DIR") or (REPO_ROOT / "logs" / "calls"))
        directory.mkdir(parents=True, exist_ok=True)
        p = directory / f"{self.call_id}.json"
        p.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return p
