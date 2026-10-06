"""Impact agent — who is affected, and how badly.

Every number here is counted from the database, not estimated by a model:
section 8 requires the counts to reconcile with the DB, and the priority score
is a fixed formula. Claude would add nothing but drift, so this agent is pure
Python and has no stub/real split.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from services.core.models import Flight
from services.core.schemas import ImpactReport
from services.mcp import aodb, crew, flightops, pss
from services.mcp.registry import check_permission

AGENT = "impact"

#: Normalisation ceilings for the priority score. A disruption at or above
#: these counts saturates that term.
NORM_PAX = 300.0
NORM_CONNECTIONS = 40.0
NORM_KNOCK_ON = 6.0
#: Departure-urgency horizon from the section 8 formula (6 h).
URGENCY_HORIZON_MINUTES = 360.0


def assess(
    db: Session, *, flight_ids: list[int], airport: str, delay_minutes: int
) -> tuple[ImpactReport, dict[str, Any]]:
    """Count the impact of `delay_minutes` applied to `flight_ids`.

    Returns the schema-validated report plus a detail blob (the evidence the
    UI renders and the Recovery agent reads).
    """
    primary_id = flight_ids[0] if flight_ids else None
    primary = db.get(Flight, primary_id) if primary_id else None

    check_permission(AGENT, "pss.get_affected_passengers")
    pax = pss.get_affected_passengers(db, flight_ids)

    check_permission(AGENT, "pss.get_connections_at_risk")
    connections = pss.get_connections_at_risk(
        db, flight_ids, delay_minutes=delay_minutes
    )

    check_permission(AGENT, "flightops.get_downstream_rotation")
    knock_on: list[dict[str, Any]] = []
    for flight_id in flight_ids:
        knock_on.extend(flightops.get_downstream_rotation(db, flight_id))
    knock_on_unique = {f["id"]: f for f in knock_on if f["id"] not in set(flight_ids)}

    check_permission(AGENT, "crew.check_duty_limits")
    crew_checks = [
        crew.check_duty_limits(db, fid, delay_minutes=delay_minutes)
        for fid in flight_ids
    ]
    breached = [c for c in crew_checks if c["breach"]]

    gate_conflicts = 0
    gate_detail: dict[str, Any] = {}
    if primary is not None:
        check_permission(AGENT, "aodb.get_gate_assignments")
        gate_detail = aodb.get_gate_assignments(
            db,
            airport,
            window_start=primary.etd,
            window_end=primary.etd + timedelta(minutes=delay_minutes + 60),
        )
        gate_conflicts = gate_detail["conflict_count"]

    minutes_to_departure = 0.0
    if primary is not None:
        from services.core.models import utcnow

        minutes_to_departure = max(
            (primary.etd - utcnow()).total_seconds() / 60.0, 0.0
        )

    vip_ratio = (pax["vip_count"] / pax["pax_count"]) if pax["pax_count"] else 0.0
    priority = priority_score(
        pax_count=pax["pax_count"],
        missed_connections=connections["count"],
        vip_ratio=vip_ratio,
        knock_on_flights=len(knock_on_unique),
        crew_breach=bool(breached),
        minutes_to_departure=minutes_to_departure,
    )

    report = ImpactReport(
        pax_count=pax["pax_count"],
        vip_count=pax["vip_count"],
        missed_connections=connections["count"],
        knock_on_flights=len(knock_on_unique),
        crew_legal_breach=bool(breached),
        gate_conflicts=gate_conflicts,
        priority_score=priority,
        narrative=(
            f"{pax['pax_count']} passengers across {len(pax['pnrs'])} PNRs on "
            f"{len(flight_ids)} departure(s); {connections['count']} connection(s) "
            f"({connections['pax_count']} pax) break at +{delay_minutes} min; "
            f"{len(knock_on_unique)} downstream leg(s) affected"
            + (
                f"; crew FTL breach on {len(breached)} flight(s)"
                if breached
                else "; crew remain legal"
            )
            + (f"; {gate_conflicts} stand conflict(s)" if gate_conflicts else "")
        ),
    )

    # The detail blob is what the UI and the Recovery agent read. It is also
    # the Phase 1 stand-in for the audit log.
    report_detail = {
        "delay_minutes_assumed": delay_minutes,
        "by_tier": pax["by_tier"],
        "pnr_count": len(pax["pnrs"]),
        "connections_at_risk": connections["at_risk"][:10],
        "connection_pax": connections["pax_count"],
        "knock_on_flights": [
            {"flight_no": f["flight_no"], "std": f["std"], "destination": f["destination"]}
            for f in knock_on_unique.values()
        ][:10],
        "crew_checks": [
            {
                "flight_no": c["flight_no"],
                "breach": c["breach"],
                "max_legal_delay_minutes": c["max_legal_delay_minutes"],
                "reasons": [r for v in c["crew"] for r in v["reasons"]][:4],
            }
            for c in crew_checks
        ],
        "gate_conflicts": gate_detail.get("conflicts", [])[:5],
        "priority_terms": {
            "pax": round(min(pax["pax_count"] / NORM_PAX, 1.0), 3),
            "missed_connections": round(
                min(connections["count"] / NORM_CONNECTIONS, 1.0), 3
            ),
            "vip_ratio": round(vip_ratio, 3),
            "knock_on": round(min(len(knock_on_unique) / NORM_KNOCK_ON, 1.0), 3),
            "crew_breach": 1.0 if breached else 0.0,
            "urgency": round(
                1 - min(minutes_to_departure / URGENCY_HORIZON_MINUTES, 1.0), 3
            ),
        },
    }
    return report, report_detail


def priority_score(
    *,
    pax_count: int,
    missed_connections: int,
    vip_ratio: float,
    knock_on_flights: int,
    crew_breach: bool,
    minutes_to_departure: float,
) -> float:
    """Architecture doc section 8, verbatim.

    `0.35*norm(pax) + 0.20*norm(missed_connections) + 0.15*vip_ratio
     + 0.15*knock_on_flights + 0.10*crew_breach
     + 0.05*(1 - minutes_to_departure/360)`
    """
    score = (
        0.35 * min(pax_count / NORM_PAX, 1.0)
        + 0.20 * min(missed_connections / NORM_CONNECTIONS, 1.0)
        + 0.15 * min(max(vip_ratio, 0.0), 1.0)
        + 0.15 * min(knock_on_flights / NORM_KNOCK_ON, 1.0)
        + 0.10 * (1.0 if crew_breach else 0.0)
        + 0.05 * (1 - min(minutes_to_departure / URGENCY_HORIZON_MINUTES, 1.0))
    )
    return round(min(max(score, 0.0), 1.0), 3)
