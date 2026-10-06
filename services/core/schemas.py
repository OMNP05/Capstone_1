"""Pydantic contracts.

Two groups:
  * **Agent output schemas** (`DisruptionEvent`, `ImpactReport`,
    `RecoveryOptionOut`, `ActionResult`, `NotificationDraft`) — these are the
    shapes from architecture doc section 8 that every Claude call must parse
    into. Validation failure costs one retry, then `NEEDS_HUMAN`.
  * **API response schemas** — what the Streamlit dashboard receives.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Agent output schemas (LLM-facing — keep them small and flat)
# --------------------------------------------------------------------------

DisruptionType = Literal["WEATHER_FOG", "AOG", "CREW_TIMEOUT", "CONGESTION"]
Severity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
OptionKind = Literal["RETIME", "SWAP_AIRCRAFT", "CANCEL"]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(description="Tool or feed the evidence came from")
    detail: str


class DisruptionEvent(BaseModel):
    """Detection agent output."""

    model_config = ConfigDict(extra="forbid")

    type: DisruptionType
    airport: str
    flight_ids: list[int]
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    eta_minutes: int = Field(ge=0, description="Minutes until impact begins")
    summary: str
    evidence: list[Evidence] = Field(default_factory=list)


class ImpactReport(BaseModel):
    """Impact agent output. Counts must reconcile with the DB."""

    model_config = ConfigDict(extra="forbid")

    pax_count: int = Field(ge=0)
    vip_count: int = Field(ge=0)
    missed_connections: int = Field(ge=0)
    knock_on_flights: int = Field(ge=0)
    crew_legal_breach: bool
    gate_conflicts: int = Field(ge=0)
    priority_score: float = Field(ge=0.0, le=1.0)
    narrative: str = ""


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(
        description="Logical MCP tool name, e.g. flightops.retime_flight"
    )
    flight_id: int | None = None
    args: dict = Field(default_factory=dict)


class RecoveryOptionOut(BaseModel):
    """One Recovery agent option, before Python scoring and guardrails."""

    model_config = ConfigDict(extra="forbid")

    kind: OptionKind
    label: str
    summary: str
    actions: list[RecoveryAction]
    cost_usd: float = Field(ge=0)
    delay_minutes: int = Field(ge=0)
    pax_impacted: int = Field(ge=0)
    feasibility: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class RecoveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: list[RecoveryOptionOut] = Field(min_length=1, max_length=4)


class ActionResult(BaseModel):
    """Coordination agent output, one per executed action."""

    model_config = ConfigDict(extra="forbid")

    team: str
    action: str
    status: Literal["DONE", "BLOCKED", "SKIPPED"]
    detail: str = ""


class NotificationDraft(BaseModel):
    """Passenger Comms agent output."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(max_length=120)
    body: str
    offer: str | None = Field(default=None, max_length=100)


# --------------------------------------------------------------------------
# API response schemas
# --------------------------------------------------------------------------


class FlightOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    flight_no: str
    origin: str
    destination: str
    std: datetime
    sta: datetime
    etd: datetime
    eta: datetime
    status: str
    delay_minutes: int
    gate: str | None
    registration: str | None = None
    scheduled_registration: str | None = None


class ImpactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pax_count: int
    vip_count: int
    missed_connections: int
    knock_on_flights: int
    crew_legal_breach: bool
    gate_conflicts: int
    priority_score: float
    detail: dict


class RecoveryOptionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    label: str
    summary: str
    actions: list
    cost_usd: float
    delay_minutes: int
    pax_impacted: int
    feasibility: float
    regulatory_ok: bool
    crew_legal: bool
    score: float
    rationale: str | None
    recommended: bool


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    pnr_locator: str | None
    recipient: str | None
    channel: str
    subject: str | None
    body: str
    offer: str | None
    status: str
    created_at: datetime


class DisruptionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    severity: str
    confidence: float
    airport: str | None
    state: str
    pending: bool
    approved_by: str | None
    created_at: datetime
    updated_at: datetime
    flight_no: str | None = None
    pax_count: int | None = None
    priority_score: float | None = None
    option_count: int = 0
    notification_count: int = 0


class DisruptionDetail(DisruptionSummary):
    eta_minutes: int
    note: str | None
    evidence: list
    timeline: list
    execution_log: list
    approved_option_id: int | None
    decision_comment: str | None
    flight: FlightOut | None = None
    affected_flights: list[FlightOut] = Field(default_factory=list)
    impact: ImpactOut | None = None
    options: list[RecoveryOptionPublic] = Field(default_factory=list)
    notifications: list[NotificationOut] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------


class InjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Phase 1 supports exactly one scenario; the param exists so Phase 2 can
    # add the other three without changing the route shape.
    scenario: Literal["fog_delhi"] = "fog_delhi"


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_id: int | None = Field(
        default=None, description="Defaults to the recommended option"
    )
    comment: str | None = None
    approved_by: str = "demo-user"


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    request_replan: bool = False
    rejected_by: str = "demo-user"


class HealthOut(BaseModel):
    status: str
    database: str
    database_kind: str
    llm_mode: str
    weather_feed: str
    seeded: dict
    phase: str
