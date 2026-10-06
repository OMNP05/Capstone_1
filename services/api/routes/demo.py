"""Demo and health routes."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.agents.llm import llm_mode
from services.api import orchestrator
from services.core.config import settings
from services.core.db import get_db
from services.core.models import Aircraft, Airport, Crew, Disruption, Flight, Pnr
from services.core.schemas import DisruptionDetail, HealthOut, InjectRequest
from services.mcp import weather

router = APIRouter(tags=["demo"])


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    """Liveness plus a straight answer about what is real right now."""
    try:
        db.execute(select(1))
        database = "ok"
    except Exception as exc:  # noqa: BLE001
        database = f"error: {type(exc).__name__}"

    counts = {
        "airports": _count(db, Airport),
        "aircraft": _count(db, Aircraft),
        "flights": _count(db, Flight),
        "crew": _count(db, Crew),
        "pnrs": _count(db, Pnr),
        "disruptions": _count(db, Disruption),
    }

    try:
        with httpx.Client(timeout=4.0) as client:
            resp = client.get(
                "https://aviationweather.gov/api/data/metar",
                params={"ids": settings.hub_icao, "format": "json"},
                headers={"User-Agent": "disruption-orchestrator/0.1"},
            )
        weather_feed = "reachable" if resp.status_code == 200 else f"http {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        weather_feed = f"unreachable: {type(exc).__name__}"

    overrides = weather.active_overrides()
    if overrides:
        weather_feed += f" (demo override active: {', '.join(overrides)})"

    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        database=database,
        database_kind="sqlite (local)" if settings.is_sqlite else "postgres",
        llm_mode=llm_mode(),
        weather_feed=weather_feed,
        seeded=counts,
        phase="1",
    )


@router.post("/demo/inject", response_model=DisruptionDetail, status_code=201)
def inject(
    body: InjectRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> DisruptionDetail:
    """Trigger the seeded scenario.

    Returns as soon as the disruption exists in `DETECTED`; Impact, Recovery
    and the approval gate run in the background so the UI can watch the
    states advance by polling.
    """
    if _count(db, Flight) == 0:
        raise HTTPException(
            status_code=409,
            detail="database is not seeded — run `python -m db.seed` first",
        )

    try:
        disruption = orchestrator.inject_fog_delhi(db)
    except orchestrator.OrchestratorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    background.add_task(orchestrator.run_until_approval, disruption.id)

    from services.api.routes.disruptions import get_disruption

    return get_disruption(disruption.id, db)


@router.post("/demo/reset", status_code=204)
def reset(db: Session = Depends(get_db)) -> None:
    """Clear disruptions and undo the scenario's effects.

    Convenience for rehearsing the demo: the seeded fleet, roster and PNRs
    survive, so this is much faster than reseeding. It has to undo everything
    Coordination wrote, not just the disruption rows — a swapped tail left
    sitting at the wrong airport would silently remove the swap option from
    every later run.
    """
    for disruption in db.scalars(select(Disruption)).all():
        db.delete(disruption)

    for flight in db.scalars(select(Flight)).all():
        flight.etd = flight.std
        flight.eta = flight.sta
        flight.delay_minutes = 0
        flight.status = "SCHEDULED"
        # Put the planned tail back on the leg.
        if flight.scheduled_aircraft_id is not None:
            flight.aircraft_id = flight.scheduled_aircraft_id

    # Tails return to base; `swap_aircraft` moves them to the destination.
    for tail in db.scalars(select(Aircraft)).all():
        tail.location_icao = tail.base_icao

    for pnr in db.scalars(select(Pnr)).all():
        pnr.status = "BOOKED"
        pnr.rebooked_flight_id = None

    # Restore the published roster: crew released by a cancellation go back
    # onto their flight, and standby crew called out go back to reserve.
    for member in db.scalars(select(Crew)).all():
        member.assigned_flight_id = member.scheduled_flight_id

    weather.clear_overrides()
    db.commit()


def _count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0
