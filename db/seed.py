"""Seed the database. Run once:  python -m db.seed

Phase 1 targets, per the roadmap: ~20 flights, ~5 tails, ~15 crew, ~80-100
PNRs. Everything except the airport table is synthetic (Faker) and stays
synthetic in every phase — there is no free API for PSS, crew rosters, AODB
gates or MEL status.

The airport table is real: OurAirports CSV, downloaded once (no key) and
cached under db/cache/. If the download fails, a small built-in fallback
table is used so seeding never blocks on the network.

Flags:
  --reset   drop and recreate every table first
  --flights N / --pnrs N / --crew N / --tails N   override the counts
"""

from __future__ import annotations

import argparse
import csv
import io
from datetime import datetime, timedelta, timezone

import httpx
from faker import Faker
from sqlalchemy import func, select

from services.core.config import REPO_ROOT, settings
from services.core.db import SessionLocal, create_all, drop_all
from services.core.models import (
    Aircraft,
    Airport,
    Crew,
    Flight,
    Passenger,
    Pnr,
)
from services.mcp.aodb import GATE_POOL

OURAIRPORTS_CSV = (
    "https://davidmegginson.github.io/ourairports-data/airports.csv"
)
CACHE = REPO_ROOT / "db" / "cache" / "airports.csv"

fake = Faker("en_IN")
Faker.seed(20260101)

# --------------------------------------------------------------------------
# The route network. Hub is DEL; the fog scenario hits DEL departures.
# --------------------------------------------------------------------------

HUB = "VIDP"  # Delhi
SPOKES = [
    ("VABB", "Mumbai"),
    ("VOBL", "Bengaluru"),
    ("VOMM", "Chennai"),
    ("VECC", "Kolkata"),
    ("VOHS", "Hyderabad"),
    ("VAAH", "Ahmedabad"),
    ("VOCI", "Kochi"),
    ("VOGO", "Goa"),
]

#: Fallback airport rows if the OurAirports download is unavailable.
FALLBACK_AIRPORTS = [
    ("VIDP", "DEL", "Indira Gandhi International", "Delhi", 28.5665, 77.1031, 75),
    ("VABB", "BOM", "Chhatrapati Shivaji Maharaj International", "Mumbai", 19.0887, 72.8679, 52),
    ("VOBL", "BLR", "Kempegowda International", "Bengaluru", 13.1979, 77.7063, 42),
    ("VOMM", "MAA", "Chennai International", "Chennai", 12.9941, 80.1709, 38),
    ("VECC", "CCU", "Netaji Subhas Chandra Bose International", "Kolkata", 22.6547, 88.4467, 35),
    ("VOHS", "HYD", "Rajiv Gandhi International", "Hyderabad", 17.2313, 78.4298, 36),
    ("VAAH", "AMD", "Sardar Vallabhbhai Patel International", "Ahmedabad", 23.0772, 72.6347, 28),
    ("VOCI", "COK", "Cochin International", "Kochi", 10.1520, 76.4019, 24),
    ("VOGO", "GOI", "Goa Dabolim International", "Goa", 15.3801, 73.8333, 20),
]

FLEET_TYPES = [("A320", 180), ("A321", 222), ("B738", 189)]
TIERS = ["BASE", "BASE", "BASE", "BASE", "SILVER", "SILVER", "GOLD", "PLATINUM"]
FARE_CLASSES = ["Y", "Y", "Y", "M", "M", "B", "J"]
SPECIAL_NEEDS = [None, None, None, None, "WCHR", "UMNR", "MEDA"]


def seed(
    *, reset: bool, n_flights: int, n_pnrs: int, n_crew: int, n_tails: int
) -> None:
    if reset:
        print("dropping all tables")
        drop_all()
    create_all()

    db = SessionLocal()
    try:
        existing = db.scalar(select(func.count()).select_from(Flight)) or 0
        if existing and not reset:
            print(
                f"{existing} flights already present — nothing to do. "
                "Use --reset to reseed from scratch."
            )
            return

        airports = seed_airports(db)
        tails = seed_fleet(db, n_tails)
        flights = seed_flights(db, tails, n_flights)
        crew = seed_crew(db, flights, tails, n_crew)
        pnrs, pax = seed_passengers(db, flights, n_pnrs)
        db.commit()

        print(
            f"\nseeded: {airports} airports, {len(tails)} tails, "
            f"{len(flights)} flights, {len(crew)} crew, {pnrs} PNRs, {pax} passengers"
        )
        print(f"database: {settings.resolved_database_url}")
        aog = [t for t in tails if t.status == "AOG"]
        if aog:
            print(
                f"note: {aog[0].registration} is flagged AOG "
                "(seeded for the Phase 2 AOG scenario)"
            )
    finally:
        db.close()


# --------------------------------------------------------------------------
# Airports — the one real (keyless) dataset
# --------------------------------------------------------------------------


def seed_airports(db) -> int:
    wanted = {HUB, *(icao for icao, _ in SPOKES)}
    rows = _fetch_ourairports(wanted)
    source = "OurAirports CSV"
    if not rows:
        rows = [
            {
                "icao": icao,
                "iata": iata,
                "name": name,
                "city": city,
                "country": "IN",
                "lat": lat,
                "lon": lon,
                "hourly_capacity": cap,
            }
            for icao, iata, name, city, lat, lon, cap in FALLBACK_AIRPORTS
        ]
        source = "built-in fallback table"

    capacities = {icao: cap for icao, _, _, _, _, _, cap in FALLBACK_AIRPORTS}
    count = 0
    for row in rows:
        if db.scalar(select(Airport).where(Airport.icao == row["icao"])):
            continue
        db.add(
            Airport(
                icao=row["icao"],
                iata=row["iata"],
                name=row["name"],
                city=row["city"],
                country=row["country"],
                lat=row["lat"],
                lon=row["lon"],
                hourly_capacity=capacities.get(row["icao"], 40),
            )
        )
        count += 1
    db.flush()
    print(f"airports: {count} loaded from {source}")
    return count


def _fetch_ourairports(wanted: set[str]) -> list[dict]:
    """Download (and cache) the OurAirports CSV, filtered to our network."""
    text: str | None = None
    if CACHE.exists():
        text = CACHE.read_text(encoding="utf-8")
        print(f"airports: using cached {CACHE.relative_to(REPO_ROOT)}")
    else:
        try:
            print("airports: downloading OurAirports CSV (no key required)...")
            with httpx.Client(timeout=45.0, follow_redirects=True) as client:
                resp = client.get(OURAIRPORTS_CSV)
                resp.raise_for_status()
            text = resp.text
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"airports: download failed ({type(exc).__name__}: {exc})")
            return []

    out: list[dict] = []
    for row in csv.DictReader(io.StringIO(text)):
        ident = (row.get("ident") or "").strip().upper()
        if ident not in wanted:
            continue
        try:
            lat = float(row["latitude_deg"])
            lon = float(row["longitude_deg"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(
            {
                "icao": ident,
                "iata": (row.get("iata_code") or "").strip() or None,
                "name": (row.get("name") or ident)[:160],
                "city": (row.get("municipality") or "").strip() or None,
                "country": (row.get("iso_country") or "").strip() or None,
                "lat": lat,
                "lon": lon,
            }
        )
    return out


# --------------------------------------------------------------------------
# Fleet, schedule, crew, passengers — all synthetic
# --------------------------------------------------------------------------


def seed_fleet(db, n_tails: int) -> list[Aircraft]:
    tails: list[Aircraft] = []
    for i in range(n_tails):
        type_code, seats = FLEET_TYPES[i % len(FLEET_TYPES)]
        # One tail is deliberately AOG and one is a spare parked at the hub:
        # the first feeds the Phase 2 AOG scenario, the second gives the
        # Recovery agent a real swap option in Phase 1.
        status = "AOG" if i == n_tails - 1 else "SERVICEABLE"
        tails.append(
            Aircraft(
                registration=f"VT-{chr(65 + i)}{chr(65 + (i * 7) % 26)}{chr(65 + (i * 3) % 26)}",
                type_code=type_code,
                seats=seats,
                base_icao=HUB,
                location_icao=HUB,
                status=status,
                mel_note=(
                    "Hydraulic system 2 leak — AOG pending part from MRO"
                    if status == "AOG"
                    else None
                ),
            )
        )
    db.add_all(tails)
    db.flush()
    print(f"fleet: {len(tails)} tails ({sum(t.status == 'AOG' for t in tails)} AOG)")
    return tails


def seed_flights(db, tails: list[Aircraft], n_flights: int) -> list[Flight]:
    """Build a hub schedule with most departures inside the next 6 hours.

    The fog scenario looks at DEL departures in a 6-hour window, so the
    schedule is anchored on "now" rather than on a calendar date — the demo
    works whenever you run it.
    """
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    serviceable = [t for t in tails if t.status == "SERVICEABLE"]
    # Hold one tail back as an unassigned spare for the swap option.
    rotation_tails = serviceable[:-1] if len(serviceable) > 1 else serviceable
    spare = serviceable[-1] if len(serviceable) > 1 else None

    flights: list[Flight] = []
    gate_cycle = iter(GATE_POOL * 4)

    # Outbound departures from the hub, spread across the detection window.
    n_out = n_flights // 2
    for i in range(n_out):
        icao, _ = SPOKES[i % len(SPOKES)]
        std = now + timedelta(minutes=45 + i * 28)
        block = timedelta(minutes=95 + (i % 4) * 25)
        tail = rotation_tails[i % len(rotation_tails)] if rotation_tails else None
        flights.append(
            Flight(
                flight_no=f"AI{200 + i}",
                origin=HUB,
                destination=icao,
                std=std,
                sta=std + block,
                etd=std,
                eta=std + block,
                status="SCHEDULED",
                gate=next(gate_cycle),
                aircraft_id=tail.id if tail else None,
                scheduled_aircraft_id=tail.id if tail else None,
            )
        )

    # Return legs on the same tails — these are the knock-on rotation.
    for i in range(n_flights - n_out):
        icao, _ = SPOKES[i % len(SPOKES)]
        std = now + timedelta(minutes=270 + i * 33)
        block = timedelta(minutes=100 + (i % 3) * 20)
        tail = rotation_tails[i % len(rotation_tails)] if rotation_tails else None
        flights.append(
            Flight(
                flight_no=f"AI{400 + i}",
                origin=icao,
                destination=HUB,
                std=std,
                sta=std + block,
                etd=std,
                eta=std + block,
                status="SCHEDULED",
                gate=next(gate_cycle),
                aircraft_id=tail.id if tail else None,
                scheduled_aircraft_id=tail.id if tail else None,
            )
        )

    db.add_all(flights)
    db.flush()

    outbound = sum(1 for f in flights if f.origin == HUB)
    print(
        f"schedule: {len(flights)} flights ({outbound} departing {HUB} "
        f"in the next {(flights[n_out - 1].std - now).total_seconds() / 3600:.1f} h)"
    )
    if spare:
        print(f"schedule: {spare.registration} left unassigned as a spare at {HUB}")
    return flights


def seed_crew(
    db, flights: list[Flight], tails: list[Aircraft], n_crew: int
) -> list[Crew]:
    roles = ["CP", "FO", "CC", "CC"]
    all_types = ",".join(t for t, _ in FLEET_TYPES)
    crew: list[Crew] = []

    hub_departures = [f for f in flights if f.origin == HUB]

    for i in range(n_crew):
        role = roles[i % len(roles)]
        # The last three are standby — the Recovery agent needs someone to
        # call when an FTL check fails.
        standby = i >= n_crew - 3
        # One crew member is deliberately close to their duty limit so the
        # fog delay produces a real FTL breach rather than a clean pass.
        tight = i == 0
        member = Crew(
            crew_code=f"{role}{100 + i}",
            name=fake.name(),
            role=role,
            base_icao=HUB,
            qualified_types=all_types,
            on_standby=standby,
            duty_minutes_so_far=610 if tight else 120 + (i * 23) % 260,
            max_duty_minutes=780,
            rest_hours_before_duty=11.5 if not tight else 10.5,
            assigned_flight_id=(
                None
                if standby or not hub_departures
                else hub_departures[i % len(hub_departures)].id
            ),
        )
        member.scheduled_flight_id = member.assigned_flight_id
        if not standby and member.assigned_flight_id:
            flight = next(
                f for f in hub_departures if f.id == member.assigned_flight_id
            )
            member.duty_start = flight.std - timedelta(minutes=70)
        crew.append(member)

    db.add_all(crew)
    db.flush()
    print(
        f"crew: {len(crew)} members "
        f"({sum(c.on_standby for c in crew)} standby, 1 near duty limit)"
    )
    return crew


def seed_passengers(db, flights: list[Flight], n_pnrs: int) -> tuple[int, int]:
    """PNRs weighted onto hub departures, some with onward connections."""
    hub_departures = [f for f in flights if f.origin == HUB]
    inbound = [f for f in flights if f.destination == HUB]
    if not hub_departures:
        return 0, 0

    pnr_count = 0
    pax_count = 0

    for i in range(n_pnrs):
        flight = hub_departures[i % len(hub_departures)]
        party = 1 if i % 4 else min(1 + (i % 3), 3)
        tier = TIERS[i % len(TIERS)]

        # Roughly a third of bookings carry an onward connection off a later
        # leg, which is what makes misconnection counting non-trivial. The
        # stride is 3 against 10 departures on purpose: with a stride that
        # shares a factor with the departure count, some flights (including
        # the first, which the fog scenario picks) would never get one.
        connection = None
        mct = 60
        if i % 3 == 0 and inbound:
            candidate = inbound[i % len(inbound)]
            if candidate.etd > flight.sta:
                connection = candidate
                mct = 45 if tier in {"GOLD", "PLATINUM"} else 75

        pnr = Pnr(
            record_locator=_locator(i),
            flight_id=flight.id,
            tier=tier,
            fare_class=FARE_CLASSES[i % len(FARE_CLASSES)],
            pax_count=party,
            special_needs=SPECIAL_NEEDS[i % len(SPECIAL_NEEDS)],
            connection_flight_id=connection.id if connection else None,
            connection_mct_minutes=mct,
            status="BOOKED",
        )
        db.add(pnr)
        db.flush()
        pnr_count += 1

        for p in range(party):
            name = fake.name()
            db.add(
                Passenger(
                    pnr_id=pnr.id,
                    name=name,
                    email=_email(name, i, p),
                    phone=fake.msisdn()[:12],
                    language="en",
                    # VIPs track the top tiers plus the lead passenger only.
                    is_vip=(tier == "PLATINUM" and p == 0),
                    # A few passengers have opted out — the Comms agent must
                    # skip them, and the demo should show that happening.
                    consent_email=not (i % 17 == 0),
                )
            )
            pax_count += 1

    db.flush()
    connections = sum(
        1
        for p in db.scalars(select(Pnr)).all()
        if p.connection_flight_id is not None
    )
    no_consent = sum(
        1 for p in db.scalars(select(Passenger)).all() if not p.consent_email
    )
    print(
        f"passengers: {pnr_count} PNRs / {pax_count} pax "
        f"({connections} with onward connections, {no_consent} without email consent)"
    )
    return pnr_count, pax_count


def _locator(i: int) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    return (
        alphabet[i % 24]
        + alphabet[(i // 24) % 24]
        + alphabet[(i * 7) % 24]
        + f"{i:03d}"
    )


def _email(name: str, i: int, p: int) -> str:
    handle = name.lower().replace(" ", ".").replace("'", "")
    return f"{handle}.{i}{p}@example.invalid"


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Phase 1 dataset")
    parser.add_argument("--reset", action="store_true", help="drop tables first")
    parser.add_argument("--flights", type=int, default=20)
    parser.add_argument("--pnrs", type=int, default=90)
    parser.add_argument("--crew", type=int, default=15)
    parser.add_argument("--tails", type=int, default=6)
    args = parser.parse_args()

    seed(
        reset=args.reset,
        n_flights=args.flights,
        n_pnrs=args.pnrs,
        n_crew=args.crew,
        n_tails=args.tails,
    )


if __name__ == "__main__":
    main()
