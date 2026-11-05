"""Send once. Every message has a key (case:action:version); a resend after a crash is a no-op.
Recipients outside the team are routed to the sandbox inbox (event rule: agents only talk to our team)."""
from __future__ import annotations

from typing import Optional

from . import config, store
from .contracts import Company, Request, VerifyResult
from .money import inr


def route(to: Optional[str]) -> str:
    if to and "@" in to and to.split("@", 1)[1].lower() in config.TEAM_DOMAINS:
        return to
    return config.SANDBOX_INBOX


def _deliver(key: str) -> None:
    """Delivery is simulated: the sandbox inbox is the outbox page. Plug a real mailer in here later."""
    store.outbox_mark_sent(key)


def send_decision(case_id: str, version: int, req: Request, letter: dict) -> bool:
    key = f"{case_id}:decision:v{version}"
    intended = req.sender_email or f"{req.sender_name or 'buyer'} (no email given)"
    queued = store.outbox_enqueue(key, case_id, "decision", intended, route(req.sender_email), letter["subject"], letter["body"])
    _deliver(key)  # idempotent: marks sent only once
    return queued


def start_hold(case_id: str, version: int, req: Request, company: Company, vr: VerifyResult) -> None:
    head = vr.finance_head
    if not head:
        store.queue_human(case_id, "hold_verify", f"No finance head found at {company.name}; verify the order by phone")
        return
    key = f"{case_id}:verify:v{version}"
    intended = f"{head['name']} ({head['title']}, {company.name}); email not looked up"
    subject = f"Please confirm an order placed in {company.name}'s name"
    body = "\n".join([
        f"Dear {head['name']},", "",
        f"{config.SELLER_NAME} received a request for credit in {company.name}'s name:",
        f"- Amount: {inr(req.amount_inr)}" + (f" on {req.terms_days} days" if req.terms_days else ""),
        f"- Goods: {req.goods or 'not stated'}" + (f", to be shipped to {req.ship_to_city}" if req.ship_to_city else ""),
        f"- Sent by: {req.sender_name or 'unnamed'} <{req.sender_email or 'no email'}>", "",
        "We could not confirm this request came from your company:",
        *[f"- {s}" for s in vr.summary], "",
        "Please confirm or deny this order. We have not shipped anything and will not extend credit until you reply.",
        "We are writing to you directly because you were found independently of the sender; we have not replied to them.", "",
        f"Credit desk, {config.SELLER_NAME}",
    ])
    store.outbox_enqueue(key, case_id, "verify", intended, route(None), subject, body)
    _deliver(key)
