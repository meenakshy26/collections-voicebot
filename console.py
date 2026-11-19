"""Mumbai Bank Collections Console — local SaaS-style front end for the voicebot.

  uv run python console.py        → http://localhost:8787

One process does four jobs:
  * receives the outcome webhook (POST /outcome), so it is also the CRM stand-in
  * shows calls live (the bot autosaves logs/calls/<id>.json after every turn)
  * serves eval results and can run a scenario against two prompt versions on demand
  * keeps a human work queue of calls that need follow-up, with assignment state

Everything is read from the files the bot already writes; nothing here changes how the
bot behaves except logs/next_call.json, which picks the version/customer for the next call.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.config import GUARDRAILS_BY_VERSION, PROMPT_VERSIONS, REPO_ROOT, get_settings  # noqa: E402
from bot.prompts import build_system_prompt  # noqa: E402
from bot.mock_data import ACCOUNTS, get_account  # noqa: E402
from eval.harness import Scenario, run_scenario, transcript_text  # noqa: E402
from eval.judge import combine, deterministic, judge  # noqa: E402
from eval.llm_client import client_from_settings  # noqa: E402

LOGS = REPO_ROOT / "logs"
CALLS = LOGS / "calls"
QUEUE_FILE = LOGS / "work_queue.json"
NEXT_CALL = LOGS / "next_call.json"
INBOX = LOGS / "crm_inbox.jsonl"
RESULTS = REPO_ROOT / "eval" / "results"
SCENARIOS = REPO_ROOT / "eval" / "scenarios.yaml"
INDEX_HTML = REPO_ROOT / "console" / "index.html"

AGENTS = [
    {"id": "anita", "name": "Anita Kulkarni", "team": "Mumbai West", "languages": ["english", "hinglish", "hindi"]},
    {"id": "ravi", "name": "Ravi Shetty", "team": "Mumbai Central", "languages": ["english", "hinglish"]},
    {"id": "farhan", "name": "Farhan Sheikh", "team": "Navi Mumbai", "languages": ["english", "hinglish", "hindi"]},
    {"id": "disputes", "name": "Disputes desk", "team": "Central ops", "languages": ["english"]},
]

app = FastAPI(title="Mumbai Bank Collections Console")
jobs: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------- helpers
def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return default


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str))


_CALL_CACHE: dict[str, tuple[float, dict]] = {}


def load_calls() -> list[dict]:
    # Cached by mtime: the console polls every 1.5 s and the folder grows with every call,
    # so re-parsing every file each poll would stall the event loop (and the webhook receiver).
    calls = []
    for f in CALLS.glob("*.json"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        cached = _CALL_CACHE.get(f.name)
        if cached and cached[0] == mtime:
            d = cached[1]
        else:
            d = _read_json(f, None)
            if d and "call_id" in d:
                d.setdefault("channel", "unknown")
                _CALL_CACHE[f.name] = (mtime, d)
        if d and "call_id" in d:
            calls.append(dict(d))
    calls.sort(key=lambda c: c.get("started_at", ""), reverse=True)
    return calls


def dpd_bucket(dpd: int) -> str:
    return "1-7 days" if dpd <= 7 else ("8-15 days" if dpd <= 15 else "16-30 days")


def balance_band(amount: int) -> str:
    return "≤ ₹10k" if amount <= 10_000 else ("₹10k–50k" if amount <= 50_000 else "> ₹50k")


def customer_context(customer_id: str) -> dict:
    try:
        a = get_account(customer_id)
    except KeyError:
        return {"customer_id": customer_id, "name": customer_id}
    return {
        "customer_id": a.customer_id,
        "name": a.full_name,
        "mobile_last4": a.registered_mobile_last4,
        "outstanding_inr": a.outstanding_inr,
        "minimum_due_inr": a.minimum_due_inr,
        "days_past_due": a.days_past_due,
        "dpd_bucket": dpd_bucket(a.days_past_due),
        "balance_band": balance_band(a.outstanding_inr),
        "late_fee_inr": a.late_fee_inr,
    }


def summarise_call(c: dict) -> dict:
    oc = c.get("outcome") or {}
    last_customer = next((t["text"] for t in reversed(c.get("transcript", [])) if t["role"] == "customer"), "")
    last_bot = next((t["text"] for t in reversed(c.get("transcript", [])) if t["role"] == "bot"), "")
    return {
        "call_id": c["call_id"],
        "started_at": c.get("started_at"),
        "duration_sec": c.get("duration_sec"),
        "prompt_version": c.get("prompt_version"),
        "channel": c.get("channel", "unknown"),
        "customer": customer_context(c.get("customer_id", "")),
        "outcome": oc.get("outcome"),
        "reason": oc.get("transfer_reason") or oc.get("callback_reason"),
        "callback_time": oc.get("callback_time"),
        "ptp_date": oc.get("ptp_date"),
        "ptp_amount_inr": oc.get("ptp_amount_inr"),
        "paid_mode": oc.get("paid_mode"),
        "paid_date": oc.get("paid_date"),
        "customer_note": oc.get("customer_note", ""),
        "otp_verified": c.get("otp_verified"),
        "language_mix": oc.get("language_mix"),
        "bot_turns": c.get("bot_turns"),
        "guardrail_hits": sum(1 for e in c.get("guardrail_events", []) if e.get("kind") in ("output_violation", "input_trigger", "forced_transfer", "tool_refused", "outcome_rejected")),
        "webhook": (c.get("webhook_result") or {}).get("status"),
        "ended": c.get("ended", True),
        "last_customer": last_customer,
        "last_bot": last_bot,
        "identity_confirmed": c.get("identity_confirmed"),
        "violations": sum(1 for e in c.get("guardrail_events", []) if e.get("kind") in ("output_violation", "forced_transfer", "outcome_rejected")),
        "event_kinds": [e.get("kind") for e in c.get("guardrail_events", []) if e.get("kind") != "ttfb_summary"],
        "ttfb": _ttfb(c),
    }


def _ttfb(c: dict) -> dict:
    """Median first-token / first-audio latency for the call, from the monitor's summary event."""
    ev = next((e for e in c.get("guardrail_events", []) if e.get("kind") == "ttfb_summary"), None)
    out = {}
    if ev:
        for k, v in ev.items():
            if isinstance(v, list) and v:
                key = "llm" if "LLM" in k else "tts" if "TTS" in k else None
                if key:
                    vs = sorted(v)
                    out[key] = vs[len(vs) // 2]
    return out


# --------------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
async def index():
    # no-store so Chrome never serves a stale copy of the console after a patch
    return HTMLResponse(INDEX_HTML.read_text(), headers={"Cache-Control": "no-store"})


VERSION_INFO = {
    "v1": {
        "name": "Baseline — the brief's prompt, verbatim",
        "summary": "The system prompt exactly as supplied in the assignment, with the customer's account facts pasted into it. No code guardrails. This is the control: it shows what the model does when policy lives only in a loosely written prompt.",
        "changes": ["Prompt text unchanged from the brief", "Balance, due date and late fee are inside the prompt, so the model can reveal them before OTP", "No output filter, no trigger detection, lenient outcome schema"],
        "fails_on": "Reads digits one by one, slips into Hindi, asks two questions at once, discusses balance before verification, promises waivers.",
    },
    "v2": {
        "name": "Rewritten prompt, same model, still prompt-only",
        "summary": "A rewritten prompt that spells out the call flow, the exact sentences to use at risky moments, and what must never be said. No code guardrails yet, so the eval can attribute improvement to the prompt alone.",
        "changes": ["English only, one question per turn, amounts in words", "Account facts removed from the prompt; the model must call tools, and OTP comes first", "Scripted lines for waiver requests, CIBIL questions, third parties, wrong numbers, do-not-call", "Today's date and a 14-day calendar injected", "Record the outcome the moment it is known"],
        "fails_on": "Still trusts the model to follow the rules: occasionally slips on product questions and implied reassurances.",
    },
    "v3": {
        "name": "v2 prompt + code guardrails",
        "summary": "Identical prompt to v2, but the code enforces what the prompt asks. Prompts set policy; code enforces it. The 'caught' cells in the matrix are exactly where v3 saved v2.",
        "changes": ["Identity and OTP gates: account tools refuse until the caller is verified", "Output filter: waiver, CIBIL, legal and product phrases are stripped before text-to-speech", "Input triggers: abuse, medical, bereavement, job loss, 'I want a human' force a hand-off even if the model misses it", "Strict outcome schema on record_outcome; the call cannot end without one", "Turn and time budget nudges the call to close"],
        "fails_on": "The one remaining miss is a structural limit of the single-outcome schema, not a behaviour slip.",
    },
}


@app.get("/api/versions")
async def versions(customer_id: str | None = None):
    s = get_settings()
    acct = get_account(customer_id or s.customer_id)
    out = []
    for v in PROMPT_VERSIONS:
        g = GUARDRAILS_BY_VERSION[v]
        out.append({
            "version": v,
            **VERSION_INFO[v],
            "guardrails": {k: getattr(g, k) for k in ("identity_gate", "otp_gate", "output_filter", "input_triggers", "outcome_validation", "turn_budget", "account_in_prompt")},
            "prompt": build_system_prompt(v, acct, "Priya", date.today()),
        })
    return out


# --------------------------------------------------------------------------- webhook (CRM stand-in)
@app.post("/outcome")
async def outcome(req: Request):
    payload = await req.json()
    entry = {"received_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    with INBOX.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"OUTCOME RECEIVED: {payload.get('outcome')} for {payload.get('customer_id')} ({payload.get('call_id', '')[:8]})", flush=True)
    return {"status": "accepted"}


@app.get("/api/inbox")
async def inbox(limit: int = 50):
    if not INBOX.exists():
        return []
    lines = INBOX.read_text().strip().splitlines()[-limit:]
    return [json.loads(x) for x in reversed(lines)]


# --------------------------------------------------------------------------- config / next call
@app.get("/api/config")
async def config():
    s = get_settings()
    return {
        "versions": list(PROMPT_VERSIONS),
        "customers": [customer_context(cid) | {"otp": a.otp, "notes": a.notes} for cid, a in ACCOUNTS.items()],
        "agents": AGENTS,
        "next_call": _read_json(NEXT_CALL, {"prompt_version": s.prompt_version, "customer_id": s.customer_id}),
        "models": {"llm": s.llm_model, "judge": s.judge_model, "stt": f"{s.stt_language}/{s.stt_mode}", "tts": f"{s.tts_model}/{s.tts_voice}"},
        "bot_url": "http://localhost:7860",
    }


@app.post("/api/next_call")
async def set_next_call(req: Request):
    body = await req.json()
    v = body.get("prompt_version")
    cid = body.get("customer_id")
    if v not in PROMPT_VERSIONS or cid not in ACCOUNTS:
        raise HTTPException(400, "unknown version or customer")
    _write_json(NEXT_CALL, {"prompt_version": v, "customer_id": cid, "set_at": datetime.now(timezone.utc).isoformat()})
    return {"ok": True, "prompt_version": v, "customer_id": cid}


# --------------------------------------------------------------------------- calls
@app.get("/api/calls")
async def calls(channel: str = "all", limit: int = 200):
    out = [summarise_call(c) for c in load_calls()]
    if channel != "all":
        out = [c for c in out if c["channel"] == channel]
    return out[:limit]


@app.get("/api/calls/live")
async def live_call():
    for c in load_calls():
        if c.get("channel", "unknown") != "simulated":
            c["customer"] = customer_context(c.get("customer_id", ""))
            return c
    return JSONResponse({"detail": "no calls yet"}, status_code=404)


@app.get("/api/calls/{call_id}")
async def call_detail(call_id: str):
    d = _read_json(CALLS / f"{call_id}.json", None)
    if not d:
        raise HTTPException(404, "no such call")
    d["customer"] = customer_context(d.get("customer_id", ""))
    return d


# --------------------------------------------------------------------------- analytics
@app.get("/api/analytics")
async def analytics(channel: str = "all", version: str = "all", days: int = 0):
    rows = [summarise_call(c) for c in load_calls() if c.get("ended", True)]
    if channel != "all":
        rows = [r for r in rows if r["channel"] == channel]
    if version != "all":
        rows = [r for r in rows if r["prompt_version"] == version]
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = [r for r in rows if (r["started_at"] or "") >= cutoff]

    def agg(group: list[dict]) -> dict:
        n = len(group)
        oc = Counter(r["outcome"] for r in group)
        reasons = Counter(r["reason"] for r in group if r["reason"])
        durs = sorted(r["duration_sec"] or 0 for r in group)
        return {
            "calls": n,
            "outcomes": dict(oc),
            "ptp_rate": round(oc.get("promise_to_pay", 0) / n, 3) if n else 0,
            "already_paid_rate": round(oc.get("already_paid", 0) / n, 3) if n else 0,
            "transfer_rate": round(oc.get("dispute_or_transfer", 0) / n, 3) if n else 0,
            "callback_rate": round(oc.get("callback_request", 0) / n, 3) if n else 0,
            "otp_verified_rate": round(sum(1 for r in group if r["otp_verified"]) / n, 3) if n else 0,
            "median_duration_sec": durs[len(durs) // 2] if durs else 0,
            "guardrail_hits_per_call": round(sum(r["guardrail_hits"] for r in group) / n, 2) if n else 0,
            "reasons": dict(reasons),
            "needs_human": sum(1 for r in group if _needs_human(r)),
        }

    order = {"1-7 days": 0, "8-15 days": 1, "16-30 days": 2, "≤ ₹10k": 0, "₹10k–50k": 1, "> ₹50k": 2, "english": 0, "hinglish": 1, "hindi": 2}
    by = lambda key: {k: agg(v) for k, v in sorted(_group(rows, key).items(), key=lambda kv: (order.get(kv[0], 9), kv[0]))}  # noqa: E731
    # --- dashboard extras
    timeline: dict[str, Counter] = defaultdict(Counter)
    hourly: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        day = (r["started_at"] or "")[:10]
        if day:
            timeline[day][r["outcome"] or "none"] += 1
            timeline[day]["calls"] += 1
            hourly[(r["started_at"] or "")[:13] + ":00"][r["outcome"] or "none"] += 1
    n = len(rows)
    funnel = [
        ("Calls connected", n),
        ("Identity confirmed", sum(1 for r in rows if r["identity_confirmed"] is True)),
        ("OTP verified", sum(1 for r in rows if r["otp_verified"])),
        ("Outcome recorded", sum(1 for r in rows if r["outcome"])),
        ("Promise to pay", sum(1 for r in rows if r["outcome"] == "promise_to_pay")),
    ]
    kinds = Counter(k for r in rows for k in r["event_kinds"])
    llm = sorted(r["ttfb"]["llm"] for r in rows if r["ttfb"].get("llm"))
    tts = sorted(r["ttfb"]["tts"] for r in rows if r["ttfb"].get("tts"))
    ids = {r["call_id"] for r in rows}
    qi = [i for i in _queue_items() if i["call_id"] in ids]  # same filter as the rest of the dashboard
    queue = {
        "pending": sum(1 for i in qi if i["status"] == "pending"),
        "assigned": sum(1 for i in qi if i["status"] in ("assigned", "in_progress")),
        "closed": sum(1 for i in qi if i["status"] == "closed"),
        "by_priority": dict(Counter(f"P{i['priority']}" for i in qi if i["status"] == "pending")),
        "by_agent": dict(Counter(next((a["name"] for a in AGENTS if a["id"] == i["assignee"]), i["assignee"]) for i in qi if i["assignee"] and i["status"] != "closed")),
    }
    return {
        "filters": {"channel": channel, "version": version, "days": days},
        "timeline": {d: dict(c) for d, c in sorted(timeline.items())},
        "timeline_hourly": {h: dict(c) for h, c in sorted(hourly.items())[-24:]},
        "funnel": funnel,
        "guardrail_kinds": dict(kinds.most_common()),
        "violations": sum(r["violations"] for r in rows),
        "latency": {"llm_p50": llm[len(llm) // 2] if llm else None, "tts_p50": tts[len(tts) // 2] if tts else None,
                    "llm_p90": llm[int(len(llm) * 0.9)] if llm else None, "tts_p90": tts[int(len(tts) * 0.9)] if tts else None},
        "queue": queue,
        "total": agg(rows),
        "by_version": by(lambda r: r["prompt_version"] or "?"),
        "by_dpd": by(lambda r: r["customer"].get("dpd_bucket", "?")),
        "by_balance": by(lambda r: r["customer"].get("balance_band", "?")),
        "by_language": by(lambda r: r["language_mix"] or "?"),
        "by_channel": by(lambda r: r["channel"]),
        "recent": rows[:15],
    }


def _group(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[key(r)].append(r)
    return g


# --------------------------------------------------------------------------- work queue
HUMAN_REASONS = {"waiver_request", "settlement_request", "do_not_call", "otp_failed", "unresolved", "customer_busy", "third_party_answered", "wrong_number"}
PRIORITY = {"abusive": 1, "medical_emergency": 1, "bereavement": 1, "job_loss": 2, "human_requested": 2, "fraud_claim": 2, "billing_dispute": 3,
            "settlement_request": 2, "waiver_request": 3, "do_not_call": 2, "unresolved": 3, "otp_failed": 3, "customer_busy": 4, "third_party_answered": 4, "wrong_number": 5}


def _needs_human(r: dict) -> bool:
    return r["outcome"] == "dispute_or_transfer" or (r["outcome"] == "callback_request" and r["reason"] in HUMAN_REASONS)


def _queue_items() -> list[dict]:
    state = _read_json(QUEUE_FILE, {})
    items = []
    for r in (summarise_call(c) for c in load_calls() if c.get("ended", True)):
        if not _needs_human(r):
            continue
        st = state.get(r["call_id"], {})
        kind = "transfer" if r["outcome"] == "dispute_or_transfer" else "callback"
        items.append({
            **r,
            "kind": kind,
            "priority": PRIORITY.get(r["reason"], 3),
            "assignee": st.get("assignee"),
            "status": st.get("status", "pending"),
            "agent_note": st.get("note", ""),
            "updated_at": st.get("updated_at"),
            "suggested_action": _suggested_action(r),
        })
    items.sort(key=lambda i: (i["status"] != "pending", i["priority"], i["started_at"] or ""), reverse=False)
    return items


def _suggested_action(r: dict) -> str:
    reason = r["reason"]
    return {
        "abusive": "Supervisor review before any further contact; check for complaint risk.",
        "medical_emergency": "Pause collections; empathetic call from a senior agent, offer hardship options.",
        "bereavement": "Pause collections; route to estate / bereavement process. Do not call the family about payment.",
        "job_loss": "Hardship assessment call; discuss restructuring within policy.",
        "human_requested": "Customer refused the bot. Human call-back within the promised window.",
        "fraud_claim": "Open a dispute case; block further collection calls until resolved.",
        "billing_dispute": "Review the disputed charge; call back with the statement.",
        "settlement_request": "Customer proposed a settlement. Agent with settlement authority to call back.",
        "waiver_request": "Late-fee waiver asked. Decide per policy and call back; bot made no promise.",
        "do_not_call": "Register the do-not-call preference; confirm in writing if required.",
        "unresolved": "Call ended without an outcome; re-dial and complete.",
        "otp_failed": "Verification failed twice; re-dial with alternate verification.",
        "customer_busy": f"Call back at the agreed time ({r.get('callback_time') or 'not captured'}).",
        "third_party_answered": "Card holder not reached; re-dial later, do not discuss with third parties.",
        "wrong_number": "Wrong number: flag the contact in CRM and source a correct number before any re-dial.",
    }.get(reason, "Review the call and follow up.")


@app.get("/api/queue")
async def queue(status: str = "all"):
    items = _queue_items()
    if status != "all":
        items = [i for i in items if i["status"] == status]
    return items


@app.post("/api/queue/auto_assign")
async def auto_assign():
    """Round-robin pending items to agents, matching language where it matters."""
    state = _read_json(QUEUE_FILE, {})
    pending = [i for i in _queue_items() if i["status"] == "pending" and not i["assignee"]]
    load = Counter(v.get("assignee") for v in state.values() if v.get("status") in ("assigned", "in_progress"))
    assigned = 0
    for item in pending:
        if item["reason"] in ("fraud_claim", "billing_dispute"):
            pick = "disputes"
        else:
            lang = item["language_mix"] or "english"
            candidates = [a for a in AGENTS if a["id"] != "disputes" and lang in a["languages"]] or [a for a in AGENTS if a["id"] != "disputes"]
            pick = min(candidates, key=lambda a: load[a["id"]])["id"]
        load[pick] += 1
        state[item["call_id"]] = {**state.get(item["call_id"], {}), "assignee": pick, "status": "assigned", "updated_at": datetime.now(timezone.utc).isoformat()}
        assigned += 1
    _write_json(QUEUE_FILE, state)
    return {"assigned": assigned}


@app.post("/api/queue/{call_id}")
async def update_queue(call_id: str, req: Request):
    body = await req.json()
    state = _read_json(QUEUE_FILE, {})
    entry = state.get(call_id, {})
    for k in ("assignee", "status", "note"):
        if k in body:
            entry[k] = body[k]
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    state[call_id] = entry
    _write_json(QUEUE_FILE, state)
    return {"ok": True, "state": entry}


# --------------------------------------------------------------------------- eval results
def _result_dirs() -> list[Path]:
    return sorted([p for p in RESULTS.iterdir() if p.is_dir() and (p / "results.csv").exists()], reverse=True)


@app.get("/api/eval/runs")
async def eval_runs():
    out = []
    for d in _result_dirs():
        with (d / "results.csv").open() as f:
            rows = list(csv.DictReader(f))
        versions = sorted({r["version"] for r in rows})
        out.append({"id": d.name, "versions": versions, "scenarios": len({r["scenario_id"] for r in rows}), "rows": len(rows)})
    return out


@app.get("/api/eval/{run_id}")
async def eval_run(run_id: str):
    d = RESULTS / run_id
    if not (d / "results.csv").exists():
        raise HTTPException(404, "no such run")
    with (d / "results.csv").open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        t = d / "transcripts" / f"{r['scenario_id']}_{r['version']}.txt"
        r["transcript"] = t.read_text() if t.exists() else ""
        for k in ("delivered_fail", "raw_fail", "outcome_ok", "forced_transfer", "balance_before_otp"):
            r[k] = r.get(k) == "True"
    summary = (d / "summary.md").read_text() if (d / "summary.md").exists() else ""
    return {"id": run_id, "rows": rows, "summary": summary}


@app.get("/api/scenarios")
async def scenarios():
    return [s.__dict__ for s in Scenario.load_all(SCENARIOS)]


# --------------------------------------------------------------------------- run a scenario on demand
@app.post("/api/run_scenario")
async def run_scenario_job(req: Request):
    body = await req.json()
    sid = body.get("scenario_id")
    versions = [v for v in body.get("versions", ["v1", "v3"]) if v in PROMPT_VERSIONS]
    use_judge = bool(body.get("judge", False))
    sc = next((s for s in Scenario.load_all(SCENARIOS) if s.id == sid), None)
    if not sc or not versions:
        raise HTTPException(400, "unknown scenario or versions")
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "running", "scenario": sc.__dict__, "results": {}, "started_at": datetime.now(timezone.utc).isoformat()}

    async def one(version: str):
        settings = get_settings(prompt_version=version)
        settings.webhook_url = ""
        bot_client = client_from_settings(settings, "bot")
        cust_client = client_from_settings(settings, "customer")
        res = await run_scenario(sc, settings, bot_client, cust_client, today=date.today())
        score = deterministic(sc, res)
        if use_judge and not res.error:
            try:
                score.judge = await judge(sc, res, client_from_settings(settings, "judge"))
            except Exception as e:  # noqa: BLE001
                score.judge_error = str(e)[:200]
        score = combine(score)
        jobs[job_id]["results"][version] = {"score": score.to_row(), "transcript": transcript_text(res), "error": res.error}

    async def runner():
        try:
            await asyncio.gather(*(one(v) for v in versions))
            jobs[job_id]["status"] = "done"
        except Exception as e:  # noqa: BLE001
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)

    asyncio.create_task(runner())
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "no such job")
    return jobs[job_id]


if __name__ == "__main__":
    print("Mumbai Bank Collections Console → http://localhost:8787   (webhook: POST /outcome)")
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
