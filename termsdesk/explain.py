"""C · Explain: the terms letter and the decision note, with a citation guard.

The letter is drafted (by Claude Sonnet when a key is set, otherwise by a fixed template)
from a JSON of the decision, the evidence ledger and the parsed request fields, and
nothing else: the raw request text never reaches the model.

The citation guard pulls every amount, number, percentage, date and name out of a
draft. Each must match the decision, an evidence value or a request field. On a
mismatch the model gets one retry with the offending items listed; after that the
template is used. Unavailable facts are listed as "Could not verify: ...".
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional

from .intake import find_amounts

SELLER = {"name": "Acme Supplies", "about": "an IT hardware distributor in Bengaluru"}

SIGNAL_LABELS = {
    "company_identity": "company identity", "exposure": "existing exposure", "domain_check": "email domain",
    "sender_check": "sender's role at the company", "finance_head": "finance head",
    "buyer_confirmation": "buyer confirmation", "ship_to_check": "ship-to city",
    "joiner_rate": "joiners in the last 12 months", "joiners_12m": "joiners in the last 12 months",
    "headcount_trend": "headcount trend", "finance_head_exit": "finance head exit",
    "finance_team_exits": "finance team exits", "clevel_exits": "other C-level exits",
    "c_level_exits": "other C-level exits", "funding_age": "funding age", "distress_news": "distress news",
    "company_age": "company age", "job_posts": "job posts", "job_posts_12m": "job posts",
    "website": "website", "website_reachable": "website",
}

SOURCE_LABELS = [
    ("company/identify", "Company Identify"), ("company/enrich", "Company Enrich"),
    ("person/search", "Person Search"), ("person/enrich", "Person Enrich"), ("job/search", "Job Search"),
    ("web/search", "Web Search"), ("web/enrich", "Web Fetch"), ("accounts", "Accounts ledger"),
    ("out-of-band", "Finance head confirmation"), ("request", "Request"),
]

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_FULL = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
               "october", "november", "december"]


# --------------------------------------------------------------------------- formatting

def format_inr(amount: Optional[float]) -> str:
    """1800000 -> '₹18 lakh'; 15000000 -> '₹1.5 crore'; 75000 -> '₹75,000'."""
    if amount is None:
        return "not stated"
    a = float(amount)
    if a >= 1e7:
        return f"₹{_trim(a / 1e7)} crore"
    if a >= 1e5:
        return f"₹{_trim(a / 1e5)} lakh"
    n = int(round(a))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        s = f"{head},{tail}"
    return f"₹{s}"


def _trim(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def format_date(value: Any) -> Optional[str]:
    """'2026-11-06' -> '6 Nov 2026'; '2023-09' -> 'Sep 2023'."""
    if value is None:
        return None
    s = value.isoformat() if hasattr(value, "isoformat") else str(value)
    m = re.match(r"(\d{4})-(\d{2})(?:-(\d{2}))?", s)
    if not m:
        return s
    y, mo, d = int(m.group(1)), int(m.group(2)), m.group(3)
    return f"{int(d)} {_MONTHS[mo - 1]} {y}" if d else f"{_MONTHS[mo - 1]} {y}"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(vars(obj))


def source_label(source: Optional[str]) -> str:
    s = (source or "").lower()
    for prefix, label in SOURCE_LABELS:
        if prefix in s:
            return label
    if s.startswith("http"):
        return re.sub(r"^https?://(www\.)?", "", s).split("/")[0]
    return source or "source"


# --------------------------------------------------------------------------- citation guard

_RE_DATE_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTH_FULL + [m.lower() for m in _MONTHS])
                          + r")[a-z]*\.?,?\s+(\d{4})\b", re.I)
_RE_DATE_MY = re.compile(r"\b(" + "|".join(_MONTH_FULL + [m.lower() for m in _MONTHS]) + r")[a-z]*\.?,?\s+(\d{4})\b", re.I)
_RE_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})(?:-(\d{2}))?(?:T[\d:.]+Z?)?\b")
_RE_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_RE_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")
_RE_NUM_WORD = re.compile(r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|"
                          r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)\b", re.I)
_NUM_WORD_VALUE = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                   "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
                   "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
                   "hundred": 100}
_RE_CAPWORD = re.compile(r"\b[A-Z][A-Za-z'\-]*\b")
_HONORIFICS = frozenset({"mr", "ms", "mrs", "dr", "shri", "smt", "sir"})
_RE_SENTENCE = re.compile(r"(?<!\bMr\.)(?<!\bMs\.)(?<!\bDr\.)(?<!Mrs\.)(?<=[.!?:])\s+")

ALLOWED_WORDS = frozenset("""
dear hello hi regards thanks thank sincerely best kind warm team credit desk acme supplies it hardware
distributor bengaluru we our you your the this that these it if as for in on at to of and or but with please
subject re note could not verify verified what why how based terms term limit line payment advance upfront
order orders invoice invoices total outstanding review reviewed approve approved reduce reduced decline
declined hold held refer referred joiners headcount finance financial head chief officer controller director
vice president vp cfo ceo coo cto procurement manager buyer sender person search company enrich identify job
web fetch news policy months month days day lakh crore rupees inr rs india indian dispatch before after a an
reasons reason summary decision our offer offered key ledger accounts letter sent once confirm deny yes no
mr ms
""".split())


def _walk(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _walk(v)
    elif value is not None:
        yield value


@dataclass
class Facts:
    amounts: set = field(default_factory=set)
    numbers: set = field(default_factory=set)
    dates_ymd: set = field(default_factory=set)
    dates_ym: set = field(default_factory=set)
    years: set = field(default_factory=set)
    percents: set = field(default_factory=set)
    words: set = field(default_factory=set)


def _add_number(f: Facts, x: float) -> None:
    for v in {x, round(x), round(x, 1), round(x, 2)}:
        f.numbers.add(float(v))
    if 0 < abs(x) < 1:                                 # ratios may be shown as percentages
        for v in {round(x * 100), round(x * 100, 1)}:
            f.numbers.add(float(v))
            f.percents.add(float(v))


def _add_amount(f: Facts, x: float) -> None:
    f.amounts.add(int(round(x)))
    rounded = find_amounts(format_inr(x))
    if rounded:
        f.amounts.add(rounded[0].value)
    _add_number(f, x)


def _add_text(f: Facts, s: str) -> None:
    f.words.update(w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", s))
    for m in _RE_PERCENT.finditer(s):
        f.percents.add(float(m.group(1)))
    for a in find_amounts(s):
        _add_amount(f, a.value)
    for m in _RE_DATE_ISO.finditer(s):
        y, mo, d = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= mo <= 12:
            f.years.add(y)
            f.dates_ym.add((y, mo))
            if d:
                f.dates_ymd.add((y, mo, int(d)))
    for m in _RE_DATE_DMY.finditer(s):
        y, mo, d = int(m.group(3)), _month_index(m.group(2)), int(m.group(1))
        f.years.add(y); f.dates_ym.add((y, mo)); f.dates_ymd.add((y, mo, d))
    for m in _RE_DATE_MY.finditer(s):
        y, mo = int(m.group(2)), _month_index(m.group(1))
        f.years.add(y); f.dates_ym.add((y, mo))
    for m in _RE_NUMBER.finditer(s):
        try:
            _add_number(f, float(m.group(0).replace(",", "")))
        except ValueError:
            pass


def _month_index(token: str) -> int:
    t = token.lower()[:3]
    return [m.lower() for m in _MONTHS].index(t) + 1


def build_facts(payload: dict) -> Facts:
    """Everything a letter may state, gathered from the decision, evidence and request fields."""
    f = Facts()
    amount_keys = ("inr", "amount", "limit", "exposure", "outstanding")
    def visit(obj: Any, key: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                visit(v, str(k))
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                visit(v, key)
        elif isinstance(obj, bool) or obj is None:
            return
        elif isinstance(obj, (int, float)):
            if any(k in key.lower() for k in amount_keys):
                _add_amount(f, obj)
            if "pct" in key.lower() or "percent" in key.lower():
                f.percents.add(float(obj))
            _add_number(f, obj)
        else:
            _add_text(f, str(obj))
    visit(payload)
    for y in list(f.years):
        _add_number(f, y)
    return f


@dataclass
class GuardResult:
    ok: bool
    bad: list[str]


def citation_guard(text: str, facts: Facts) -> GuardResult:
    bad: list[str] = []
    work = text
    spans: list[tuple[int, int]] = []

    def blank(s: int, e: int) -> None:
        spans.append((s, e))

    for a in find_amounts(work):
        if a.value not in facts.amounts:
            bad.append(a.text.strip())
        blank(a.start, a.end)
    for rx, kind in ((_RE_DATE_DMY, "dmy"), (_RE_DATE_ISO, "iso"), (_RE_DATE_MY, "my")):
        for m in rx.finditer(work):
            if any(s <= m.start() < e for s, e in spans):
                continue
            if kind == "dmy":
                key = (int(m.group(3)), _month_index(m.group(2)), int(m.group(1)))
                ok = key in facts.dates_ymd
            elif kind == "iso":
                y, mo, d = int(m.group(1)), int(m.group(2)), m.group(3)
                ok = (y, mo, int(d)) in facts.dates_ymd if d else (y, mo) in facts.dates_ym
            else:
                ok = (int(m.group(2)), _month_index(m.group(1))) in facts.dates_ym
            if not ok:
                bad.append(m.group(0))
            blank(m.start(), m.end())
    for m in _RE_PERCENT.finditer(work):
        if any(s <= m.start() < e for s, e in spans):
            continue
        if float(m.group(1)) not in facts.percents:
            bad.append(m.group(0))
        blank(m.start(), m.end())
    for m in _RE_NUMBER.finditer(work):
        if any(s <= m.start() < e for s, e in spans):
            continue
        try:
            v = float(m.group(0).replace(",", "").rstrip("."))
        except ValueError:
            continue
        if v not in facts.numbers:
            bad.append(m.group(0))
    for m in _RE_NUM_WORD.finditer(work):
        if float(_NUM_WORD_VALUE[m.group(1).lower()]) not in facts.numbers:
            bad.append(m.group(0))
    # Names: capitalised words that are neither in the facts nor ordinary letter words.
    for line in work.splitlines():
        for sentence in _RE_SENTENCE.split(line):
            words = list(_RE_CAPWORD.finditer(sentence))
            for i, w in enumerate(words):
                token = w.group(0).lower().strip("'-")
                after_title = i > 0 and words[i - 1].group(0).lower() in _HONORIFICS
                if not after_title and (token in facts.words or token in ALLOWED_WORDS or token in _MONTH_FULL
                                        or token[:3] in [x.lower() for x in _MONTHS] or token in _HONORIFICS):
                    continue
                first = not after_title and (w.start() == len(sentence) - len(sentence.lstrip())
                                             or sentence[:w.start()].strip() in ("-", "•", "*"))
                next_is_cap = i + 1 < len(words) and words[i + 1].start() - w.end() <= 1
                if first and not next_is_cap:
                    continue                           # an ordinary word that starts a sentence
                bad.append(w.group(0))
    return GuardResult(ok=not bad, bad=list(dict.fromkeys(bad)))


# --------------------------------------------------------------------------- note (case page)

def _evidence_rows(evidence: Iterable[Any]) -> list[dict]:
    return [_as_dict(e) for e in evidence or []]


def could_not_verify(evidence: Iterable[Any]) -> list[str]:
    lines = []
    for e in _evidence_rows(evidence):
        if e.get("status") not in ("unavailable", "invalid", "conflict"):
            continue
        label = SIGNAL_LABELS.get(e.get("signal") or "", (e.get("signal") or "a fact").replace("_", " "))
        v = e.get("value")
        detail = None
        if isinstance(v, dict):
            detail = v.get("error") or v.get("reason") or v.get("detail")
        elif isinstance(v, str):
            detail = v
        if not detail:
            detail = {"invalid": "failed a sanity check", "conflict": "sources disagree"}.get(e.get("status"))
        lines.append(f"Could not verify: {label}" + (f" ({detail})" if detail else ""))
    return lines


def _cite(e: dict) -> str:
    when = format_date(e.get("as_of"))
    return f"({source_label(e.get('source'))}{', ' + when if when else ''})"


def verification_reasons(evidence: Iterable[Any], company_name: Optional[str]) -> list[dict]:
    """Case-page lines for the buyer checks (Part C's own evidence)."""
    out = []
    rows = {e.get("signal"): e for e in _evidence_rows(evidence)}
    sc, dc = rows.get("sender_check"), rows.get("domain_check")
    if dc and isinstance(dc.get("value"), dict):
        v = dc["value"]
        if v.get("result") == "lookalike":
            out.append({"text": f"Email domain {v.get('sender_domain')} imitates {v.get('primary_domain')}",
                        "evidence_ids": [dc["id"]]})
        elif v.get("result") == "unverified":
            out.append({"text": f"Email domain not verified: {v.get('detail')}", "evidence_ids": [dc["id"]]})
    domain_ok = bool(dc and isinstance(dc.get("value"), dict) and dc["value"].get("result") == "pass")
    if sc and isinstance(sc.get("value"), dict) and sc.get("status") == "verified":
        v = sc["value"]
        if v.get("result") == "pass" and domain_ok:
            out.append({"text": f"Buyer verified: {v.get('title')} at {company_name or 'the company'}",
                        "evidence_ids": [sc["id"], dc["id"]]})
        elif v.get("result") == "pass":
            out.append({"text": f"The sender's name matches a current {v.get('title')} at "
                                f"{company_name or 'the company'}, but the email domain is not the company's",
                        "evidence_ids": [sc["id"]] + ([dc["id"]] if dc else [])})
        elif v.get("result") == "note":
            out.append({"text": f"Sender works at {company_name or 'the company'} as {v.get('title')}, not a buying role",
                        "evidence_ids": [sc["id"]]})
    return out


def headline(decision: Any, request: Any = None) -> str:
    kind = _get(decision, "decision")
    limit, days = _get(decision, "limit_inr"), _get(decision, "terms_days")
    req_amt, req_days = _get(request, "amount_inr"), _get(request, "terms_days")
    asked = ""
    if req_amt or req_days:
        asked = f" (requested {format_inr(req_amt) if req_amt else 'no amount'}" + \
                (f" on {req_days} days)" if req_days else ")")
    if kind == "approve":
        if req_amt is None:
            return f"Approve up to {format_inr(limit)} on {days} days (no amount requested: maximum safe limit)"
        return f"Approve {format_inr(limit)} on {days} days" + (asked if (limit != req_amt or days != req_days) else "")
    if kind == "reduce":
        return f"Reduce to {format_inr(limit)} on {days} days" + asked
    if kind == "advance_only":
        pct = _get(decision, "advance_pct") or 100
        return f"Advance payment only ({pct}% before dispatch)" + asked
    if kind == "hold_verify":
        return "Hold: verify the buyer out of band before any credit" + asked
    if kind == "refer":
        return "Refer to a credit reviewer" + asked
    return "Decline credit" + asked


@dataclass
class Note:
    headline: str
    reasons: list[dict]
    could_not_verify: list[str]
    footer: str


def build_note(decision: Any, evidence: Iterable[Any], request: Any = None, company_name: Optional[str] = None,
               letter_key: Optional[str] = None, credits: Optional[float] = None) -> Note:
    rows = {e["id"]: e for e in _evidence_rows(evidence) if e.get("id")}
    reasons = []
    for r in list(_get(decision, "reasons") or []) + verification_reasons(evidence, company_name):
        text, ids = _get(r, "text"), list(_get(r, "evidence_ids") or [])
        if any(text == x["text"] for x in reasons):
            continue
        cites = [_cite(rows[i]) for i in ids if i in rows]
        sources = [{"label": source_label(rows[i].get("source")), "as_of": rows[i].get("as_of"),
                    "url": rows[i].get("url")} for i in ids if i in rows]
        reasons.append({"text": text, "evidence_ids": ids, "sources": sources,
                        "display": text + (" " + " ".join(dict.fromkeys(cites)) if cites else "")})
    footer = [f"Policy {_get(decision, 'policy_version') or 'v?'}"]
    if credits is not None:
        footer.append(f"{credits:.1f} credits")
    if letter_key:
        footer.append(f"letter key {letter_key}")
    return Note(headline(decision, request), reasons, could_not_verify(evidence), " · ".join(footer))


# --------------------------------------------------------------------------- letters

@dataclass
class Letter:
    key: str
    to_intended: Optional[str]
    subject: str
    body: str
    method: str                       # template | model | model_retry


def letter_key(case_id: str, version: Any = 1, action: str = "terms") -> str:
    v = str(version)
    return f"{case_id}:{action}:{v if v.startswith('v') else 'v' + v}"


def _greeting(request: Any) -> str:
    name = _get(request, "sender_name")
    return f"Dear {name.split()[0]}," if name else "Dear Accounts team,"


def template_letter(decision: Any, evidence: Iterable[Any], request: Any, company_name: str) -> tuple[str, str]:
    kind = _get(decision, "decision")
    limit, days = _get(decision, "limit_inr"), _get(decision, "terms_days")
    pct = _get(decision, "advance_pct") or 100
    req_amt, req_days = _get(request, "amount_inr"), _get(request, "terms_days")
    reasons = [_get(r, "text") for r in (_get(decision, "reasons") or []) if _get(r, "text")]
    gaps = could_not_verify(evidence)
    lines = [_greeting(request), "", f"Thank you for your request to buy from {SELLER['name']} on credit."]
    if kind == "approve":
        subject = f"Credit terms for {company_name}: {format_inr(limit)} on {days} days"
        lines.append(f"We can offer {company_name} a credit line of {format_inr(limit)} on {days}-day terms.")
    elif kind == "reduce":
        subject = f"Credit terms for {company_name}: {format_inr(limit)} on {days} days"
        asked = f"{format_inr(req_amt)} on {req_days} days" if req_amt and req_days else \
            (format_inr(req_amt) if req_amt else "a larger line")
        lines.append(f"We can offer {company_name} a credit line of {format_inr(limit)} on {days}-day terms. "
                     f"You asked for {asked}; we can review a higher limit later.")
    elif kind == "advance_only":
        subject = f"Order terms for {company_name}: advance payment"
        lines.append(f"We can supply this order on advance payment: {pct}% before dispatch, "
                     f"with no credit line for now.")
    elif kind == "decline":
        subject = f"Your credit request with {SELLER['name']}"
        lines.append("We are unable to offer credit on this order.")
    else:                                              # refer: a neutral acknowledgement only
        subject = f"Your credit request with {SELLER['name']} is under review"
        lines.append("Your request is with our credit team for review. We will reply once the review is done.")
        reasons, gaps = [], []
    if reasons:
        lines += ["", "What this is based on:"] + [f"- {r}" for r in reasons]
    if gaps:
        lines += [""] + gaps
    if kind in ("approve", "reduce"):
        lines += ["", "The limit covers everything outstanding with us, including earlier invoices."]
    lines += ["", "Regards,", f"Credit desk, {SELLER['name']}"]
    return subject, "\n".join(lines)


_LETTER_SYSTEM = (
    "You write short, plain business letters for the credit desk of Acme Supplies, an IT hardware distributor "
    "in Bengaluru. Use only the facts in the JSON you are given. Never add a number, amount, percentage, date or "
    "name that is not in the JSON. Write amounts the way they appear in display_amounts (for example '₹18 lakh'). "
    "State the decision first, then the reasons, then anything listed under could_not_verify, word for word. "
    "No markdown. Under 170 words. Output the first line as 'Subject: ...', then a blank line, then the body."
)


LAST_LETTER_MODEL: dict = {"name": None}


def _model_letter(payload: dict, feedback: Optional[list[str]] = None) -> Optional[tuple[str, str]]:
    """Gemini via termsdesk.llm (integration: Part C called Claude; the team uses Gemini)."""
    from . import llm
    if not llm.available():
        return None
    content = json.dumps(payload, ensure_ascii=False, default=str)
    if feedback:
        content += ("\n\nYour last draft used these items, which are not in the facts: "
                    + "; ".join(feedback) + ". Rewrite it without them.")
    text, model = llm.generate("letter_c_v1", _LETTER_SYSTEM + "\n\nFacts (JSON):\n" + content)
    if not isinstance(text, str):
        return None
    LAST_LETTER_MODEL["name"] = model
    m = re.match(r"\s*subject:\s*(.+?)\n\s*\n(.*)", text.strip(), re.I | re.S)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def _payload(decision: Any, evidence: Iterable[Any], request: Any, company_name: str) -> dict:
    d = _as_dict(decision)
    req = _as_dict(request)
    case_fields = {k: req.get(k) for k in ("company_name", "sender_name", "amount_inr", "terms_days", "goods",
                                            "ship_to_city")}
    display = {k: format_inr(v) for k, v in (("limit", d.get("limit_inr")), ("requested", req.get("amount_inr")),
                                             ("exposure", d.get("exposure_inr"))) if v}
    rows = []
    for e in _evidence_rows(evidence):
        rows.append({k: e.get(k) for k in ("id", "signal", "value", "unit", "source", "as_of", "status")})
    return {"seller": SELLER, "buyer_company": company_name, "decision": d, "request": case_fields,
            "display_amounts": display, "evidence": rows, "could_not_verify": could_not_verify(evidence)}


def _missing_content(draft: str, template_body: str, decision: Any, evidence: Iterable[Any]) -> list[str]:
    """Completeness check: the draft must state the decided amount and keep every 'Could not verify' line,
    and must not be much shorter than the template (a cut-off draft contains no wrong facts, only fewer)."""
    out = []
    limit = _get(decision, "limit_inr")
    if _get(decision, "decision") in ("approve", "reduce") and limit:
        if not any(a.value == int(limit) for a in find_amounts(draft)):
            out.append(f"missing the decided limit {format_inr(limit)}")
    for line in could_not_verify(evidence):
        label = line.split(":", 1)[1].split("(")[0].strip().lower()
        if label and label not in draft.lower():
            out.append(f"dropped '{line}'")
    if len(draft) < 0.6 * len(template_body):
        out.append("draft much shorter than the facts it must cover (cut off?)")
    return out


@dataclass
class Explanation:
    letter: Optional[Letter]
    note: Note
    guard_log: list[dict] = field(default_factory=list)


def explain(decision: Any, evidence: Iterable[Any], request: Any = None, company_name: Optional[str] = None,
            case_id: Optional[str] = None, version: Any = 1, credits: Optional[float] = None,
            use_model: Optional[bool] = None) -> Explanation:
    """Letter + note for one decision. A hold produces no buyer letter (verify.py writes the
    finance-head email instead), and we never reply to the sender in a hold."""
    evidence = list(evidence or [])
    case_id = case_id or _get(request, "case_id") or "case"
    company = company_name or _get(request, "company_name") or "your company"
    kind = _get(decision, "decision")
    key = letter_key(case_id, version)
    note = build_note(decision, evidence, request, company, key if kind != "hold_verify" else None, credits)
    if kind == "hold_verify":
        return Explanation(letter=None, note=note)

    payload = _payload(decision, evidence, request, company)
    facts = build_facts(payload)
    log: list[dict] = []
    subject, body = template_letter(decision, evidence, request, company)
    method = "template"
    want_model = use_model if use_model is not None else os.environ.get("TERMSDESK_USE_MODEL", "1") != "0"
    if want_model and kind != "refer":
        feedback = None
        for attempt in ("model", "model_retry"):
            draft = _model_letter(payload, feedback)
            if draft is None:
                break
            g = citation_guard(draft[0] + "\n" + draft[1], facts)
            missing = _missing_content(draft[1], body, decision, evidence)
            if missing:  # a truncated or thin draft passes the fact check but drops reasons: reject it too
                g = GuardResult(ok=False, bad=g.bad + missing)
            log.append({"attempt": attempt, "ok": g.ok, "rejected": g.bad})
            if g.ok:
                subject, body, method = draft[0], draft[1], attempt
                break
            feedback = g.bad
    final = citation_guard(subject + "\n" + body, facts)
    log.append({"attempt": "final:" + method, "ok": final.ok, "rejected": final.bad})
    letter = Letter(key=key, to_intended=_get(request, "sender_email"), subject=subject, body=body, method=method)
    return Explanation(letter=letter, note=note, guard_log=log)


# --------------------------------------------------------------------------- pipeline adapter (role A)

def write(case_id: str, req: Any, company: Any, decision: Any) -> tuple[str, str, str]:
    """Called by pipeline.step_explain. Returns (subject, body, written_by)."""
    from . import store
    version, _ = store.latest_decision(case_id)
    LAST_LETTER_MODEL["name"] = None
    out = explain(decision, store.list_evidence(case_id), req, getattr(company, "name", None), case_id=case_id,
                  version=version, credits=store.case_cost(case_id))
    if out.letter is None:  # holds write to the finance head instead (outbox.start_hold)
        return "On hold", "\n".join([out.note.headline] + [r["display"] for r in out.note.reasons]), "note only"
    final = out.guard_log[-1] if out.guard_log else {}
    by = out.letter.method
    if by.startswith("model"):
        by = f"{LAST_LETTER_MODEL['name'] or 'model'} ({by}), passed citation guard"
    elif any(not g.get("ok", True) for g in out.guard_log[:-1]):
        rejected = [x for g in out.guard_log[:-1] for x in g.get("rejected", [])]
        by = f"template (citation guard rejected the model draft: {', '.join(rejected[:4])})"
    if not final.get("ok", True):
        by += f"; WARNING final guard flagged {final.get('rejected')}"
    return out.letter.subject, out.letter.body, by
