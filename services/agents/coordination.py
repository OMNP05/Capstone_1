"""Coordination agent — execute the approved option.

No external system exists to call in Phase 1, so "executing" means writing
rows through the `W` tools in `services.mcp`. That is deliberate: the tool
signatures are the ones the real flightops/pss/crew integrations will have,
so Phase 3/4 replaces the body of those tools and this agent is unchanged.

No LLM here. Executing an approved plan is dispatch, not reasoning — and an
`H`-tagged write is the last place you want a model improvising.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.core.models import Pnr, RecoveryOption
from services.core.schemas import ActionResult
from services.mcp import aodb, crew, flightops, pss
from services.mcp.registry import check_permission

AGENT = "coordination"

#: Which ops team owns each tool — surfaced in the UI execution log.
TEAM = {
    "flightops.retime_flight": "Network Control",
    "flightops.swap_aircraft": "Fleet Control",
    "flightops.cancel_flight": "Network Control",
    "pss.rebook_passengers": "Reservations",
    "aodb.reassign_gate": "Airport Ops",
    "crew.assign_crew": "Crew Control",
}


def execute(
    db: Session, option: RecoveryOption, *, approved: bool
) -> list[ActionResult]:
    """Run every action on `option`, in order, returning one result each.

    `approved` is the Phase 1 stand-in for the HMAC approval token: an
    unapproved call raises rather than executing an `H` write. Phase 3
    replaces the boolean with a signed, TTL-bound token checked at the
    gateway — the guard moves, the guarantee does not.
    """
    if not approved:
        raise PermissionError(
            f"option {option.id} executed without approval — refusing H-tagged writes"
        )

    results: list[ActionResult] = []
    for action in option.actions or []:
        tool = action.get("tool")
        flight_id = action.get("flight_id")
        args = action.get("args") or {}
        try:
            check_permission(AGENT, tool)
        except Exception as exc:
            results.append(
                ActionResult(
                    team=TEAM.get(tool, "Unknown"),
                    action=tool or "?",
                    status="BLOCKED",
                    detail=str(exc),
                )
            )
            continue

        outcome = _dispatch(db, tool, flight_id, args, option)
        results.append(
            ActionResult(
                team=TEAM.get(tool, "Ops"),
                action=tool,
                status=outcome.get("status", "BLOCKED"),
                detail=outcome.get("detail", ""),
            )
        )

    # Idempotency-ish: flush so a later action in the same option sees the
    # effects of the earlier ones (e.g. rebook after cancel).
    db.flush()
    return results


def _dispatch(
    db: Session,
    tool: str,
    flight_id: int | None,
    args: dict[str, Any],
    option: RecoveryOption,
) -> dict[str, Any]:
    """Map a logical tool name onto the module function behind it."""
    target_flight = flight_id or _primary_flight_id(option)

    if tool == "flightops.retime_flight":
        delay = int(args.get("delay_minutes") or option.delay_minutes)
        return flightops.retime_flight(db, target_flight, delay_minutes=delay)

    if tool == "flightops.swap_aircraft":
        registration = args.get("registration")
        if not registration:
            return {"status": "BLOCKED", "detail": "no registration supplied"}
        return flightops.swap_aircraft(db, target_flight, registration=registration)

    if tool == "flightops.cancel_flight":
        return flightops.cancel_flight(db, target_flight)

    if tool == "aodb.reassign_gate":
        return aodb.reassign_gate(db, target_flight, gate=args.get("gate"))

    if tool == "pss.rebook_passengers":
        pnr_ids = args.get("pnr_ids")
        if not pnr_ids:
            # Default to everyone still booked on the disrupted flight.
            pnr_ids = [
                p.id
                for p in db.scalars(
                    select(Pnr).where(
                        Pnr.flight_id == target_flight, Pnr.status == "BOOKED"
                    )
                ).all()
            ]
        return pss.rebook_passengers(
            db, pnr_ids, target_flight_id=args.get("target_flight_id")
        )

    if tool == "crew.assign_crew":
        codes = args.get("crew_codes") or []
        if not codes:
            return {"status": "SKIPPED", "detail": "no crew codes supplied"}
        return crew.assign_crew(db, codes, flight_id=target_flight)

    return {"status": "BLOCKED", "detail": f"no handler for {tool}"}


def _primary_flight_id(option: RecoveryOption) -> int:
    disruption = option.disruption
    if disruption.flight_id:
        return disruption.flight_id
    ids = disruption.flight_ids or []
    if not ids:
        raise ValueError(f"disruption {disruption.id} has no flights")
    return int(ids[0])
