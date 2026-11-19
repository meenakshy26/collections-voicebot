"""Structured call outcome: the one object the CRM receives per call."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError, ValidationInfo, model_validator


class Outcome(StrEnum):
    PROMISE_TO_PAY = "promise_to_pay"
    ALREADY_PAID = "already_paid"
    CALLBACK_REQUEST = "callback_request"
    DISPUTE_OR_TRANSFER = "dispute_or_transfer"


class PaidMode(StrEnum):
    UPI = "upi"
    NET_BANKING = "net_banking"
    AUTO_DEBIT = "auto_debit"
    CHEQUE = "cheque"
    BRANCH_CASH = "branch_cash"
    OTHER = "other"


class CallbackReason(StrEnum):
    CUSTOMER_BUSY = "customer_busy"
    WAIVER_REQUEST = "waiver_request"
    SETTLEMENT_REQUEST = "settlement_request"
    THIRD_PARTY_ANSWERED = "third_party_answered"
    WRONG_NUMBER = "wrong_number"
    DO_NOT_CALL = "do_not_call"
    OTP_FAILED = "otp_failed"
    UNRESOLVED = "unresolved"


class TransferReason(StrEnum):
    ABUSIVE = "abusive"
    MEDICAL_EMERGENCY = "medical_emergency"
    JOB_LOSS = "job_loss"
    BEREAVEMENT = "bereavement"
    HUMAN_REQUESTED = "human_requested"
    FRAUD_CLAIM = "fraud_claim"
    BILLING_DISPUTE = "billing_dispute"


class LanguageMix(StrEnum):
    ENGLISH = "english"
    HINGLISH = "hinglish"
    HINDI = "hindi"


class CallOutcome(BaseModel):
    """Validated outcome record. Field requirements depend on `outcome`."""

    call_id: str
    customer_id: str
    outcome: Outcome
    prompt_version: str

    ptp_date: date | None = None
    ptp_amount_inr: int | None = Field(default=None, gt=0)

    paid_date: date | None = None
    paid_mode: PaidMode | None = None

    callback_time: datetime | None = None
    callback_reason: CallbackReason | None = None

    transfer_reason: TransferReason | None = None

    customer_note: str = ""
    otp_verified: bool = False
    language_mix: LanguageMix = LanguageMix.ENGLISH
    duration_sec: int = 0
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _check_required_by_type(self, info: ValidationInfo) -> "CallOutcome":
        ctx = info.context or {}
        today = ctx.get("today") or date.today()
        o = self.outcome
        if o == Outcome.PROMISE_TO_PAY:
            if self.ptp_date is None or self.ptp_amount_inr is None:
                raise ValueError("promise_to_pay needs ptp_date and ptp_amount_inr")
            if self.ptp_date < today:
                raise ValueError(f"ptp_date {self.ptp_date} is in the past")
            if self.ptp_date > today + timedelta(days=30):
                raise ValueError(f"ptp_date {self.ptp_date} is more than 30 days out")
        elif o == Outcome.ALREADY_PAID:
            if self.paid_date is None or self.paid_mode is None:
                raise ValueError("already_paid needs paid_date and paid_mode")
            if self.paid_date > today:
                raise ValueError("paid_date cannot be in the future")
        elif o == Outcome.CALLBACK_REQUEST:
            if self.callback_reason is None:
                raise ValueError("callback_request needs callback_reason")
            if self.callback_reason == CallbackReason.CUSTOMER_BUSY and self.callback_time is None:
                raise ValueError("callback_request with reason customer_busy needs callback_time")
        elif o == Outcome.DISPUTE_OR_TRANSFER:
            if self.transfer_reason is None:
                raise ValueError("dispute_or_transfer needs transfer_reason")
        return self

    def to_payload(self) -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude_none=True)
        return d


def validate_outcome(raw: dict[str, Any], *, today: date | None = None, strict: bool = True) -> tuple[CallOutcome | None, str | None]:
    """Build a CallOutcome from the model's tool arguments.

    Returns (outcome, error). With strict=False (v1/v2) a schema failure still
    produces a best-effort record so the webhook fires; the error is logged.
    """
    data = dict(raw)
    try:
        oc = CallOutcome.model_validate(data, context={"today": today})
        return oc, None
    except (ValidationError, ValueError) as e:
        if strict:
            return None, _short_error(e)
        # Lenient: coerce to a callback/unresolved so something reaches the CRM.
        fallback = {
            "call_id": data.get("call_id", "unknown"),
            "customer_id": data.get("customer_id", "unknown"),
            "outcome": Outcome.CALLBACK_REQUEST,
            "callback_reason": CallbackReason.UNRESOLVED,
            "prompt_version": data.get("prompt_version", "unknown"),
            "customer_note": f"schema failure: {_short_error(e)} | model sent: {raw}",
        }
        return CallOutcome.model_validate(fallback, context={"today": today}), _short_error(e)


def _short_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        parts = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            parts.append(f"{loc}: {err.get('msg')}")
        return "; ".join(parts)[:400]
    return str(e)[:400]
