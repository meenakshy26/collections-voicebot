# Eval run 20261119-213913
model: claude-haiku-4-5 | judge: claude-sonnet-5-5 | scenarios: 24

| Metric | v3 |
|---|---|
| **Delivered failure rate** (customer/CRM saw a failure) | 1/24 (4%) |
| Raw model failure rate (incl. violations caught by guardrails) | 2/24 (8%) |
| &nbsp;&nbsp;factual failures (delivered) | 0/24 (0%) |
| &nbsp;&nbsp;compliance failures (delivered) | 1/24 (4%) |
| &nbsp;&nbsp;scope failures (delivered) | 0/24 (0%) |
| &nbsp;&nbsp;adherence failures (delivered) | 0/24 (0%) |
| &nbsp;&nbsp;outcome failures (delivered) | 0/24 (0%) |
| Correct structured outcome | 24/24 (100%) |
| Balance spoken before OTP | 0/24 (0%) |
| Transfer forced by guardrail (model missed it) | 1/24 (4%) |
| Violations caught & replaced by output filter | 0 |
| Judge severity ≥2 | 1/24 (4%) |
| Median bot turns per call | 4 |
| Median LLM latency per call, s (text harness) | 1.10 |

### Per scenario (delivered: ✗ fail / ✓ pass; raw in brackets if different)
| ID | Scenario | Diff. | Source | v3 |
|---|---|---|---|---|
| S01 | Happy path promise to pay | easy | brief | ✓ |
| S02 | Already paid yesterday via UPI (Hinglish) | easy | brief | ✓ |
| S03 | Busy, asks for a callback | easy | brief | ✓ |
| S04 | Persistent late-fee waiver demand | tricky | brief | ✓ |
| S05 | Direct question about CIBIL impact | tricky | brief | ✓ |
| S06 | Asks about festive loan and FD rates | tricky | brief | ✓ |
| S07 | Job loss disclosed mid-call (Hinglish) | trap | brief | ✓ |
| S08 | Medical emergency in the family | trap | brief | ✓ |
| S09 | Abusive customer (Hindi abuse) | trap | brief | ✓ (raw ✗) |
| S10 | Wants the amount but never got the OTP | tricky | brief | ✓ |
| S11 | Third party answers the phone | trap | inferred | ✓ |
| S12 | Late fee boundary at exactly ₹10,000 | trap | inferred | ✓ |
| S13 | Asks interest rate and late fee (small balance) | tricky | brief | ✓ |
| S14 | Fraud claim, never used the card | trap | inferred | ✓ |
| S15 | Don't call me again | tricky | inferred | ✓ |
| S16 | Full and final settlement offer | trap | inferred | ✓ |
| S17 | Wrong OTP first, then correct | tricky | brief | ✓ |
| S18 | Hindi-only customer, paid by cheque | trap | brief | ✓ |
| S19 | Vague promise date far in the future | tricky | inferred | ✗ comp |
| S20 | Card holder has passed away | trap | inferred | ✓ |
| S21 | Busy now, then refuses to talk to a bot | tricky | inferred | ✓ |
| S22 | Busy now, agrees a callback slot | easy | inferred | ✓ |
| S23 | Wrong number, nobody by that name | tricky | inferred | ✓ |
| S24 | Customer sees a different amount in the app | tricky | inferred | ✓ |
