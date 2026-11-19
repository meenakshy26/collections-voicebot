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
