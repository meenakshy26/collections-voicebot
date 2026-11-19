"""Re-score an existing results folder with the LLM judge, without re-running the calls.

  uv run python -m eval.rejudge eval/results/20261118-212000
  uv run python -m eval.rejudge eval/results/20261118-212000 --all     # re-judge every row, not just unscored ones

Useful when the judge failed mid-run (billing, rate limits) or when you change the judge
rubric and want comparable scores across versions.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from pathlib import Path

from bot.config import get_settings
from eval.harness import RunResult, Scenario
from eval.judge import Score, combine, judge
from eval.llm_client import client_from_settings
from eval.run import FALLBACK_JUDGES, summarise

RETRY_RE = re.compile(r"try again in (\d+(?:\.\d+)?)s", re.I)


async def judge_with_retries(sc: Scenario, result: RunResult, models: list[str]) -> tuple[dict, str | None]:
    """Try each judge model in turn; on a rate limit, wait as instructed (max 3 times)."""
    errors: list[str] = []
    for model in models:
        settings = get_settings(judge_model=model)
        client = client_from_settings(settings, "judge")
        for attempt in range(3):
            try:
                j = await judge(sc, result, client)
                if not isinstance(j, dict) or "compliance" not in j:
                    raise ValueError(f"judge returned malformed JSON: {str(j)[:120]}")
                j.setdefault("severity", 0)
                j.setdefault("worst_moment", "")
                j.setdefault("checks", [])
                return j, (None if model == models[0] else f"scored by {model}; " + " | ".join(errors))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{model}: {type(e).__name__}: {str(e)[:160]}")
                m = RETRY_RE.search(str(e))
                if "429" in str(e) and m and attempt < 2:
                    await asyncio.sleep(float(m.group(1)) + 1)
                    continue
                if "429" in str(e) and "credits" not in str(e).lower() and attempt < 2:
                    await asyncio.sleep(10)
                    continue
                if type(e).__name__ in ("JSONDecodeError", "ValueError") and attempt < 2:
                    continue  # the judge put an unescaped quote in its evidence; ask again
                if "credit balance" in str(e).lower() or "no credits" in str(e).lower():
                    break  # no point retrying this model
                break
    return {}, " | ".join(errors)


async def main_async(args):
    out = Path(args.results_dir)
    runs = sorted((out / "runs").glob("*.json"))
    if not runs:
        raise SystemExit(f"no runs in {out}")
    settings = get_settings()
    models = [settings.judge_model] + [m for m in FALLBACK_JUDGES if m != settings.judge_model]
    sem = asyncio.Semaphore(args.concurrency)
    scores: list[Score] = []

    async def one(path: Path):
        data = json.loads(path.read_text())
        sc = Scenario(**data["scenario"])
        res = RunResult(**data["result"])
        row = data["score"]
        score = Score(
            **{k: row[k] for k in Score.__dataclass_fields__ if k in row and k not in ("judge", "spoken_violations", "caught_violations", "fail_buckets_delivered", "fail_buckets_raw")},
            spoken_violations=[x for x in row.get("spoken_violations", "").split(";") if x],
            caught_violations=[x for x in row.get("caught_violations", "").split(";") if x],
        )
        score.judge = {}
        already = row.get("judge_severity") not in (None, "")
        if already and not args.all:
            # keep the stored judge verdict
            score.judge = {b: {"pass": row.get(f"judge_{b}"), "evidence": row.get(f"judge_{b}_evidence", "")} for b in ("factual", "compliance", "scope", "adherence", "outcome", "tone")}
            score.judge["severity"] = row.get("judge_severity")
            score.judge["worst_moment"] = row.get("judge_worst_moment", "")
            score.judge["checks"] = json.loads(row.get("judge_checks") or "[]")
            score.judge_error = row.get("judge_error")
        elif not res.error:
            async with sem:
                score.judge, score.judge_error = await judge_with_retries(sc, res, models)
        score = combine(score)
        data["score"] = score.to_row()
        path.write_text(json.dumps(data, indent=2, default=str))
        status = "FAIL" if score.delivered_fail else ("fail(raw)" if score.raw_fail else "pass")
        print(f"  {score.scenario_id:<4} {score.version} {status:<9} judge={'ok' if score.judge else 'none'} {score.judge_error or ''}"[:160], flush=True)
        return score

    scores = list(await asyncio.gather(*(one(p) for p in runs)))
    versions = []
    for s in scores:
        if s.version not in versions:
            versions.append(s.version)
    scores.sort(key=lambda s: (versions.index(s.version), s.scenario_id))
    rows = [s.to_row() for s in scores]
    with (out / "results.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    judged = sum(1 for s in scores if s.judge)
    summary = summarise(scores, versions)
    (out / "summary.md").write_text(f"# Eval run {out.name} (re-judged)\nmodel: {scores[0].model} | judge: {settings.judge_model} (fallbacks: {', '.join(FALLBACK_JUDGES)}) | scenarios: {len(scores)//len(versions)} | judged rows: {judged}/{len(scores)}\n\n{summary}\n")
    print("\n" + summary)
    print(f"\nJudged {judged}/{len(scores)} rows. Written: {out/'results.csv'}  {out/'summary.md'}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("results_dir")
    p.add_argument("--all", action="store_true", help="re-judge rows that already have a verdict")
    p.add_argument("--concurrency", type=int, default=2)
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
