"""The orchestrator: one function per leg of the state machine.

Detection -> Impact -> Recovery -> (human approves) -> Coordination -> Comms,
writing `disruptions.state` after each step. It is a state machine; it just
isn't a graph library yet. Phase 3 replaces this file with a LangGraph graph
over the same states and the same agent functions.

Two entry points:
  * `run_until_approval` — everything before the human gate.
  * `run_after_approval`  — everything after it.

Both are safe to call from a FastAPI background task: they open their own
session and never touch request-scoped objects.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.agents import coordination, comms, detection, impact, recovery
from services.agents.llm import AgentOutputError, llm_mode
from services.core import state
from services.core.config import settings
from services.core.db import session_scope
from services.core.models import Disruption, Impact, RecoveryOption, utcnow
from services.core.schemas import DisruptionEvent
from services.mcp import weather

log = logging.getLogger(__name__)

#: How much delay the fog scenario is assumed to cause. Used by Impact to
#: count what breaks, and as the Recovery agent's baseline.
ASSUMED_DELAY_MINUTES = detection.FOG_DELAY_MINUTES


class OrchestratorError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Scenario injection
# --------------------------------------------------------------------------


def inject_fog_delhi(db: Session, *, icao: str | None = None) -> Disruption:
    """`POST /demo/inject` with scenario=fog_delhi.

    Installs a CAT III fog override on the hub's METAR, runs Detection, and
    opens a disruption in `DETECTED`. Returns the existing row if the same
    event was already detected this hour (dedupe by hash).
    """
    icao = (icao or settings.hub_icao).upper()

    weather.set_override(
        weather.WeatherOverride(
            icao=icao,
            visibility_m=150,
            ceiling_ft=100,
            wx="FG",
            note="demo: fog_delhi scenario injected",
        )
    )

    event = detection.detect_weather_disruption(db, icao=icao)
    if event is None:
        raise OrchestratorError(
            f"detection found nothing at {icao} — is the database seeded with "
            "departures in the next 6 hours? Run `python -m db.seed`."
        )

    digest = detection.dedupe_hash(event)
    existing = db.scalar(select(Disruption).where(Disruption.dedupe_hash == digest))
    if existing is not None:
        log.info("disruption %s already open for this event", existing.id)
        return existing

    disruption = _persist_event(db, event, digest)
    db.commit()
    return disruption


def _persist_event(db: Session, event: DisruptionEvent, digest: str) -> Disruption:
    disruption = Disruption(
        type=event.type,
        severity=event.severity,
        confidence=event.confidence,
        eta_minutes=event.eta_minutes,
        airport=event.airport,
        flight_id=event.flight_ids[0] if event.flight_ids else None,
        flight_ids=event.flight_ids,
        state=state.DETECTED,
        evidence=[e.model_dump() for e in event.evidence],
        note=event.summary,
        dedupe_hash=digest,
        timeline=[
            {
                "state": state.DETECTED,
                "at": utcnow().isoformat(),
                "note": event.summary,
            }
        ],
        execution_log=[],
    )
    db.add(disruption)
    db.flush()
    log.info("disruption %s opened: %s", disruption.id, event.summary)
    return disruption


# --------------------------------------------------------------------------
# Leg 1: DETECTED -> ... -> PENDING_APPROVAL
# --------------------------------------------------------------------------


def run_until_approval(disruption_id: int) -> None:
    """Impact, then Recovery, then park at the human gate.

    Runs in its own session so it can be handed to BackgroundTasks. Any
    failure marks the disruption `NEEDS_HUMAN` with the reason, rather than
    leaving it stuck mid-state.
    """
    _pause()
    try:
        with session_scope() as db:
            disruption = _load(db, disruption_id)
            _assess(db, disruption)

        _pause()

        with session_scope() as db:
            disruption = _load(db, disruption_id)
            _plan(db, disruption)

        _pause()

        with session_scope() as db:
            disruption = _load(db, disruption_id)
            _request_approval(db, disruption)

    except Exception as exc:  # noqa: BLE001 — the whole point is to record it
        log.exception("disruption %s failed before approval", disruption_id)
        _mark_needs_human(disruption_id, exc)


def _assess(db: Session, disruption: Disruption) -> None:
    if disruption.state != state.DETECTED:
        return

    flight_ids = _flight_ids(disruption)
    report, detail = impact.assess(
        db,
        flight_ids=flight_ids,
        airport=disruption.airport or settings.hub_icao,
        delay_minutes=ASSUMED_DELAY_MINUTES,
    )

    row = disruption.impact or Impact(disruption_id=disruption.id)
    row.pax_count = report.pax_count
    row.vip_count = report.vip_count
    row.missed_connections = report.missed_connections
    row.knock_on_flights = report.knock_on_flights
    row.crew_legal_breach = report.crew_legal_breach
    row.gate_conflicts = report.gate_conflicts
    row.priority_score = report.priority_score
    row.detail = detail
    db.add(row)

    if report.priority_score >= 0.6:
        disruption.severity = "CRITICAL"
    elif report.priority_score >= 0.4:
        disruption.severity = "HIGH"

    state.transition(disruption, state.ASSESSED, report.narrative)
    log.info(
        "disruption %s assessed: %s pax, priority %.3f",
        disruption.id,
        report.pax_count,
        report.priority_score,
    )


def _plan(db: Session, disruption: Disruption) -> None:
    if disruption.state != state.ASSESSED:
        return

    detail = disruption.impact.detail if disruption.impact else {}
    try:
        scored, source, _ = recovery.plan(
            db,
            flight_ids=_flight_ids(disruption),
            airport=disruption.airport or settings.hub_icao,
            delay_minutes=ASSUMED_DELAY_MINUTES,
            impact_detail=detail,
            disruption_summary=disruption.note or "weather disruption at the hub",
        )
    except AgentOutputError as exc:
        raise OrchestratorError(f"Recovery agent produced unusable output: {exc}") from exc

    if not scored:
        raise OrchestratorError(
            "Recovery produced no option that survived the hard constraints"
        )

    # Replace any previous options (a reject-and-replan path lands here too).
    for old in list(disruption.options):
        db.delete(old)
    db.flush()

    for rank, item in enumerate(scored):
        option = item.option
        db.add(
            RecoveryOption(
                disruption_id=disruption.id,
                kind=option.kind,
                label=option.label,
                summary=option.summary,
                actions=[a.model_dump() for a in option.actions],
                cost_usd=option.cost_usd,
                delay_minutes=option.delay_minutes,
                pax_impacted=option.pax_impacted,
                feasibility=option.feasibility,
                regulatory_ok=item.regulatory_ok,
                crew_legal=item.crew_legal,
                score=item.score,
                rationale=option.rationale,
                recommended=(rank == 0),
            )
        )

    state.transition(
        disruption,
        state.PLANNED,
        f"{len(scored)} option(s) generated by the Recovery agent ({source})",
    )
    log.info("disruption %s planned: %s options (%s)", disruption.id, len(scored), source)


def _request_approval(db: Session, disruption: Disruption) -> None:
    """Phase 1 HITL: always ask.

    Phase 2 replaces this with the section 8 risk rule (auto-approve low-risk,
    require a human for cancellations / >100 pax / >$20k / crew override).
    """
    if disruption.state != state.PLANNED:
        return
    disruption.pending = True
    state.transition(
        disruption,
        state.PENDING_APPROVAL,
        "Phase 1 policy: every option requires explicit human approval",
    )


# --------------------------------------------------------------------------
# Leg 2: APPROVED -> EXECUTING -> COMPLETED
# --------------------------------------------------------------------------


def run_after_approval(disruption_id: int) -> None:
    """Coordination, then Passenger Comms."""
    _pause()
    try:
        with session_scope() as db:
            disruption = _load(db, disruption_id)
            _coordinate(db, disruption)

        _pause()

        with session_scope() as db:
            disruption = _load(db, disruption_id)
            _communicate(db, disruption)

    except Exception as exc:  # noqa: BLE001
        log.exception("disruption %s failed after approval", disruption_id)
        _mark_needs_human(disruption_id, exc)


def _coordinate(db: Session, disruption: Disruption) -> None:
    if disruption.state != state.APPROVED:
        return
    option = _approved_option(db, disruption)

    state.transition(
        disruption, state.EXECUTING, f"executing {option.label}"
    )
    db.flush()

    results = coordination.execute(db, option, approved=not disruption.pending)
    disruption.execution_log = [
        *(disruption.execution_log or []),
        *[
            {**r.model_dump(), "at": utcnow().isoformat()}
            for r in results
        ],
    ]

    blocked = [r for r in results if r.status == "BLOCKED"]
    if blocked:
        raise OrchestratorError(
            "coordination blocked: "
            + "; ".join(f"{r.action}: {r.detail}" for r in blocked)
        )
    log.info("disruption %s executed %s action(s)", disruption.id, len(results))


def _communicate(db: Session, disruption: Disruption) -> None:
    if disruption.state != state.EXECUTING:
        return
    option = _approved_option(db, disruption)

    notifications, source = comms.notify_passengers(db, disruption, option)
    state.transition(
        disruption,
        state.COMPLETED,
        f"{len(notifications)} notification(s) drafted ({source}) and logged",
    )
    log.info(
        "disruption %s completed: %s notification(s) (%s)",
        disruption.id,
        len(notifications),
        source,
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _load(db: Session, disruption_id: int) -> Disruption:
    disruption = db.get(Disruption, disruption_id)
    if disruption is None:
        raise OrchestratorError(f"disruption {disruption_id} not found")
    return disruption


def _flight_ids(disruption: Disruption) -> list[int]:
    ids = [int(i) for i in (disruption.flight_ids or [])]
    if disruption.flight_id and disruption.flight_id not in ids:
        ids.insert(0, disruption.flight_id)
    return ids


def _approved_option(db: Session, disruption: Disruption) -> RecoveryOption:
    if disruption.approved_option_id is None:
        raise OrchestratorError(f"disruption {disruption.id} has no approved option")
    option = db.get(RecoveryOption, disruption.approved_option_id)
    if option is None:
        raise OrchestratorError(
            f"approved option {disruption.approved_option_id} not found"
        )
    return option


def _mark_needs_human(disruption_id: int, exc: BaseException) -> None:
    """Park a failed run in NEEDS_HUMAN with the reason attached."""
    try:
        with session_scope() as db:
            disruption = db.get(Disruption, disruption_id)
            if disruption is None or disruption.state in state.TERMINAL:
                return
            reason = f"{type(exc).__name__}: {exc}"
            disruption.note = reason
            disruption.pending = False
            if state.can_transition(disruption.state, state.NEEDS_HUMAN):
                state.transition(disruption, state.NEEDS_HUMAN, reason)
            else:
                disruption.timeline = [
                    *(disruption.timeline or []),
                    {
                        "state": disruption.state,
                        "at": utcnow().isoformat(),
                        "note": f"failed: {reason}",
                    },
                ]
    except Exception:
        log.exception("could not mark disruption %s as NEEDS_HUMAN", disruption_id)


def _pause() -> None:
    """Brief pause so the polling UI visibly advances through the states."""
    delay = settings.demo_step_delay_seconds
    if delay > 0:
        time.sleep(delay)


def now() -> datetime:
    return datetime.now(timezone.utc)
