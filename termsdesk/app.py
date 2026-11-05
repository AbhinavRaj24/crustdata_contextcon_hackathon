"""Web UI: run it with  .venv/bin/uvicorn termsdesk.app:app --port 8000

Pages: / (new request)  /case/{id} (live case page)  /queue (holds and referrals)  /accounts  /outbox  /backtest
Cases run in a background thread; the case page refreshes itself until the case finishes or waits for a person.
"""
from __future__ import annotations

import threading
from pathlib import Path

import markdown
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, monitor, pipeline, store
from .crustdata import current_fault
from .money import inr

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="TermsDesk")
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.filters["inr"] = inr

RUNNING: set[str] = set()
ACTIVE = {"RECEIVED", "PARSED", "RESOLVED", "VERIFIED", "EVIDENCE_DONE", "DECIDED", "EXPLAINED", "ACTED"}
FAULTS = {"": "No failure", "enrich:timeout": "Company Enrich times out", "person_search:429": "People Search rate-limited",
          "enrich:garbage": "Company Enrich returns garbage values", "web_fetch:timeout": "Website fetch times out"}

SAMPLES = [
    ("Healthy buyer", "Hi team, Razorpay needs 40 laptops for the new Bengaluru floor. Order value Rs 30 lakh, we would like "
                      "45 days credit. Thanks, Paul Dinakar (paul.dinakar@razorpay.com)"),
    ("Dead company", "Hi, we need 50 laptops for our Bengaluru office, around ₹20 lakh, on 60 days credit please. "
                     "— procurement@dunzo.com"),
    ("Purchase-order fraud", "Hello, this is Vikram from Razorpay procurement. Please supply 80 laptops worth Rs 40 lakh on "
                             "60-day credit, ship to our new warehouse in Dubai. Regards, vikram.s@razorpay-payments.co"),
    ("Hinglish, no amount", "bhai Zepto se Ravi bol raha hu (ravi.k@zeptonow.com), 20 laptop chahiye office ke liye, "
                            "30 din ka credit mil jayega?"),
    ("Unknown small business", "Namaste, Sharma Leather Exports, Kanpur here. We have 120 employees across three units and "
                               "need packaging material worth Rs 8 lakh on 45 days credit. — Rakesh Sharma, rakesh@sharmaleatherexports.in"),
]


def _in_thread(case_id: str, fn, *args, fault: str = "") -> None:
    def work():
        RUNNING.add(case_id)
        token = current_fault.set(fault)
        try:
            fn(*args)
        finally:
            current_fault.reset(token)
            RUNNING.discard(case_id)
    threading.Thread(target=work, daemon=True).start()


def _page(request: Request, name: str, **ctx):
    ctx.setdefault("queue_count", len(store.list_queue()))
    return templates.TemplateResponse(request, name, ctx)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    cases = []
    for c in store.list_cases(30):
        if c["id"].startswith("bt-"):
            continue
        _, d = store.latest_decision(c["id"])
        cases.append({"case": c, "decision": d})
    return _page(request, "home.html", samples=SAMPLES, faults=FAULTS, cases=cases)


@app.post("/request")
def new_request(text: str = Form(...), fault: str = Form("")):
    case_id = store.create_case(text.strip())
    if fault:
        store.log_event(case_id, "FAULT", f"Demo: injected failure '{FAULTS.get(fault, fault)}' for this case")
    _in_thread(case_id, pipeline.run, case_id, fault=fault)
    return RedirectResponse(f"/case/{case_id}", status_code=303)


@app.get("/case/{case_id}", response_class=HTMLResponse)
def case_page(request: Request, case_id: str):
    case = store.get_case(case_id)
    if not case:
        return HTMLResponse("Case not found", status_code=404)
    evidence = {e.id: e for e in store.list_evidence(case_id)}
    version, decision = store.latest_decision(case_id)
    running = case_id in RUNNING or case["state"] in ACTIVE
    queue = [q for q in store.list_queue() if q["case_id"] == case_id]
    return _page(request, "case.html", case=case, events=store.list_events(case_id), evidence=evidence,
                 decision=decision, version=version, letter=store.latest_letter(case_id), outbox=store.list_outbox(case_id),
                 cost=store.case_cost(case_id), running=running, queue=queue, sandbox=config.SANDBOX_INBOX)


@app.post("/case/{case_id}/resume")
def resume(case_id: str):
    _in_thread(case_id, pipeline.resume, case_id)
    return RedirectResponse(f"/case/{case_id}", status_code=303)


@app.get("/case/{case_id}/verify")
def verify_link(case_id: str, answer: str):
    """The Confirm / Deny links inside the email to the independently found finance head."""
    case = store.get_case(case_id)
    if case and case["state"] == "HOLD_VERIFY" and answer in ("confirm", "deny"):
        _in_thread(case_id, pipeline.confirm_hold, case_id, answer == "confirm", "Finance head (via email link)")
    return RedirectResponse(f"/case/{case_id}", status_code=303)


@app.post("/case/{case_id}/simulate")
def simulate(case_id: str):
    case = store.get_case(case_id)
    if case and case.get("company_id"):
        payload = {"name": "Finance head", "title": "Head of Finance", "left": "2026-11"}
        _in_thread(case_id, monitor.simulate, case["company_id"], "finance_head_left", payload)
    return RedirectResponse(f"/case/{case_id}", status_code=303)


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    items = []
    for q in store.list_queue():
        c = store.get_case(q["case_id"]) or {}
        _, d = store.latest_decision(q["case_id"])
        items.append({"item": q, "case": c, "decision": d})
    return _page(request, "queue.html", items=items)


@app.post("/queue/{item_id}")
def resolve_item(item_id: int, action: str = Form(...), reviewer: str = Form("Credit reviewer")):
    item = next((q for q in store.list_queue() if q["id"] == item_id), None)
    if not item:
        return RedirectResponse("/queue", status_code=303)
    cid = item["case_id"]
    if item["kind"] == "hold_verify":
        _in_thread(cid, pipeline.confirm_hold, cid, action == "confirm", reviewer)
    else:
        _in_thread(cid, pipeline.approve_referral, cid, reviewer, action == "approve")
    return RedirectResponse(f"/case/{cid}", status_code=303)


@app.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request):
    rows = [a for a in store.list_accounts()]
    for a in rows:
        r = store.conn().execute("SELECT aliases_json FROM companies WHERE crustdata_id = ?", (a["company_id"],)).fetchone()
        a["aliases"] = ", ".join(__import__("json").loads(r["aliases_json"])) if r and r["aliases_json"] else ""
    return _page(request, "accounts.html", accounts=rows)


@app.get("/outbox", response_class=HTMLResponse)
def outbox(request: Request):
    return _page(request, "outbox.html", messages=store.list_outbox(), sandbox=config.SANDBOX_INBOX)


@app.get("/backtest", response_class=HTMLResponse)
def backtest(request: Request):
    p = config.ROOT / "backtest" / "results_live" / "summary.md"
    html = markdown.markdown(p.read_text(), extensions=["tables"]) if p.exists() else "<p>No backtest results yet.</p>"
    return _page(request, "backtest.html", summary=html)
