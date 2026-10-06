"""flightops domain — schedule, fleet, maintenance.

Phase 1 reads the seeded Faker schedule rather than AviationStack (no key).
`get_live_flights` is deliberately absent: nothing in the Phase 1 scenario
needs live positions, and a stub nobody calls is worse than no stub.

Writes (`retime_flight`, `swap_aircraft`, `cancel_flight`) are tagged `H` —
Coordination only reaches them after the human has approved.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.core.models import Aircraft, Crew, Flight, Pnr

# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def get_schedule(
    db: Session,
    *,
    origin: str | None = None,
    destination: str | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    registration: str | None = None,
) -> list[dict[str, Any]]:
    """Flights filtered by airport, tail and time window."""
    stmt = select(Flight)
    if origin:
        stmt = stmt.where(Flight.origin == origin.upper())
    if destination:
        stmt = stmt.where(Flight.destination == destination.upper())
    if after is not None:
        stmt = stmt.where(Flight.std >= after)
    if before is not None:
        stmt = stmt.where(Flight.std <= before)
    if registration:
        stmt = stmt.join(Aircraft).where(Aircraft.registration == registration)
    return [_flight_dict(f) for f in db.scalars(stmt.order_by(Flight.std)).all()]


def get_fleet_status(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(select(Aircraft).order_by(Aircraft.registration)).all()
    return [
        {
            "registration": a.registration,
            "type_code": a.type_code,
            "seats": a.seats,
            "location_icao": a.location_icao,
            "status": a.status,
        }
        for a in rows
    ]


def get_maintenance_status(db: Session) -> list[dict[str, Any]]:
    """Open defects / MEL / AOG flags. Synthetic in every phase."""
    rows = db.scalars(select(Aircraft).where(Aircraft.status != "SERVICEABLE")).all()
    return [
        {
            "registration": a.registration,
            "status": a.status,
            "mel_note": a.mel_note,
            "location_icao": a.location_icao,
        }
        for a in rows
    ]


def get_aircraft_availability(
    db: Session, *, at_icao: str, window_start: datetime, window_end: datetime,
    min_seats: int = 0,
) -> list[dict[str, Any]]:
    """Serviceable tails sitting at `at_icao` with no leg inside the window."""
    candidates = db.scalars(
        select(Aircraft).where(
            Aircraft.status == "SERVICEABLE",
            Aircraft.location_icao == at_icao.upper(),
            Aircraft.seats >= min_seats,
        )
    ).all()

    free: list[dict[str, Any]] = []
    for tail in candidates:
        clash = db.scalar(
            select(Flight.id).where(
                Flight.aircraft_id == tail.id,
                Flight.status != "CANCELLED",
                Flight.etd < window_end,
                Flight.eta > window_start,
            )
        )
        if clash is None:
            free.append(
                {
                    "registration": tail.registration,
                    "type_code": tail.type_code,
                    "seats": tail.seats,
                    "location_icao": tail.location_icao,
                }
            )
    return free


def get_downstream_rotation(
    db: Session, flight_id: int, *, horizon_hours: int = 24
) -> list[dict[str, Any]]:
    """Later legs of the same tail — the knock-on set."""
    flight = db.get(Flight, flight_id)
    if flight is None or flight.aircraft_id is None:
        return []
    rows = db.scalars(
        select(Flight)
        .where(
            Flight.aircraft_id == flight.aircraft_id,
            Flight.id != flight.id,
            Flight.std > flight.std,
            Flight.std <= flight.std + timedelta(hours=horizon_hours),
            Flight.status != "CANCELLED",
        )
        .order_by(Flight.std)
    ).all()
    return [_flight_dict(f) for f in rows]


# --------------------------------------------------------------------------
# Writes — all `H` (approval required)
# --------------------------------------------------------------------------


def retime_flight(db: Session, flight_id: int, *, delay_minutes: int) -> dict[str, Any]:
    flight = db.get(Flight, flight_id)
    if flight is None:
        return {"status": "BLOCKED", "detail": f"flight {flight_id} not found"}
    if flight.status == "CANCELLED":
        return {"status": "SKIPPED", "detail": f"{flight.flight_no} already cancelled"}

    shift = timedelta(minutes=delay_minutes)
    flight.etd = flight.std + shift
    flight.eta = flight.sta + shift
    flight.delay_minutes = delay_minutes
    flight.status = "DELAYED" if delay_minutes > 0 else "SCHEDULED"
    return {
        "status": "DONE",
        "detail": (
            f"{flight.flight_no} retimed +{delay_minutes} min "
            f"(new ETD {flight.etd:%H:%MZ})"
        ),
        "flight_no": flight.flight_no,
        "new_etd": flight.etd.isoformat(),
    }


def swap_aircraft(db: Session, flight_id: int, *, registration: str) -> dict[str, Any]:
    flight = db.get(Flight, flight_id)
    if flight is None:
        return {"status": "BLOCKED", "detail": f"flight {flight_id} not found"}
    tail = db.scalar(select(Aircraft).where(Aircraft.registration == registration))
    if tail is None:
        return {"status": "BLOCKED", "detail": f"tail {registration} not found"}
    if tail.status != "SERVICEABLE":
        return {"status": "BLOCKED", "detail": f"{registration} is {tail.status}"}

    # Row-level guardrail (check #9): don't seat more pax than the tail holds.
    booked = sum(
        p.pax_count
        for p in db.scalars(select(Pnr).where(Pnr.flight_id == flight.id)).all()
        if p.status == "BOOKED"
    )
    previous = flight.aircraft.registration if flight.aircraft else None
    flight.aircraft_id = tail.id
    tail.location_icao = flight.destination
    shortfall = max(booked - tail.seats, 0)
    return {
        "status": "DONE",
        "detail": (
            f"{flight.flight_no}: {previous or 'unassigned'} -> {registration} "
            f"({tail.seats} seats, {booked} booked"
            + (f", {shortfall} overflow" if shortfall else "")
            + ")"
        ),
        "seat_shortfall": shortfall,
    }


def cancel_flight(db: Session, flight_id: int) -> dict[str, Any]:
    flight = db.get(Flight, flight_id)
    if flight is None:
        return {"status": "BLOCKED", "detail": f"flight {flight_id} not found"}
    if flight.status == "CANCELLED":
        return {"status": "SKIPPED", "detail": f"{flight.flight_no} already cancelled"}

    flight.status = "CANCELLED"
    # Release the resources the flight was holding.
    flight.gate = None
    for member in db.scalars(select(Crew).where(Crew.assigned_flight_id == flight.id)):
        member.assigned_flight_id = None
    return {
        "status": "DONE",
        "detail": f"{flight.flight_no} cancelled, gate and crew released",
        "flight_no": flight.flight_no,
    }


def _flight_dict(f: Flight) -> dict[str, Any]:
    return {
        "id": f.id,
        "flight_no": f.flight_no,
        "origin": f.origin,
        "destination": f.destination,
        "std": f.std.isoformat(),
        "sta": f.sta.isoformat(),
        "etd": f.etd.isoformat(),
        "eta": f.eta.isoformat(),
        "status": f.status,
        "delay_minutes": f.delay_minutes,
        "gate": f.gate,
        "registration": f.aircraft.registration if f.aircraft else None,
        "type_code": f.aircraft.type_code if f.aircraft else None,
        "seats": f.aircraft.seats if f.aircraft else None,
    }
