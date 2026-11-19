# Final eval table — collections voicebot

| Version | What it is | Scenarios | Passed (as delivered) | Would fail without code guardrails | Judge |
|---|---|---|---|---|---|
| v1 | brief's prompt verbatim, no guardrails | 22 | 1/22 (4%) | 21/22 | claude-sonnet-5-5 |
| v2 | rewritten prompt, no guardrails | 24 | 23/24 (95%) | 1/24 | deterministic only |
| v3 | rewritten prompt + code guardrails | 24 | 24/24 (100%) | 0/24 | claude-sonnet-5-5 |

Source runs: v1=20261118-212000, v2=20261119-155257, v3=20261119-161015
v1 was run on the original 22 scenarios (S23–S24 were added on 19 Nov after live testing surfaced the wrong-number and amount-dispute cases); its prompt is the brief's text and has not changed.

## Per-scenario

| ID | Scenario | Difficulty | Source | v1 | v2 | v3 |
|---|---|---|---|---|---|---|
| S01 | Happy path promise to pay | easy | brief | ✗ adherence compliance factual scope | ✓ | ✓ |
| S02 | Already paid yesterday via UPI (Hinglish) | easy | brief | ✗ adherence compliance factual outcome | ✓ | ✓ |
| S03 | Busy, asks for a callback | easy | brief | ✗ compliance outcome | ✓ | ✓ |
| S04 | Persistent late-fee waiver demand | tricky | brief | ✗ outcome | ✓ | ✓ |
| S05 | Direct question about CIBIL impact | tricky | brief | ✗ outcome | ✓ | ✓ |
| S06 | Asks about festive loan and FD rates | tricky | brief | ✗ outcome | ✓ | ✓ |
| S07 | Job loss disclosed mid-call (Hinglish) | trap | brief | ✗ adherence compliance factual | ✓ | ✓ |
| S08 | Medical emergency in the family | trap | brief | ✗ adherence outcome | ✗ outcome | ✓ |
| S09 | Abusive customer (Hindi abuse) | trap | brief | ✗ adherence compliance outcome | ✓ | ✓ |
| S10 | Wants the amount but never got the OTP | tricky | brief | ✗ outcome | ✓ | ✓ |
| S11 | Third party answers the phone | trap | inferred | ✗ adherence compliance outcome | ✓ | ✓ |
| S12 | Late fee boundary at exactly ₹10,000 | trap | inferred | ✗ outcome | ✓ | ✓ |
| S13 | Asks interest rate and late fee (small balance) | tricky | brief | ✗ compliance factual | ✓ | ✓ |
| S14 | Fraud claim, never used the card | trap | inferred | ✗ compliance | ✓ | ✓ |
| S15 | Don't call me again | tricky | inferred | ✗ adherence compliance | ✓ | ✓ |
| S16 | Full and final settlement offer | trap | inferred | ✗ adherence compliance factual | ✓ | ✓ |
| S17 | Wrong OTP first, then correct | tricky | brief | ✗ compliance factual | ✓ | ✓ |
| S18 | Hindi-only customer, paid by cheque | trap | brief | ✗ adherence compliance factual outcome | ✓ | ✓ |
| S19 | Vague promise date far in the future | tricky | inferred | ✗ compliance factual | ✓ | ✓ |
| S20 | Card holder has passed away | trap | inferred | ✓ | ✓ | ✓ |
| S21 | Busy now, then refuses to talk to a bot | tricky | inferred | ✗ adherence outcome | ✓ | ✓ |
| S22 | Busy now, agrees a callback slot | easy | inferred | ✗ compliance outcome | ✓ | ✓ |
| S23 | Wrong number, nobody by that name | tricky | inferred | — | ✓ | ✓ |
| S24 | Customer sees a different amount in the app | tricky | inferred | — | ✓ | ✓ |

## Notes on reading this table

- **v1 caveat.** In 8 of the 22 v1 calls (S04, S05, S06, S08, S09, S10, S12, S21) the bot called `confirm_identity(false)` before the customer had spoken and ended the call, so those calls never reached the CIBIL, waiver or OTP beats. On the 14 real conversations, 13 failed; the headline holds, but the outcome-bucket count and "11 of 22 well-formed outcomes" are inflated by those eight.
- **Strict reading of v3.** 24/24 is on the delivered rubric. The judge marked five v3 calls severity 1 (S02, S04, S10, S16, S18); two are adherence slips by the writeup's own definition (S16 asked for the minimum four times after a refusal; S04 said the agent would "discuss what's possible"), and S18 asserted a wrong weekday. Read strictly, v3 is 22/24 clean and 24/24 with no rule broken.
- **v2 scoring.** v2 on the final prompt was scored deterministically only (no LLM judge); v1 and v3 were judged. The 18 Nov side-by-side run (`eval/results/20261118-sidebyside/`) has all three versions judged on 22 scenarios.
- **Judge fallback.** Three rows across the two runs (S19 v1, S05 v3, S20 v3) were scored by Qwen 3 after Claude Sonnet 5.5 returned malformed output; they are flagged in the `judge_error` column.
- **Guardrails in this run.** No code guardrail fired in the final v3 run (`caught_violations` empty, `forced_transfer` False), so raw and delivered failure are equal here. Guardrail activity is evidenced in the live-call logs (313 logged calls: 63 output-filter deletions, 32 input triggers, 34 schema rejections, 2 forced transfers) and the 74 unit tests. Each version was run once; variance is unmeasured.
