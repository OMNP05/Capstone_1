"""crew domain — roster and flight-time-limitation checks.

Permanently synthetic. The FTL rules below are DGCA/EASA-*style* and
deliberately live in Python, never in a prompt: crew legality is a hard
constraint (check #7, architecture doc section 10).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.core.models import Crew, Flight

#: FTL rule set, configurable in one place.
MIN_REST_HOURS = 10.0
MAX_DUTY_MINUTES_DEFAULT = 780  # 13h
SECTOR_DUTY_ALLOWANCE_MINUTES = 60  # pre-flight + post-flight duty around a leg


def get_crew_roster(db: Session, flight_ids: list[int]) -> list[dict[str, Any]]:
    if not flight_ids:
        return []
    rows = db.scalars(
        select(Crew).where(Crew.assigned_flight_id.in_(flight_ids))
    ).all()
    return [_crew_dict(c) for c in rows]


def check_duty_limits(
    db: Session, flight_id: int, *, delay_minutes: int = 0
) -> dict[str, Any]:
    """FTL check for the crew of `flight_id` under an added delay.

    Returns a per-crew verdict plus an overall `breach` flag. A delay extends
    duty by the delay itself; rest below `MIN_REST_HOURS` is a breach
    regardless of duty length.
    """
    flight = db.get(Flight, flight_id)
    if flight is None:
        return {"breach": False, "crew": [], "detail": "flight not found"}

    roster = db.scalars(
        select(Crew).where(Crew.assigned_flight_id == flight_id)
    ).all()

    sector_minutes = int((flight.sta - flight.std).total_seconds() // 60)
    verdicts: list[dict[str, Any]] = []
    breach = False

    for member in roster:
        projected = (
            member.duty_minutes_so_far
            + sector_minutes
            + SECTOR_DUTY_ALLOWANCE_MINUTES
            + delay_minutes
        )
        limit = member.max_duty_minutes or MAX_DUTY_MINUTES_DEFAULT
        reasons: list[str] = []
        if projected > limit:
            reasons.append(
                f"projected duty {projected} min exceeds {limit} min limit"
            )
        if member.rest_hours_before_duty < MIN_REST_HOURS:
            reasons.append(
                f"pre-duty rest {member.rest_hours_before_duty:.1f} h "
                f"below {MIN_REST_HOURS:.0f} h minimum"
            )
        legal = not reasons
        breach = breach or not legal
        verdicts.append(
            {
                "crew_code": member.crew_code,
                "name": member.name,
                "role": member.role,
                "projected_duty_minutes": projected,
                "max_duty_minutes": limit,
                "margin_minutes": limit - projected,
                "legal": legal,
                "reasons": reasons,
            }
        )

    return {
        "flight_id": flight_id,
        "flight_no": flight.flight_no,
        "delay_minutes": delay_minutes,
        "breach": breach,
        "crew": verdicts,
        "max_legal_delay_minutes": _max_legal_delay(verdicts, delay_minutes),
    }


def _max_legal_delay(verdicts: list[dict[str, Any]], applied: int) -> int | None:
    """How much delay the tightest crew member can still absorb."""
    if not verdicts:
        return None
    margins = [v["margin_minutes"] + applied for v in verdicts]
    return max(min(margins), 0)


def find_standby_crew(
    db: Session, *, at_icao: str, type_code: str, roles: list[str] | None = None
) -> list[dict[str, Any]]:
    """Reserve crew at `at_icao` qualified on `type_code` and legal to fly."""
    rows = db.scalars(
        select(Crew).where(
            Crew.on_standby.is_(True),
            Crew.base_icao == at_icao.upper(),
            Crew.assigned_flight_id.is_(None),
        )
    ).all()

    wanted = set(roles) if roles else None
    out: list[dict[str, Any]] = []
    for member in rows:
        if wanted and member.role not in wanted:
            continue
        if not member.qualified_for(type_code):
            continue
        if member.rest_hours_before_duty < MIN_REST_HOURS:
            continue
        out.append(_crew_dict(member))
    return out


def assign_crew(
    db: Session, crew_codes: list[str], *, flight_id: int
) -> dict[str, Any]:
    """`H` tool: assign or swap crew onto a flight."""
    flight = db.get(Flight, flight_id)
    if flight is None:
        return {"status": "BLOCKED", "detail": f"flight {flight_id} not found"}

    members = db.scalars(
        select(Crew).where(Crew.crew_code.in_(crew_codes))
    ).all()
    if not members:
        return {"status": "BLOCKED", "detail": "no matching crew"}

    type_code = flight.aircraft.type_code if flight.aircraft else None
    assigned: list[str] = []
    for member in members:
        if type_code and not member.qualified_for(type_code):
            continue
        member.assigned_flight_id = flight.id
        member.duty_start = flight.etd - timedelta(minutes=60)
        assigned.append(member.crew_code)

    if not assigned:
        return {
            "status": "BLOCKED",
            "detail": f"no candidate qualified on {type_code}",
        }
    return {
        "status": "DONE",
        "detail": f"assigned {', '.join(assigned)} to {flight.flight_no}",
        "crew_codes": assigned,
    }


def _crew_dict(c: Crew) -> dict[str, Any]:
    return {
        "crew_code": c.crew_code,
        "name": c.name,
        "role": c.role,
        "base_icao": c.base_icao,
        "qualified_types": c.qualified_types,
        "on_standby": c.on_standby,
        "duty_minutes_so_far": c.duty_minutes_so_far,
        "max_duty_minutes": c.max_duty_minutes,
        "rest_hours_before_duty": c.rest_hours_before_duty,
        "assigned_flight_id": c.assigned_flight_id,
    }


def now_utc() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)
