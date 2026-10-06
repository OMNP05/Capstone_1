"""aodb domain — airport reference data, gates, congestion.

Airport master data is real (OurAirports CSV, loaded by db/seed.py). Gates
and stand allocation are synthetic in every phase; Phase 1 keeps them as a
string on `flights.gate` rather than a `gates` table (that lands in Phase 2).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.core.models import Airport, Flight

#: Phase 1 stand inventory for the hub, seeded as a constant.
GATE_POOL = [f"A{n}" for n in range(1, 13)] + [f"B{n}" for n in range(1, 9)]


def get_airport_info(db: Session, icao: str) -> dict[str, Any] | None:
    airport = db.scalar(select(Airport).where(Airport.icao == icao.upper()))
    if airport is None:
        return None
    return {
        "icao": airport.icao,
        "iata": airport.iata,
        "name": airport.name,
        "city": airport.city,
        "country": airport.country,
        "lat": airport.lat,
        "lon": airport.lon,
        "hourly_capacity": airport.hourly_capacity,
    }


def get_gate_assignments(
    db: Session, icao: str, *, window_start: datetime, window_end: datetime
) -> dict[str, Any]:
    """Gate usage at `icao` in the window, plus any double-booked stands.

    A conflict in Phase 1 means two flights hold the same gate with
    overlapping ground times — exactly what a retime can create.
    """
    rows = db.scalars(
        select(Flight).where(
            Flight.origin == icao.upper(),
            Flight.status != "CANCELLED",
            Flight.etd >= window_start - timedelta(hours=1),
            Flight.etd <= window_end,
        )
    ).all()

    by_gate: dict[str, list[Flight]] = {}
    for flight in rows:
        if flight.gate:
            by_gate.setdefault(flight.gate, []).append(flight)

    conflicts = []
    for gate, flights in by_gate.items():
        flights.sort(key=lambda f: f.etd)
        for earlier, later in zip(flights, flights[1:]):
            # 45 min minimum turnaround on a stand.
            if later.etd - earlier.etd < timedelta(minutes=45):
                conflicts.append(
                    {
                        "gate": gate,
                        "flights": [earlier.flight_no, later.flight_no],
                        "gap_minutes": int(
                            (later.etd - earlier.etd).total_seconds() // 60
                        ),
                    }
                )

    return {
        "icao": icao.upper(),
        "assignments": [
            {"gate": g, "flights": [f.flight_no for f in fl]}
            for g, fl in sorted(by_gate.items())
        ],
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
    }


def get_airport_congestion(
    db: Session, icao: str, *, at: datetime, hours: int = 4
) -> dict[str, Any]:
    """Movements per hour against declared capacity."""
    airport = db.scalar(select(Airport).where(Airport.icao == icao.upper()))
    capacity = airport.hourly_capacity if airport else 40

    rows = db.scalars(
        select(Flight).where(
            Flight.origin == icao.upper(),
            Flight.status != "CANCELLED",
            Flight.etd >= at,
            Flight.etd <= at + timedelta(hours=hours),
        )
    ).all()

    buckets = Counter(f.etd.replace(minute=0, second=0, microsecond=0) for f in rows)
    hourly = [
        {
            "hour": hour.isoformat(),
            "movements": count,
            "capacity": capacity,
            "over_capacity": count > capacity,
        }
        for hour, count in sorted(buckets.items())
    ]
    peak = max((b["movements"] for b in hourly), default=0)
    return {
        "icao": icao.upper(),
        "hourly": hourly,
        "peak_movements": peak,
        "capacity": capacity,
        "congested": peak > capacity,
    }


def reassign_gate(db: Session, flight_id: int, *, gate: str | None = None) -> dict[str, Any]:
    """`A` tool: move a flight to a free stand.

    With no `gate` given, picks the first stand with no overlapping use.
    """
    flight = db.get(Flight, flight_id)
    if flight is None:
        return {"status": "BLOCKED", "detail": f"flight {flight_id} not found"}

    window_start = flight.etd - timedelta(minutes=45)
    window_end = flight.etd + timedelta(minutes=45)
    taken = {
        f.gate
        for f in db.scalars(
            select(Flight).where(
                Flight.origin == flight.origin,
                Flight.id != flight.id,
                Flight.status != "CANCELLED",
                Flight.etd >= window_start,
                Flight.etd <= window_end,
            )
        ).all()
        if f.gate
    }

    # The flight's current stand is still fine if nothing else claimed it.
    if gate is None and flight.gate and flight.gate not in taken:
        return {
            "status": "SKIPPED",
            "detail": (
                f"{flight.flight_no} keeps stand {flight.gate} — still free at "
                f"the revised time"
            ),
            "gate": flight.gate,
        }

    if gate is None:
        gate = next((g for g in GATE_POOL if g not in taken), None)
    if gate is None:
        return {"status": "BLOCKED", "detail": "no free stand in the window"}
    if gate in taken:
        return {"status": "BLOCKED", "detail": f"gate {gate} is occupied"}
    if gate == flight.gate:
        return {
            "status": "SKIPPED",
            "detail": f"{flight.flight_no} already on stand {gate}",
            "gate": gate,
        }

    previous = flight.gate
    flight.gate = gate
    return {
        "status": "DONE",
        "detail": f"{flight.flight_no}: stand {previous or 'none'} -> {gate}",
        "gate": gate,
    }
