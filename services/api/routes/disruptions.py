"""Disruption routes — list, detail, approve, reject.

Phase 1 has no auth: one implicit "ops" role for everyone. The `approved_by`
field is a plain string from the request body so the audit trail still records
*who* clicked, even without a login.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from services.api import orchestrator
from services.core import state
from services.core.db import get_db
from services.core.models import Disruption, Flight, Notification, RecoveryOption
from services.core.schemas import (
    ApproveRequest,
    DisruptionDetail,
    DisruptionSummary,
    FlightOut,
    RejectRequest,
)

router = APIRouter(prefix="/disruptions", tags=["disruptions"])


@router.get("", response_model=list[DisruptionSummary])
def list_disruptions(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None, description="Filter by state"),
    severity: str | None = None,
    airport: str | None = None,
    limit: int = Query(default=50, le=200),
) -> list[DisruptionSummary]:
    """The table the dashboard polls every 5 seconds."""
    stmt = (
        select(Disruption)
        .options(selectinload(Disruption.impact), selectinload(Disruption.options))
        .order_by(Disruption.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(Disruption.state == status.upper())
    if severity:
        stmt = stmt.where(Disruption.severity == severity.upper())
    if airport:
        stmt = stmt.where(Disruption.airport == airport.upper())

    rows = db.scalars(stmt).all()
    return [_summary(db, d) for d in rows]


@router.get("/{disruption_id}", response_model=DisruptionDetail)
def get_disruption(
    disruption_id: int, db: Session = Depends(get_db)
) -> DisruptionDetail:
    disruption = _get(db, disruption_id)
    base = _summary(db, disruption).model_dump()

    flight_ids = [int(i) for i in (disruption.flight_ids or [])]
    affected = (
        db.scalars(
            select(Flight).where(Flight.id.in_(flight_ids)).order_by(Flight.std)
        ).all()
        if flight_ids
        else []
    )

    options = db.scalars(
        select(RecoveryOption)
        .where(RecoveryOption.disruption_id == disruption.id)
        .order_by(RecoveryOption.score.desc())
    ).all()

    notifications = db.scalars(
        select(Notification)
        .where(Notification.disruption_id == disruption.id)
        .order_by(Notification.id)
    ).all()

    return DisruptionDetail(
        **base,
        eta_minutes=disruption.eta_minutes,
        note=disruption.note,
        evidence=disruption.evidence or [],
        timeline=disruption.timeline or [],
        execution_log=disruption.execution_log or [],
        approved_option_id=disruption.approved_option_id,
        decision_comment=disruption.decision_comment,
        flight=_flight_out(disruption.flight) if disruption.flight else None,
        affected_flights=[_flight_out(f) for f in affected],
        impact=disruption.impact,
        options=options,
        notifications=notifications,
    )


@router.post("/{disruption_id}/approve", response_model=DisruptionDetail)
def approve(
    disruption_id: int,
    body: ApproveRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> DisruptionDetail:
    """The one HITL button.

    Phase 1: sets `pending = false`, records `approved_by`, moves to APPROVED
    and lets the orchestrator continue. No token, no HMAC, no TTL — that
    arrives in Phase 3.
    """
    disruption = _get(db, disruption_id)
    if disruption.state != state.PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=(
                f"disruption {disruption_id} is {disruption.state}, "
                "not PENDING_APPROVAL"
            ),
        )

    option = _resolve_option(db, disruption, body.option_id)

    disruption.pending = False
    disruption.approved_by = body.approved_by
    disruption.approved_option_id = option.id
    disruption.decision_comment = body.comment
    state.transition(
        disruption,
        state.APPROVED,
        f"{body.approved_by} approved: {option.label}"
        + (f" — {body.comment}" if body.comment else ""),
    )
    db.commit()

    background.add_task(orchestrator.run_after_approval, disruption_id)
    return get_disruption(disruption_id, db)


@router.post("/{disruption_id}/reject", response_model=DisruptionDetail)
def reject(
    disruption_id: int,
    body: RejectRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> DisruptionDetail:
    """Reject the plan.

    With `request_replan`, the disruption drops back to ASSESSED so the
    Recovery agent runs again; otherwise it is CLOSED.
    """
    disruption = _get(db, disruption_id)
    if disruption.state != state.PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=(
                f"disruption {disruption_id} is {disruption.state}, "
                "not PENDING_APPROVAL"
            ),
        )

    disruption.pending = False
    disruption.decision_comment = body.reason
    reason = body.reason or "no reason given"

    if body.request_replan:
        state.transition(
            disruption, state.PLANNED, f"{body.rejected_by} rejected: {reason}"
        )
        # Rewind to ASSESSED so the Recovery agent reruns against the same
        # impact assessment; `_plan` deletes the stale options.
        state.transition(
            disruption, state.ASSESSED, "replan requested — regenerating options"
        )
        db.commit()
        background.add_task(orchestrator.run_until_approval, disruption_id)
    else:
        state.transition(
            disruption, state.CLOSED, f"{body.rejected_by} rejected: {reason}"
        )
        db.commit()

    return get_disruption(disruption_id, db)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _get(db: Session, disruption_id: int) -> Disruption:
    disruption = db.get(Disruption, disruption_id)
    if disruption is None:
        raise HTTPException(status_code=404, detail=f"no disruption {disruption_id}")
    return disruption


def _resolve_option(
    db: Session, disruption: Disruption, option_id: int | None
) -> RecoveryOption:
    options = db.scalars(
        select(RecoveryOption).where(RecoveryOption.disruption_id == disruption.id)
    ).all()
    if not options:
        raise HTTPException(
            status_code=409, detail="no recovery options to approve"
        )

    if option_id is None:
        return max(options, key=lambda o: (o.recommended, o.score))

    match = next((o for o in options if o.id == option_id), None)
    if match is None:
        raise HTTPException(
            status_code=400,
            detail=f"option {option_id} does not belong to disruption {disruption.id}",
        )
    if not match.regulatory_ok:
        raise HTTPException(
            status_code=400,
            detail=f"option {option_id} failed its regulatory check and cannot be approved",
        )
    return match


def _summary(db: Session, d: Disruption) -> DisruptionSummary:
    return DisruptionSummary(
        id=d.id,
        type=d.type,
        severity=d.severity,
        confidence=d.confidence,
        airport=d.airport,
        state=d.state,
        pending=d.pending,
        approved_by=d.approved_by,
        created_at=d.created_at,
        updated_at=d.updated_at,
        flight_no=d.flight.flight_no if d.flight else None,
        pax_count=d.impact.pax_count if d.impact else None,
        priority_score=d.impact.priority_score if d.impact else None,
        option_count=len(d.options),
        notification_count=db.scalar(
            select(func.count(Notification.id)).where(
                Notification.disruption_id == d.id
            )
        )
        or 0,
    )


def _flight_out(flight: Flight) -> FlightOut:
    return FlightOut(
        id=flight.id,
        flight_no=flight.flight_no,
        origin=flight.origin,
        destination=flight.destination,
        std=flight.std,
        sta=flight.sta,
        etd=flight.etd,
        eta=flight.eta,
        status=flight.status,
        delay_minutes=flight.delay_minutes,
        gate=flight.gate,
        registration=flight.aircraft.registration if flight.aircraft else None,
        scheduled_registration=(
            flight.scheduled_aircraft.registration
            if flight.scheduled_aircraft
            else None
        ),
    )
