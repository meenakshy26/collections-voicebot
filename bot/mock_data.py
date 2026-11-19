"""Fictional Mumbai Bank card accounts used for build, eval and the live demo.

All names, numbers and OTP codes are invented. Derived facts (late fee,
interest, due date) are computed here, never by the language model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

INTEREST_RATE_MONTHLY_PCT = 3.5
LATE_FEE_INR = 750
LATE_FEE_THRESHOLD_INR = 10_000  # fee applies only when outstanding is strictly above this


@dataclass(frozen=True)
class Account:
    customer_id: str
    full_name: str
    outstanding_inr: int
    minimum_due_inr: int
    days_past_due: int
    otp: str
    registered_mobile_last4: str
    notes: str = ""

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0]

    @property
    def late_fee_inr(self) -> int:
        return LATE_FEE_INR if self.outstanding_inr > LATE_FEE_THRESHOLD_INR else 0

    def due_date(self, today: date | None = None) -> date:
        today = today or date.today()
        return today - timedelta(days=self.days_past_due)

    def summary(self, today: date | None = None) -> dict:
        """What get_account_summary returns after OTP verification."""
        return {
            "customer_name": self.full_name,
            "outstanding_inr": self.outstanding_inr,
            "minimum_due_inr": self.minimum_due_inr,
            "due_date": self.due_date(today).isoformat(),
            "days_past_due": self.days_past_due,
            "late_fee_inr": self.late_fee_inr,
            "late_fee_rule": f"₹{LATE_FEE_INR} applies only if outstanding balance is over ₹{LATE_FEE_THRESHOLD_INR:,}",
            "interest_rate_monthly_pct": INTEREST_RATE_MONTHLY_PCT,
        }


ACCOUNTS: dict[str, Account] = {
    a.customer_id: a
    for a in [
        Account("MB-CC-10042", "Rahul Mehta", 48_200, 12_500, 12, "4821", "7731", "Default happy-path persona"),
        Account("MB-CC-10077", "Priya Nair", 8_400, 2_100, 5, "7719", "2208", "Under threshold: no late fee"),
        Account("MB-CC-10113", "Mohammed Irfan", 124_000, 31_000, 28, "1130", "9054", "Large balance, near 30-day edge"),
        Account("MB-CC-10158", "Sunita Deshmukh", 15_600, 3_900, 1, "5801", "4417", "Day-1 case; may have just paid"),
        Account("MB-CC-10201", "Arjun Varghese", 10_000, 2_500, 19, "2010", "6690", "Boundary: exactly 10,000 means no fee"),
    ]
}


def get_account(customer_id: str) -> Account:
    try:
        return ACCOUNTS[customer_id]
    except KeyError as e:
        raise KeyError(f"Unknown customer_id {customer_id!r}; known: {list(ACCOUNTS)}") from e
