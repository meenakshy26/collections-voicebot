# Mumbai Bank Collections Voicebot

An outbound collections voice agent for credit-card customers 1–30 days past due, built for the GreyLabs AI PM take-home. It listens, speaks, holds a multi-turn conversation in English with Hinglish-speaking customers, enforces the bank's rules in code, posts a structured outcome to a webhook at the end of every call, and comes with an operations console that shows the call, compares prompt versions and queues hand-offs for humans.

```
Browser mic ──► Pipecat pipeline ──► webhook (CRM stand-in = the console)
                 ├─ STT  Sarvam Saaras v4 (realtime, Hinglish)   fallback: Deepgram Nova-3 multi
                 ├─ LLM  Claude Haiku 4.5 (five tools)
                 └─ TTS  Sarvam Bulbul v4 flash (Indian-English)  fallback: Deepgram Aura-2
```

**Results (final run, `eval/results/20261119-final/`):** v1 (the brief's prompt verbatim) passed 1 of 22 scenarios; v2 (rewritten prompt, no guardrails) 23 of 24; v3 (rewritten prompt + code guardrails) 24 of 24 on the delivered rubric (22 of 24 under the strict reading in writeup section 3: two minor adherence slips the judge filed as tone). The 18 Nov side-by-side run, all three versions judged on 22 scenarios, is in `eval/results/20261118-sidebyside/`. The writeup PDF explains the three versions, the failure definition and what each step bought.

## Layout

| Path | What it is |
|---|---|
| `voice_bot.py` | The Pipecat voice pipeline: transport → STT → LLM → TTS, plus the call controller (turn/time budget, forced hand-off, end-of-call). |
| `console.py`, `console/index.html` | The operations console on :8787 and the webhook receiver. Dashboard · Live call · Compare versions · Cohort analytics · Work queue · CRM inbox. |
| `bot/prompts.py` | System prompts: **v1** (assignment prompt, verbatim) and **v2** (rewrite against the brief). v3 = the v2 prompt + the code guardrails below. |
| `bot/config.py` | Per-version guardrail switches (`GUARDRAILS_BY_VERSION`) and settings. |
| `bot/session.py` | Per-call state and the five tools the model can call (`confirm_identity`, `verify_otp`, `get_account_summary`, `request_transfer`, `record_outcome`). |
| `bot/guardrails.py` | Output filter (never say: wrong rates, CIBIL, legal, waivers, products, Hindi) and input triggers (abuse / medical / job loss / bereavement / "I want a human" → forced transfer). |
| `bot/outcome.py` | The structured outcome schema (promise_to_pay, already_paid, callback_request, dispute_or_transfer) and its validation. |
| `bot/mock_data.py` | Five fictional accounts; late fee and interest are computed here, never by the model. |
| `bot/webhook.py` | POSTs the outcome JSON to `WEBHOOK_URL`. |
| `eval/scenarios.yaml` | 24 scripted customer scenarios (easy / tricky / trap), each tagged with the failure case it probes (F1–F22) and whether it came from the brief or was inferred. |
| `eval/harness.py` | Text-level simulator: an LLM plays the customer against the **same** prompt, tools, guardrails and call controller the voice bot uses. |
| `eval/judge.py` | Scoring: deterministic checks + an LLM judge. Defines "failure" (five buckets). |
| `eval/run.py` | Runs scenarios × prompt versions, writes `results.csv`, transcripts and a summary table. |
| `eval/results/20261119-final/` | The submission run: all 70 rows (v1 × 22, v2 × 24, v3 × 24) with transcripts. |
| `recordings/` | Three compressed call recordings: the broken v1 baseline, a v3 promise-to-pay with OTP, a v3 edge case. |
| `tests/` | 74 offline tests (no keys needed). |
| `logs/calls/` | One JSON per real call: transcript, tool calls, guardrail events, latencies (git-ignored). |

## Setup (macOS)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # uv, once
source $HOME/.local/bin/env
uv sync
cp .env.example .env                                  # then fill in the keys
uv run python scripts/check_keys.py                   # proves the keys work (costs ~₹0.03)
uv run pytest                                         # offline tests
```

## Run it (two Terminal tabs)

```bash
# tab 1 — console + webhook receiver → http://localhost:8787
uv run python console.py

# tab 2 — the voice bot (takes ~40 s to load the VAD and noise models)
uv run voice_bot.py
```

Open http://localhost:8787, pick the prompt version and the customer, press **Connect**, allow the microphone. The bot speaks first. The transcript, tool calls, guardrail events and the outcome appear as the call runs; the outcome also lands in the CRM inbox and, if a human is needed, in the work queue.

If a port is held by a stale process: `lsof -ti :8787 | xargs kill -9` (console) or `lsof -ti :7860 | xargs kill -9` (bot). To stop a tab, close it (the `tee` pipe swallows Ctrl+C).

### Switches (`.env` or in front of the command)

| Variable | Default | What it does |
|---|---|---|
| `VOICE_PROVIDER` | `sarvam` | `deepgram` swaps STT + TTS to Deepgram when Sarvam is rate-limited or out of credit. |
| `ALLOW_BARGE_IN` | `0` | `1` lets the customer interrupt the bot mid-sentence (uses Sarvam's VAD speech-start). `0` = the bot always finishes its sentence; safer in a noisy room. |
| `NOISE_SUPPRESSION` | `1` | RNNoise on the microphone input. |
| `STT_VAD_THRESHOLD`, `STT_MIN_SPEECH_MS`, `STT_SILENCE_MS` | `0.75`, `350` (`500` with barge-in), `700` | Sarvam's server-side voice-activity tuning. |
| `PROMPT_VERSION`, `CUSTOMER_ID` | `v3`, `MB-CC-10042` | Defaults when the console has not set a next call. |

## Run the eval

```bash
PYTHONPATH=. uv run python eval/run.py --version v3 --no-webhook                 # v3, 24 scenarios, with the judge
PYTHONPATH=. uv run python eval/run.py --version v1 v2 v3 --no-webhook           # side by side
PYTHONPATH=. uv run python eval/run.py --version v3 --ids S11,S24 --no-webhook   # just two scenarios
PYTHONPATH=. uv run python eval/run.py --version v2 --no-judge --no-webhook      # deterministic checks only (free)
```

Results land in `eval/results/<timestamp>/` as `results.csv`, `summary.md` and one transcript per scenario, and show up on the console's Compare page. The same scenario can also be run from the console ("Run a stress test now").

## Mock accounts

| customer_id | Name | Outstanding | Min due | DPD | Late fee | OTP |
|---|---|---|---|---|---|---|
| MB-CC-10042 | Rahul Mehta | ₹48,200 | ₹12,500 | 12 | ₹750 | 4821 |
| MB-CC-10077 | Priya Nair | ₹8,400 | ₹2,100 | 5 | none | 7719 |
| MB-CC-10113 | Mohammed Irfan | ₹1,24,000 | ₹31,000 | 28 | ₹750 | 1130 |
| MB-CC-10158 | Sunita Deshmukh | ₹15,600 | ₹3,900 | 1 | ₹750 | 5801 |
| MB-CC-10201 | Arjun Varghese | ₹10,000 | ₹2,500 | 19 | none (boundary) | 2010 |

## Design notes

- **The model never sees the balance in its prompt** (except in v1, which reproduces the assignment's "you know everything about the account"). It gets figures only from `get_account_summary`, which refuses until `verify_otp` succeeds on that call.
- **Prompts set policy; code enforces it.** The output filter runs on every sentence before TTS; the input triggers run on every transcript; `record_outcome` validates the schema; the call controller forces a transfer if the model misses a trigger and ends the call once the outcome is recorded and the goodbye has been spoken.
- **One `CallSession` serves both the voice pipeline and the text eval**, so eval numbers describe the real bot.
- **Two failure rates are reported**: *delivered* (what the customer heard / the CRM got) and *raw* (what the model would have done without the code guardrails). The gap between them is the measured value of the guardrails; the raw number is the honest quality of the prompt.
- **The eval is allowed to fail the author.** Two scripted lines written on the last day were rejected by the judge (a third-party disclosure, an invented explanation) and were changed; see the writeup.
