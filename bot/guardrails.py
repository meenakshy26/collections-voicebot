"""Code-level guardrails.

Two halves:

* Output filter — runs on every sentence the bot is about to speak. Blocks the
  things the brief says the bot must never say, whatever the model produced.
* Input triggers — runs on every customer transcript. Detects abuse, medical
  emergency and job loss in English and romanised Hindi, so a transfer happens
  even if the model misses the cue.

Both are deliberately simple (regex + word lists). They are a safety net under
the prompt, not a replacement for it, and every hit is logged so the eval can
separate "model behaved" from "guardrail caught it".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bot.mock_data import INTEREST_RATE_MONTHLY_PCT, LATE_FEE_INR
from bot.outcome import TransferReason

# --------------------------------------------------------------------------- #
# Output filter
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Violation:
    rule: str  # short id matching the spec's rule table
    detail: str


_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|percent|per cent)", re.I)
_PCT_WORDS_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|eighteen|twenty|thirty|forty|fifty)"
    r"(?:\s+point\s+\w+)?\s+percent\b",
    re.I,
)
_ALLOWED_PCT_WORDS = {"three point five percent"}

_LATE_FEE_SENTENCE_RE = re.compile(r"late\s*fee|late\s*payment\s*(?:fee|charge)|penalty", re.I)
# Amount must sit next to the fee words, so "late fee of ₹750 if balance is over ₹10,000" passes.
_LATE_FEE_FIGURE_RES = [
    re.compile(r"(?:late\s*fee|late\s*payment\s*(?:fee|charge)|penalty)[^.,;]{0,30}?(?:₹|rs\.?|inr|rupees?)\s?(\d[\d,]*)", re.I),
    re.compile(r"(?:late\s*fee|late\s*payment\s*(?:fee|charge)|penalty)[^.,;]{0,30}?(\d[\d,]*)\s?(?:rupees?|rs\.?|inr)", re.I),
    re.compile(r"(?:₹|rs\.?|inr)\s?(\d[\d,]*)[^.,;]{0,20}?(?:late\s*fee|penalty)", re.I),
    re.compile(r"(\d[\d,]*)\s?rupees?[^.,;]{0,20}?(?:late\s*fee|penalty)", re.I),
]
_LATE_FEE_WORDS_RE = re.compile(
    r"(?:late\s*fee|late\s*payment\s*(?:fee|charge)|penalty)\s+(?:of|is|will be|would be|comes to)\s+((?:[a-z-]+\s+){1,7}?)rupees", re.I
)
_LATE_FEE_OK_WORDS = {"seven hundred and fifty", "seven hundred fifty"}

_WAIVER_AFFIRMATIVE_RES = [
    re.compile(r"\b(i|we)\s+(can|could|will|'ll|am able to|are able to|would)\s+(waive|reduce|discount|lower|cut|settle|offer)", re.I),
    re.compile(r"\b(waiver|discount|reduction|settlement)\s+(is|has been|will be|can be)\s+(approved|granted|applied|offered|arranged)", re.I),
    re.compile(r"\b\d{1,3}\s?(%|percent)\s+(waiver|discount|off)", re.I),
    re.compile(r"\b(special|festive|one[- ]time)\s+(offer|discount|deal)", re.I),
    re.compile(r"\bdiscount\b", re.I),
    re.compile(r"\bwaive\s+(the|your|this)\b", re.I),
]
_CIBIL_RE = re.compile(r"\bcibil\b|\bcredit\s*(score|rating|bureau|history|report)\b|\bbureau\b|\bexperian\b|\bequifax\b", re.I)
_LEGAL_RE = re.compile(
    r"\blegal\b|\bcourt\b|\blawsuit\b|\blawyer\b|\badvocate\b|\bsue\b|\bsummons\b|\blegal\s*notice\b|\bpolice\b|"
    r"\brecovery\s*agent|\bfir\b|\bcriminal\b|\bprosecut",
    re.I,
)
_PRODUCT_PITCH_RE = re.compile(
    r"\b(festive|personal|home|car|gold)\s+loan\b|\bloan\s+offer|\bfixed\s+deposit|\bfd\b|\binsurance\s+(plan|policy)|\bemi\s+conversion|\bpre-?approved",
    re.I,
)
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
_HINDI_WORDS = {
    "aap", "aapka", "aapke", "aapki", "hai", "hain", "kya", "nahi", "nahin", "haan", "ji", "kripya", "dhanyavaad",
    "shukriya", "paisa", "paise", "karna", "karo", "kijiye", "bataiye", "bhugtan", "kal", "aaj", "abhi", "theek",
    "thik", "accha", "achha", "namaste", "namaskar", "mera", "meri", "mere", "hum", "tum", "kyun", "kaise", "kab",
}

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _pct_values(text: str) -> list[float]:
    vals = [float(m.group(1)) for m in _PCT_RE.finditer(text)]
    for m in _PCT_WORDS_RE.finditer(text):
        phrase = m.group(0).lower().strip()
        if phrase not in _ALLOWED_PCT_WORDS:
            vals.append(-1.0)  # any spelled-out percentage other than the allowed one
    return vals


def check_sentence(text: str) -> Violation | None:
    """Return the first rule a sentence violates, or None."""
    t = text.strip()
    if not t:
        return None

    if _DEVANAGARI_RE.search(t):
        return Violation("R1", "Devanagari script in bot output")
    words = re.findall(r"[a-zA-Z']+", t.lower())
    if len(words) >= 4:
        hindi = sum(1 for w in words if w in _HINDI_WORDS)
        if hindi / len(words) > 0.2:
            return Violation("R1", f"romanised Hindi ratio {hindi}/{len(words)}")

    for rx in _WAIVER_AFFIRMATIVE_RES:
        if rx.search(t):
            return Violation("R3", f"waiver/discount language: {rx.pattern[:40]}")

    if _CIBIL_RE.search(t):
        return Violation("R8", "credit score / bureau mentioned")
    if _LEGAL_RE.search(t):
        return Violation("R9", "legal / court / police language")
    if _PRODUCT_PITCH_RE.search(t):
        return Violation("R10", "product pitch language")
    for v in _pct_values(t):
        if v != INTEREST_RATE_MONTHLY_PCT:
            return Violation("R5", f"percentage other than {INTEREST_RATE_MONTHLY_PCT}% spoken: {v if v>=0 else 'spelled out'}")

    if _LATE_FEE_SENTENCE_RE.search(t):
        for rx in _LATE_FEE_FIGURE_RES:
            for m in rx.finditer(t):
                try:
                    amt = int(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                if amt != LATE_FEE_INR:
                    return Violation("R6", f"late fee stated as ₹{amt}")
        for m in _LATE_FEE_WORDS_RE.finditer(t):
            phrase = " ".join(m.group(1).lower().split())
            if phrase not in _LATE_FEE_OK_WORDS:
                return Violation("R6", f"late fee stated in words as '{phrase}'")

    return None


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s]


def filter_text(text: str, fallback: str) -> tuple[str, list[Violation]]:
    """Check every sentence; replace violating ones with the fallback line."""
    out, violations = [], []
    for s in split_sentences(text):
        v = check_sentence(s)
        if v:
            violations.append(v)
            if fallback not in out:
                out.append(fallback)
        else:
            out.append(s)
    return " ".join(out), violations


# --------------------------------------------------------------------------- #
# Input triggers
# --------------------------------------------------------------------------- #

_ABUSE_TERMS = [
    # English
    r"\bf+u+c*k+", r"\bshit\b", r"\bbastard\b", r"\bbloody\s+(idiot|fool)", r"\bidiot\b", r"\bstupid\s+(bot|bank|people)",
    r"\bshut\s*up\b", r"\bscam(mer)?s?\b", r"\bfraud\s+bank\b", r"\bharass", r"\bgo\s+to\s+hell\b", r"\bnonsense\b",
    # Romanised Hindi
    r"\bbhench?od", r"\bmadarch?od", r"\bchutiy", r"\bsaal[ae]\b", r"\bkutt[ae]\b", r"\bharam", r"\bgand\w*\b",
    r"\bbakwa+s\b", r"\bchup\s*(kar|raho|ho)", r"\bbewakoof", r"\bpagal\b", r"\bbadtameez",
]
_MEDICAL_TERMS = [
    r"\bhospital", r"\bicu\b", r"\bsurgery\b", r"\boperation\b", r"\baccident\b", r"\bemergency\b", r"\bcancer\b",
    r"\bchemo", r"\bheart\s*attack", r"\bstroke\b", r"\badmitted\b", r"\bdialysis\b",
    r"\bcritical\s+condition", r"\bdoctor\s+(said|says)", r"\bmedical\b",
    r"\bbimaa?r\b", r"\bbeemaa?r\b", r"\btabiyat\b", r"\bilaa?j\b", r"\baspataa?l\b", r"\bdawaa?i\b", r"\boperation\s+hua",
]
_BEREAVEMENT_TERMS = [
    r"\bpassed\s+away\b", r"\bdied\b", r"\bdeath\b", r"\bfuneral\b", r"\bno\s+more\b", r"\bexpired\s+(last|on|in|yesterday)",
    r"\bguzar\s+ga", r"\bexpire\s+ho\s+ga", r"\bmaut\b", r"\bdehant\b", r"\bshraddh", r"\bnahi\s+rah[ae]\b",
]
_JOB_LOSS_TERMS = [
    r"\blost\s+my\s+job", r"\blaid\s+off", r"\blayoff", r"\bfired\b", r"\bterminated\b", r"\bretrench", r"\bjobless\b",
    r"\bunemployed\b", r"\bno\s+job\b", r"\bno\s+income\b", r"\bcompany\s+(shut|closed)", r"\bbusiness\s+(closed|shut|failed)",
    r"\bsalary\s+(nahi|nahin|stopped|not\s+(come|coming|paid|credited))",
    r"\bnaukri\s+(chali|gayi|gai|nahi|nahin|chhoot|chut|khatam|gaya|gayi)", r"\bjob\s+(chali|chala|gayi|gai|gaya|nahi|nahin|chhoot|chut|khatam)",
    r"\bnikaa?l\s+diya", r"\bnikaa?l\s+di", r"\bkaam\s+(nahi|nahin|chhoot|chut|band)", r"\bberozgaa?r\b", r"\bchhatni\b",
]

_HUMAN_REQUEST_TERMS = [
    r"\b(don'?t|do not|dont) want to (talk|speak) (to|with) (a |the )?(bot|machine|computer|recording|ai|robot)",
    r"\b(talk|speak) (to|with) (a |an |some )?(real|actual) (person|human|agent|executive)",
    r"\b(talk|speak) (to|with) (a |an |the )?(human|person|agent|executive|manager|someone)\b",
    r"\b(connect|transfer) me to (a |an |the )?(human|person|agent|executive|branch|manager)",
    r"\b(are you|is this) (a |an )?(bot|robot|machine|recording|ai)\b.*\b(don'?t|not)\b",
    r"\bbot se baat nahi", r"\b(insaan|aadmi|banda|agent|executive) se baat", r"\bkisi (insaan|aadmi|agent) ko (do|dijiye)",
    r"\bmachine se baat", r"\bcomputer se baat",
]
_TRIGGERS: list[tuple[TransferReason, list[re.Pattern]]] = [
    (TransferReason.ABUSIVE, [re.compile(p, re.I) for p in _ABUSE_TERMS]),
    (TransferReason.MEDICAL_EMERGENCY, [re.compile(p, re.I) for p in _MEDICAL_TERMS]),
    (TransferReason.BEREAVEMENT, [re.compile(p, re.I) for p in _BEREAVEMENT_TERMS]),
    (TransferReason.JOB_LOSS, [re.compile(p, re.I) for p in _JOB_LOSS_TERMS]),
    (TransferReason.HUMAN_REQUESTED, [re.compile(p, re.I) for p in _HUMAN_REQUEST_TERMS]),
]


@dataclass(frozen=True)
class Trigger:
    reason: TransferReason
    matched: str


def detect_trigger(customer_text: str) -> Trigger | None:
    """Return the first transfer trigger found in a customer utterance."""
    t = customer_text.lower()
    # Order matters: medical and job loss before abuse? No — abuse first, because an
    # abusive customer who also mentions a hospital still needs a human, and the
    # reason recorded is the one that stops the pitch fastest. Priority is a product
    # call; it is documented in the writeup.
    for reason, patterns in _TRIGGERS:
        for rx in patterns:
            m = rx.search(t)
            if m:
                return Trigger(reason, m.group(0))
    return None


def classify_language(customer_turns: list[str]) -> str:
    """Rough language-mix label for the outcome record (QA only)."""
    if not customer_turns:
        return "english"
    text = " ".join(customer_turns)
    if _DEVANAGARI_RE.search(text):
        return "hindi"
    words = re.findall(r"[a-zA-Z']+", text.lower())
    if not words:
        return "english"
    hindi = sum(1 for w in words if w in _HINDI_WORDS)
    ratio = hindi / len(words)
    if ratio > 0.5:
        return "hindi"
    if ratio > 0.1:
        return "hinglish"
    return "english"
