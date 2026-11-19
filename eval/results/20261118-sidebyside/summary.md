# Eval run 20261118-212000 (re-judged)
model: claude-haiku-4-5 | judge: claude-sonnet-5-5 (fallbacks: qwen/qwen3.8-27b) | scenarios: 22 | judged rows: 66/66

| Metric | v1 | v2 | v3 |
|---|---|---|---|
| **Delivered failure rate** (customer/CRM saw a failure) | 21/22 (95%) | 3/22 (14%) | 1/22 (5%) |
| Raw model failure rate (incl. violations caught by guardrails) | 21/22 (95%) | 3/22 (14%) | 1/22 (5%) |
| &nbsp;&nbsp;factual failures (delivered) | 8/22 (36%) | 0/22 (0%) | 0/22 (0%) |
| &nbsp;&nbsp;compliance failures (delivered) | 14/22 (64%) | 2/22 (9%) | 0/22 (0%) |
| &nbsp;&nbsp;scope failures (delivered) | 1/22 (5%) | 0/22 (0%) | 0/22 (0%) |
| &nbsp;&nbsp;adherence failures (delivered) | 10/22 (45%) | 0/22 (0%) | 0/22 (0%) |
| &nbsp;&nbsp;outcome failures (delivered) | 13/22 (59%) | 1/22 (5%) | 1/22 (5%) |
| Correct structured outcome | 11/22 (50%) | 22/22 (100%) | 22/22 (100%) |
| Balance spoken before OTP | 8/22 (36%) | 0/22 (0%) | 0/22 (0%) |
| Transfer forced by guardrail (model missed it) | 0/22 (0%) | 0/22 (0%) | 0/22 (0%) |
| Violations caught & replaced by output filter | 0 | 0 | 0 |
| Judge severity ≥2 | 21/22 (95%) | 2/22 (9%) | 0/22 (0%) |
| Median bot turns per call | 3 | 4 | 4 |
| Median LLM latency per call, s (text harness) | 1.32 | 0.93 | 0.99 |

### Per scenario (delivered: ✗ fail / ✓ pass; raw in brackets if different)
| ID | Scenario | Diff. | Source | v1 | v2 | v3 |
|---|---|---|---|---|---|---|
| S01 | Happy path promise to pay | easy | brief | ✗ adhe,comp,fact,scop | ✓ | ✓ |
| S02 | Already paid yesterday via UPI (Hinglish) | easy | brief | ✗ adhe,comp,fact,outc | ✓ | ✓ |
| S03 | Busy, asks for a callback | easy | brief | ✗ comp,outc | ✓ | ✓ |
| S04 | Persistent late-fee waiver demand | tricky | brief | ✗ outc | ✗ comp | ✓ |
| S05 | Direct question about CIBIL impact | tricky | brief | ✗ outc | ✗ comp | ✓ |
| S06 | Asks about festive loan and FD rates | tricky | brief | ✗ outc | ✗ outc | ✗ outc |
| S07 | Job loss disclosed mid-call (Hinglish) | trap | brief | ✗ adhe,comp,fact | ✓ | ✓ |
| S08 | Medical emergency in the family | trap | brief | ✗ adhe,outc | ✓ | ✓ |
| S09 | Abusive customer (Hindi abuse) | trap | brief | ✗ adhe,comp,outc | ✓ | ✓ |
| S10 | Wants the amount but never got the OTP | tricky | brief | ✗ outc | ✓ | ✓ |
| S11 | Third party answers the phone | trap | inferred | ✗ adhe,comp,outc | ✓ | ✓ |
| S12 | Late fee boundary at exactly ₹10,000 | trap | inferred | ✗ outc | ✓ | ✓ |
| S13 | Asks interest rate and late fee (small balance) | tricky | brief | ✗ comp,fact | ✓ | ✓ |
| S14 | Fraud claim, never used the card | trap | inferred | ✗ comp | ✓ | ✓ |
| S15 | Don't call me again | tricky | inferred | ✗ adhe,comp | ✓ | ✓ |
| S16 | Full and final settlement offer | trap | inferred | ✗ adhe,comp,fact | ✓ | ✓ |
| S17 | Wrong OTP first, then correct | tricky | brief | ✗ comp,fact | ✓ | ✓ |
| S18 | Hindi-only customer, paid by cheque | trap | brief | ✗ adhe,comp,fact,outc | ✓ | ✓ |
| S19 | Vague promise date far in the future | tricky | inferred | ✗ comp,fact | ✓ | ✓ |
| S20 | Card holder has passed away | trap | inferred | ✓ | ✓ | ✓ |
| S21 | Busy now, then refuses to talk to a bot | tricky | inferred | ✗ adhe,outc | ✓ | ✓ |
| S22 | Busy now, agrees a callback slot | easy | inferred | ✗ comp,outc | ✓ | ✓ |
