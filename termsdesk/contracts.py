"""Shapes shared between modules. Frozen at CP0: announce any change in the team chat first."""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class State(str, Enum):
    RECEIVED = "RECEIVED"
    PARSED = "PARSED"
    RESOLVED = "RESOLVED"
    VERIFIED = "VERIFIED"
    HOLD_VERIFY = "HOLD_VERIFY"
    EVIDENCE_DONE = "EVIDENCE_DONE"
    DECIDED = "DECIDED"
    EXPLAINED = "EXPLAINED"
    ACTED = "ACTED"
    REFERRED = "REFERRED"
    MONITORING = "MONITORING"
    DECLINED = "DECLINED"
    FAILED = "FAILED"


class Request(BaseModel):
    case_id: str
    raw_text: str
    company_name: Optional[str] = None
    company_domain: Optional[str] = None
    sender_name: Optional[str] = None
    sender_email: Optional[str] = None
    amount_inr: Optional[int] = None
    terms_days: Optional[int] = None
    goods: Optional[str] = None
    ship_to_city: Optional[str] = None
    parsed_by: str = "rules"  # "rules" or the model name that filled gaps


class Company(BaseModel):
    crustdata_id: int
    name: str
    primary_domain: Optional[str] = None
    website: Optional[str] = None
    employee_count_range: Optional[str] = None
    match_confidence: float = 0.0
    matched_on: str = ""  # "domain" or "name"
    candidates: list[dict] = Field(default_factory=list)


EvidenceStatus = Literal["verified", "unavailable", "invalid", "conflict"]
Level = Literal["red", "amber", "ok", "na"]  # na = could not be computed


class Evidence(BaseModel):
    id: str = ""
    case_id: str
    signal: str
    value: Any = None
    unit: Optional[str] = None
    source: str
    url: Optional[str] = None
    as_of: Optional[str] = None
    status: EvidenceStatus = "verified"
    note: Optional[str] = None
    cost: float = 0.0


class SignalResult(BaseModel):
    signal: str
    level: Level
    summary: str  # one plain sentence, numbers only from evidence
    evidence_ids: list[str] = Field(default_factory=list)


class VerifyResult(BaseModel):
    status: Literal["pass", "unverified", "fraud"]
    flags: list[str] = Field(default_factory=list)
    summary: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    finance_head: Optional[dict] = None  # {"name","title","crustdata_person_id"} found independently


class Reason(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)


DecisionKind = Literal["approve", "reduce", "advance_only", "hold_verify", "refer", "decline"]


class Decision(BaseModel):
    decision: DecisionKind
    limit_inr: int = 0
    terms_days: int = 0
    advance_pct: int = 0
    fraud_flags: list[str] = Field(default_factory=list)
    red: list[str] = Field(default_factory=list)
    amber: list[str] = Field(default_factory=list)
    reasons: list[Reason] = Field(default_factory=list)
    could_not_verify: list[str] = Field(default_factory=list)
    requested_inr: Optional[int] = None
    requested_terms: Optional[int] = None
    exposure_inr: int = 0
    base_limit_inr: int = 0
    rule: str = ""
    policy_version: str = ""
    needs_human: bool = False
