"""Run the scenario set against one or more prompt versions and score it.

  uv run python -m eval.run --version v1                 # baseline
  uv run python -m eval.run --version v1 v2 v3           # all versions
  uv run python -m eval.run --version v3 --ids S03,S09   # a subset
  uv run python -m eval.run --version v3 --model qwen/qwen3.8-27b   # LLM bake-off (Groq)
  uv run python -m eval.run --version v3 --no-judge      # deterministic checks only (free)

Outputs (per run, under eval/results/<timestamp>/):
  results.csv           one row per scenario x version
  transcripts/*.txt     readable transcripts
  runs/*.json           full machine-readable run (events, tool calls, latencies)
  summary.md            v1 vs v2 vs v3 table
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from bot.config import REPO_ROOT, get_settings
from eval.harness import RunResult, Scenario, run_scenario, transcript_text
from eval.judge import BUCKETS, Score, combine, deterministic, judge
from eval.llm_client import client_from_settings

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("bot").setLevel(logging.WARNING)

SCENARIOS_PATH = REPO_ROOT / "eval" / "scenarios.yaml"
# Primary judge is OpenAI (different family from the bot). Fallbacks in order: a stronger
# Claude model (different model, same family), then an open-weight model on Groq (free but
# rate-limited). eval/rejudge.py can re-score a run later with the primary judge.
FALLBACK_JUDGES = ["qwen/qwen3.8-27b"]


async def run_one(sc: Scenario, version: str, model: str, args, out: Path, sem: asyncio.Semaphore) -> Score:
    settings = get_settings(prompt_version=version, llm_model=model)
    if args.no_webhook:
        settings.webhook_url = ""
    bot_client = client_from_settings(settings, "bot")
    customer_client = None if sc.turns is not None else client_from_settings(settings, "customer")
    async with sem:
        result = await run_scenario(sc, settings, bot_client, customer_client, today=date.today())
        score = deterministic(sc, result)
        if not args.no_judge and not result.error:
            try:
                score.judge = await judge(sc, result, client_from_settings(settings, "judge"))
            except Exception as e:  # noqa: BLE001
                # Fall back so a billing hiccup on the primary judge never leaves a run unscored.
                score.judge_error = f"{type(e).__name__}: {str(e)[:120]}"
                for fb in FALLBACK_JUDGES:
                    try:
                        score.judge = await judge(sc, result, client_from_settings(get_settings(judge_model=fb), "judge"))
                        score.judge_error = f"primary judge failed ({type(e).__name__}); scored by {fb}"
                        break
                    except Exception as e2:  # noqa: BLE001
                        score.judge_error += f" | {fb}: {type(e2).__name__}: {str(e2)[:120]}"
        score = combine(score)
    tag = f"{sc.id}_{version}"
    (out / "transcripts").mkdir(parents=True, exist_ok=True)
    (out / "runs").mkdir(parents=True, exist_ok=True)
    (out / "transcripts" / f"{tag}.txt").write_text(
        f"# {sc.id} {sc.title} | {version} | {model}\n# expected {score.outcome_expected} | got {score.outcome_actual} | delivered_fail={score.delivered_fail} raw_fail={score.raw_fail}\n"
        f"# buckets delivered={score.fail_buckets_delivered} raw={score.fail_buckets_raw}\n# judge: {json.dumps(score.judge, ensure_ascii=False)[:600]}\n\n{transcript_text(result)}\n"
    )
    (out / "runs" / f"{tag}.json").write_text(json.dumps({"scenario": sc.__dict__, "result": result.__dict__, "score": score.to_row()}, indent=2, default=str))
    status = "FAIL" if score.delivered_fail else ("fail(raw)" if score.raw_fail else "pass")
    print(f"  {sc.id:<4} {version} {status:<9} expected={score.outcome_expected:<32} got={score.outcome_actual}", flush=True)
    return score


def summarise(scores: list[Score], versions: list[str]) -> str:
    lines = ["| Metric | " + " | ".join(versions) + " |", "|---|" + "---|" * len(versions)]
    by_v = {v: [s for s in scores if s.version == v] for v in versions}

    def pct(v, pred):
        rows = by_v[v]
        return f"{sum(1 for s in rows if pred(s))}/{len(rows)} ({100*sum(1 for s in rows if pred(s))/max(1,len(rows)):.0f}%)"

    lines.append("| **Delivered failure rate** (customer/CRM saw a failure) | " + " | ".join(pct(v, lambda s: s.delivered_fail) for v in versions) + " |")
    lines.append("| Raw model failure rate (incl. violations caught by guardrails) | " + " | ".join(pct(v, lambda s: s.raw_fail) for v in versions) + " |")
    for b in BUCKETS:
        lines.append(f"| &nbsp;&nbsp;{b} failures (delivered) | " + " | ".join(pct(v, lambda s, b=b: b in s.fail_buckets_delivered) for v in versions) + " |")
    lines.append("| Correct structured outcome | " + " | ".join(pct(v, lambda s: s.outcome_ok) for v in versions) + " |")
    lines.append("| Balance spoken before OTP | " + " | ".join(pct(v, lambda s: s.balance_before_otp) for v in versions) + " |")
    lines.append("| Transfer forced by guardrail (model missed it) | " + " | ".join(pct(v, lambda s: s.forced_transfer) for v in versions) + " |")
    lines.append("| Violations caught & replaced by output filter | " + " | ".join(str(sum(len(s.caught_violations) for s in by_v[v])) for v in versions) + " |")
    lines.append("| Judge severity ≥2 | " + " | ".join(pct(v, lambda s: (s.judge or {}).get("severity", 0) >= 2) for v in versions) + " |")
    lines.append("| Median bot turns per call | " + " | ".join(f"{sorted(s.bot_turns for s in by_v[v])[len(by_v[v])//2] if by_v[v] else 0}" for v in versions) + " |")
    lines.append("| Median LLM latency per call, s (text harness) | " + " | ".join(f"{sorted(s.llm_p50_latency for s in by_v[v])[len(by_v[v])//2] if by_v[v] else 0:.2f}" for v in versions) + " |")
    lines.append("")
    lines.append("### Per scenario (delivered: ✗ fail / ✓ pass; raw in brackets if different)")
    lines.append("| ID | Scenario | Diff. | Source | " + " | ".join(versions) + " |")
    lines.append("|---|---|---|---|" + "---|" * len(versions))
    ids = []
    for s in scores:
        if s.scenario_id not in ids:
            ids.append(s.scenario_id)
    for sid in ids:
        row = next(s for s in scores if s.scenario_id == sid)
        cells = []
        for v in versions:
            m = next((s for s in by_v[v] if s.scenario_id == sid), None)
            if m is None:
                cells.append("—")
                continue
            mark = "✗" if m.delivered_fail else "✓"
            if m.raw_fail and not m.delivered_fail:
                mark += " (raw ✗)"
            if m.fail_buckets_delivered:
                mark += " " + ",".join(b[:4] for b in m.fail_buckets_delivered)
            cells.append(mark)
        lines.append(f"| {sid} | {row.title} | {row.difficulty} | {row.source} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


async def main_async(args):
    scenarios = Scenario.load_all(Path(args.scenarios))
    if args.ids:
        want = set(x.strip() for x in args.ids.split(","))
        scenarios = [s for s in scenarios if s.id in want]
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    print(f"Running {len(scenarios)} scenarios x {args.version} with model {args.model} -> {out}")
    sem = asyncio.Semaphore(args.concurrency)
    tasks = [run_one(sc, v, args.model, args, out, sem) for v in args.version for sc in scenarios]
    scores: list[Score] = list(await asyncio.gather(*tasks))

    rows = [s.to_row() for s in scores]
    with (out / "results.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    summary = summarise(scores, args.version)
    (out / "summary.md").write_text(f"# Eval run {out.name}\nmodel: {args.model} | judge: {get_settings().judge_model} | scenarios: {len(scenarios)}\n\n{summary}\n")
    print("\n" + summary)
    print(f"\nWritten: {out/'results.csv'}  {out/'summary.md'}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--version", nargs="+", default=["v1"], choices=["v1", "v2", "v3"])
    p.add_argument("--model", default=get_settings().llm_model)
    p.add_argument("--scenarios", default=str(SCENARIOS_PATH))
    p.add_argument("--ids", default="")
    p.add_argument("--out", default=str(REPO_ROOT / "eval" / "results"))
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--no-webhook", action="store_true", help="don't POST eval outcomes to the webhook")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
