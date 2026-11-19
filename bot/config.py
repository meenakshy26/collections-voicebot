"""Runtime configuration, loaded once from environment / .env.

Everything tunable lives here so the pipeline, the eval harness and the
scripts all read the same values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

PROMPT_VERSIONS = ("v1", "v2", "v3")


@dataclass(frozen=True)
class GuardrailConfig:
    """Which code-level guardrails are switched on.

    v1 and v2 run prompt-only so the eval can attribute improvement to the
    prompt versus the code. v3 switches everything on.
    """

    otp_gate: bool = False  # get_account_summary refuses before OTP
    identity_gate: bool = False  # all account tools refuse before identity confirmed
    output_filter: bool = False  # sentence-level filter before TTS
    input_triggers: bool = False  # abuse / medical / job-loss forced transfer
    outcome_validation: bool = False  # strict schema on record_outcome
    turn_budget: bool = False  # nudge to close after N bot turns
    account_in_prompt: bool = True  # v1 only: account facts injected into the prompt


GUARDRAILS_BY_VERSION: dict[str, GuardrailConfig] = {
    "v1": GuardrailConfig(account_in_prompt=True),
    "v2": GuardrailConfig(account_in_prompt=False),
    "v3": GuardrailConfig(
        otp_gate=True,
        identity_gate=True,
        output_filter=True,
        input_triggers=True,
        outcome_validation=True,
        turn_budget=True,
        account_in_prompt=False,
    ),
}


@dataclass
class Settings:
    prompt_version: str = field(default_factory=lambda: os.getenv("PROMPT_VERSION", "v3"))
    customer_id: str = field(default_factory=lambda: os.getenv("CUSTOMER_ID", "MB-CC-10042"))

    # Providers
    sarvam_api_key: str = field(default_factory=lambda: os.getenv("SARVAM_API_KEY", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""))
    deepgram_api_key: str = field(default_factory=lambda: os.getenv("DEEPGRAM_API_KEY", ""))
    webhook_url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL", ""))

    # Model choices (interview question #1: "why this model?")
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-haiku-4-5"))
    judge_model: str = field(default_factory=lambda: os.getenv("JUDGE_MODEL", "claude-sonnet-5-5"))
    customer_model: str = field(default_factory=lambda: os.getenv("CUSTOMER_MODEL", "claude-haiku-5-5"))  # simulated customer in the eval
    stt_language: str = field(default_factory=lambda: os.getenv("STT_LANGUAGE", "hi-IN"))
    stt_mode: str = field(default_factory=lambda: os.getenv("STT_MODE", "translit"))
    tts_model: str = field(default_factory=lambda: os.getenv("TTS_MODEL", "bulbul:v4-flash"))
    tts_voice: str = field(default_factory=lambda: os.getenv("TTS_VOICE", "simran_en_customer"))

    # Behaviour
    bot_name: str = "Priya"
    max_bot_turns: int = int(os.getenv("MAX_BOT_TURNS", "12"))  # spoken turns before the close nudge
    nudge_after_seconds: int = int(os.getenv("NUDGE_AFTER_SECONDS", "150"))
    max_call_seconds: int = int(os.getenv("MAX_CALL_SECONDS", "300"))
    otp_max_attempts: int = 2

    @property
    def guardrails(self) -> GuardrailConfig:
        if self.prompt_version not in GUARDRAILS_BY_VERSION:
            raise ValueError(f"Unknown PROMPT_VERSION {self.prompt_version!r}; use one of {PROMPT_VERSIONS}")
        return GUARDRAILS_BY_VERSION[self.prompt_version]


def get_settings(**overrides) -> Settings:
    s = Settings()
    for k, v in overrides.items():
        if v is not None:
            setattr(s, k, v)
    return s
