"""pss domain — passenger service system.

Permanently synthetic: no free API exposes PNRs, fare classes or rebooking
inventory. The seeded Neon/SQLite dataset *is* this product's PSS, in every
phase — not a placeholder.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from services.core.models import Flight, Passenger, Pnr

TIER_RANK = {"BASE": 0, "SILVER": 1, "GOLD": 2, "PLATINUM": 3}


def get_affected_passengers(db: Session, flight_ids: list[int]) -> dict[str, Any]:
    """PNRs on the given flights with tier, fare class and special needs."""
    if not flight_ids:
        return {"pnrs": [], "pax_count": 0, "vip_count": 0, "by_tier": {}}

    rows = db.scalars(
        select(Pnr)
        .options(selectinload(Pnr.passengers))
        .where(Pnr.flight_id.in_(flight_ids), Pnr.status == "BOOKED")
        .order_by(Pnr.record_locator)
    ).all()

    pnrs: list[dict[str, Any]] = []
    pax_count = 0
    vip_count = 0
    by_tier: dict[str, int] = {}

    for pnr in rows:
        pax_count += pnr.pax_count
        by_tier[pnr.tier] = by_tier.get(pnr.tier, 0) + pnr.pax_count
        vips = [p for p in pnr.passengers if p.is_vip]
        vip_count += len(vips)
        pnrs.append(
            {
                "pnr_id": pnr.id,
                "record_locator": pnr.record_locator,
                "flight_id": pnr.flight_id,
                "tier": pnr.tier,
                "fare_class": pnr.fare_class,
                "pax_count": pnr.pax_count,
                "special_needs": pnr.special_needs,
                "has_connection": pnr.connection_flight_id is not None,
                "vip": bool(vips),
            }
        )

    return {
        "pnrs": pnrs,
        "pax_count": pax_count,
        "vip_count": vip_count,
        "by_tier": by_tier,
    }


def get_connections_at_risk(
    db: Session, flight_ids: list[int], *, delay_minutes: int
) -> dict[str, Any]:
    """PNRs whose minimum connect time breaks under `delay_minutes`."""
    if not flight_ids:
        return {"at_risk": [], "count": 0, "pax_count": 0}

    rows = db.scalars(
        select(Pnr).where(
            Pnr.flight_id.in_(flight_ids),
            Pnr.connection_flight_id.is_not(None),
            Pnr.status == "BOOKED",
        )
    ).all()

    at_risk: list[dict[str, Any]] = []
    pax = 0
    shift = timedelta(minutes=delay_minutes)

    for pnr in rows:
        inbound = db.get(Flight, pnr.flight_id)
        onward = db.get(Flight, pnr.connection_flight_id)
        if inbound is None or onward is None:
            continue
        projected_arrival = inbound.sta + shift
        available = onward.etd - projected_arrival
        if available < timedelta(minutes=pnr.connection_mct_minutes):
            pax += pnr.pax_count
            at_risk.append(
                {
                    "record_locator": pnr.record_locator,
                    "pax_count": pnr.pax_count,
                    "tier": pnr.tier,
                    "inbound": inbound.flight_no,
                    "onward": onward.flight_no,
                    "available_minutes": int(available.total_seconds() // 60),
                    "required_mct_minutes": pnr.connection_mct_minutes,
                }
            )

    return {"at_risk": at_risk, "count": len(at_risk), "pax_count": pax}


def get_inventory(
    db: Session,
    *,
    origin: str,
    destination: str,
    after: datetime,
    before: datetime,
    exclude_flight_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Seats left on alternate flights for the same city pair."""
    exclude = set(exclude_flight_ids or [])
    flights = db.scalars(
        select(Flight)
        .where(
            Flight.origin == origin.upper(),
            Flight.destination == destination.upper(),
            Flight.status != "CANCELLED",
            Flight.etd >= after,
            Flight.etd <= before,
        )
        .order_by(Flight.etd)
    ).all()

    inventory: list[dict[str, Any]] = []
    for flight in flights:
        if flight.id in exclude or flight.aircraft is None:
            continue
        booked = sum(
            p.pax_count
            for p in db.scalars(select(Pnr).where(Pnr.flight_id == flight.id)).all()
            if p.status == "BOOKED"
        )
        inventory.append(
            {
                "flight_id": flight.id,
                "flight_no": flight.flight_no,
                "etd": flight.etd.isoformat(),
                "seats": flight.aircraft.seats,
                "booked": booked,
                "seats_available": max(flight.aircraft.seats - booked, 0),
            }
        )
    return inventory


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def rebook_passengers(
    db: Session, pnr_ids: list[int], *, target_flight_id: int | None
) -> dict[str, Any]:
    """`H` tool: move PNRs to `target_flight_id`, highest tier first.

    With no target (nothing to rebook onto), PNRs are marked `DISRUPTED` so
    the Comms agent can offer a refund instead of a reaccommodation.
    """
    if not pnr_ids:
        return {"status": "SKIPPED", "detail": "no PNRs to rebook", "moved": 0}

    pnrs = db.scalars(select(Pnr).where(Pnr.id.in_(pnr_ids))).all()
    pnrs.sort(key=lambda p: TIER_RANK.get(p.tier, 0), reverse=True)

    if target_flight_id is None:
        for pnr in pnrs:
            pnr.status = "DISRUPTED"
        return {
            "status": "DONE",
            "detail": f"{len(pnrs)} PNR(s) marked disrupted — no alternate inventory",
            "moved": 0,
            "not_accommodated": len(pnrs),
        }

    target = db.get(Flight, target_flight_id)
    if target is None or target.aircraft is None:
        return {
            "status": "BLOCKED",
            "detail": f"target flight {target_flight_id} unusable",
            "moved": 0,
        }

    booked = sum(
        p.pax_count
        for p in db.scalars(select(Pnr).where(Pnr.flight_id == target.id)).all()
        if p.status == "BOOKED"
    )
    free = max(target.aircraft.seats - booked, 0)

    moved = 0
    overflow = 0
    for pnr in pnrs:
        if pnr.pax_count <= free:
            pnr.status = "REBOOKED"
            pnr.rebooked_flight_id = target.id
            free -= pnr.pax_count
            moved += pnr.pax_count
        else:
            pnr.status = "DISRUPTED"
            overflow += pnr.pax_count

    detail = f"{moved} pax rebooked onto {target.flight_no}"
    if overflow:
        detail += f", {overflow} pax not accommodated (no seats)"
    return {
        "status": "DONE",
        "detail": detail,
        "moved": moved,
        "not_accommodated": overflow,
        "target_flight_no": target.flight_no,
    }


#: Entitlement thresholds, coded not inferred (check #7 / section 8).
MEAL_VOUCHER_DELAY_MIN = 180
HOTEL_VOUCHER_DELAY_MIN = 360
MEAL_VOUCHER_USD = 15.0
HOTEL_VOUCHER_USD = 90.0


def issue_voucher(pnr_locator: str, *, delay_minutes: int) -> dict[str, Any] | None:
    """`A` tool: capped meal / hotel entitlement for a delay.

    Returns `None` when the delay doesn't earn one — the Comms agent then
    sends an apology without an offer rather than inventing compensation.
    """
    if delay_minutes >= HOTEL_VOUCHER_DELAY_MIN:
        return {
            "pnr": pnr_locator,
            "kind": "HOTEL",
            "amount_usd": HOTEL_VOUCHER_USD,
            "text": f"Overnight hotel and transfer (up to ${HOTEL_VOUCHER_USD:.0f})",
        }
    if delay_minutes >= MEAL_VOUCHER_DELAY_MIN:
        return {
            "pnr": pnr_locator,
            "kind": "MEAL",
            "amount_usd": MEAL_VOUCHER_USD,
            "text": f"Meal voucher (${MEAL_VOUCHER_USD:.0f}) redeemable airside",
        }
    return None
