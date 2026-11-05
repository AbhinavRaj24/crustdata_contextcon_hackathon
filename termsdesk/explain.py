"""Write the terms letter. The template is built only from the decision and its evidence; the model may
re-word it, but a citation guard rejects any number that is not in the template, and we fall back."""
from __future__ import annotations

import re

from . import config, llm
from .contracts import Company, Decision, Request
from .money import inr

HEADLINE = {
    "approve": "Approved: {limit} on {terms} days",
    "reduce": "Approved with a lower limit: {limit} on {terms} days",
    "advance_only": "Advance payment required",
    "hold_verify": "On hold pending verification",
    "refer": "Under review",
    "decline": "Declined",
}


def template(req: Request, company: Company, d: Decision) -> tuple[str, str]:
    head = HEADLINE[d.decision].format(limit=inr(d.limit_inr), terms=d.terms_days)
    subject = f"{config.SELLER_NAME}: credit terms for {company.name} ({head})"
    lines = [f"Dear {req.sender_name or 'Procurement team'},", ""]
    asked = f"{inr(d.requested_inr)}" + (f" on {d.requested_terms} days" if d.requested_terms else "")
    if req.goods:
        asked += f" for {req.goods}"
    lines.append(f"Thank you for your request for {asked}.")
    if d.decision == "approve":
        lines.append(f"We are pleased to offer {company.name} a credit limit of {inr(d.limit_inr)} on {d.terms_days}-day terms.")
    elif d.decision == "reduce":
        lines.append(f"We can offer {company.name} a credit limit of {inr(d.limit_inr)} on {d.terms_days}-day terms. "
                     f"Orders above this limit can be fulfilled against advance payment.")
    elif d.decision == "advance_only":
        lines.append(f"At this time we can supply {company.name} against full advance payment only.")
    elif d.decision == "decline":
        lines.append("We are unable to extend credit for this order.")
    lines += ["", "This decision is based on the following checks:"]
    for r in d.reasons[:6]:
        lines.append(f"- {r.text}")
    if d.could_not_verify:
        lines += ["", "Could not verify (not counted for or against you):"]
        lines += [f"- {c}" for c in d.could_not_verify[:5]]
    lines += ["", "We review terms as your situation changes. Reply to this email if any of these facts are out of date.", "",
              f"Credit desk, {config.SELLER_NAME}", f"(Policy {d.policy_version}, rule {d.rule.split(' ')[0]})"]
    return subject, "\n".join(lines)


NUM = re.compile(r"\d[\d,.]*")


def _numbers(text: str) -> set[str]:
    return {n.strip(".,").replace(",", "") for n in NUM.findall(text) if n.strip(".,")}


def citation_guard(draft: str, source: str) -> list[str]:
    """Numbers in the draft that do not appear in the source letter. Empty list = safe."""
    allowed = _numbers(source)
    return sorted(n for n in _numbers(draft) if n not in allowed)


POLISH = """Rewrite this credit decision letter so it reads like a courteous, professional credit analyst wrote it.
Rules: keep every fact, number, amount, date and name exactly as given; do not add any new fact, number, promise or opinion;
keep the bullet list of checks and the "Could not verify" list; plain text only, no markdown headings.

Letter:
<<<
{letter}
>>>"""


def write(case_id: str, req: Request, company: Company, d: Decision) -> tuple[str, str, str]:
    subject, body = template(req, company, d)
    if not llm.available():
        return subject, body, "template"
    draft, model = llm.generate("polish_letter_v1", POLISH.format(letter=body))
    if not isinstance(draft, str) or len(draft) < 0.5 * len(body):
        return subject, body, "template (model unavailable)"
    bad = citation_guard(draft, body)
    if bad:
        return subject, body, f"template (citation guard rejected {model}: unsupported numbers {', '.join(bad[:5])})"
    return subject, draft, f"{model}, passed citation guard"
