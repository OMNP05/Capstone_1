"""Detection agent — is anything wrong?

Phase 1 scope: the Fog-at-DEL scenario. It reads real weather (the one live
external call in this phase) plus the seeded flight list, and decides which
departures are disrupted.

The *decision* is rule-based on purpose — `weather.assess_weather_risk`
thresholds and a departure window — because a hallucinated disruption is the
one failure mode nobody can recover from downstream. Claude is not in this
path in Phase 1; Phase 2 generalises detection across four scenarios and that
is where the LLM earns its place.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from services.core.schemas import DisruptionEvent, Evidence
from services.mcp import aodb, crew, flightops, weather
from services.mcp.registry import check_permission

AGENT = "detection"

#: A fog event at the hub is assumed to suppress departures for this long.
#: CAT III fog at Delhi routinely runs past three hours, which also means the
#: run exercises the meal-entitlement threshold in `pss.issue_voucher`.
FOG_DELAY_MINUTES = 195
#: Departures within this window of "now" are in scope for the event.
DEPARTURE_WINDOW_HOURS = 6
#: Below this, detection reports nothing (section 8: confidence >= 0.6).
MIN_CONFIDENCE = 0.6


def detect_weather_disruption(
    db: Session, *, icao: str, now: datetime | None = None
) -> DisruptionEvent | None:
    """Assess `icao` and return a DisruptionEvent, or None if it's flyable."""
    now = now or datetime.now(timezone.utc)

    check_permission(AGENT, "weather.assess_weather_risk")
    risk = weather.assess_weather_risk(icao)

    check_permission(AGENT, "weather.get_taf")
    taf = weather.get_taf(icao, hours=DEPARTURE_WINDOW_HOURS)

    check_permission(AGENT, "aodb.get_airport_info")
    airport = aodb.get_airport_info(db, icao)

    # Open-Meteo: a second, independent keyless feed. Called even when a demo
    # override is active so the evidence trail always contains live data.
    forecast: dict[str, Any] = {"source": "skipped"}
    if airport:
        check_permission(AGENT, "weather.get_forecast")
        forecast = weather.get_forecast(
            airport["lat"], airport["lon"], hours=DEPARTURE_WINDOW_HOURS
        )

    if risk["risk"] < 50:
        return None

    check_permission(AGENT, "flightops.get_schedule")
    departures = flightops.get_schedule(
        db,
        origin=icao,
        after=now,
        before=now + timedelta(hours=DEPARTURE_WINDOW_HOURS),
    )
    departures = [d for d in departures if d["status"] != "CANCELLED"]
    if not departures:
        return None

    check_permission(AGENT, "aodb.get_airport_congestion")
    congestion = aodb.get_airport_congestion(db, icao, at=now, hours=DEPARTURE_WINDOW_HOURS)

    flight_ids = [d["id"] for d in departures]
    check_permission(AGENT, "crew.get_crew_roster")
    roster = crew.get_crew_roster(db, flight_ids)

    severity = (
        "CRITICAL" if risk["risk"] >= 85
        else "HIGH" if risk["risk"] >= 70
        else "MEDIUM"
    )
    # Confidence tracks the strength of the observation, not the forecast.
    confidence = min(0.5 + risk["risk"] / 200, 0.97)
    if risk.get("metar_source") == "unavailable":
        confidence -= 0.15
    if confidence < MIN_CONFIDENCE:
        return None

    first = departures[0]
    eta_minutes = max(
        int((datetime.fromisoformat(first["etd"]) - now).total_seconds() // 60), 0
    )

    evidence = [
        Evidence(
            source=f"weather.assess_weather_risk ({risk.get('metar_source')})",
            detail=f"risk {risk['risk']}/100 ({risk['level']}): " + "; ".join(risk["reasons"]),
        ),
        Evidence(
            source="weather.get_metar",
            detail=(risk.get("raw") or "no raw observation")[:220],
        ),
        Evidence(
            source=f"weather.get_taf ({taf.get('source')})",
            detail=(taf.get("raw") or f"{len(taf.get('periods') or [])} forecast periods")[:220],
        ),
        Evidence(
            source=f"weather.get_forecast ({forecast.get('source')})",
            detail=_forecast_detail(forecast),
        ),
        Evidence(
            source="flightops.get_schedule",
            detail=(
                f"{len(departures)} departures from {icao.upper()} in the next "
                f"{DEPARTURE_WINDOW_HOURS} h: "
                + ", ".join(d["flight_no"] for d in departures[:8])
                + ("..." if len(departures) > 8 else "")
            ),
        ),
        Evidence(
            source="aodb.get_airport_congestion",
            detail=(
                f"peak {congestion['peak_movements']} movements/h vs capacity "
                f"{congestion['capacity']}"
                + (" — over capacity" if congestion["congested"] else "")
            ),
        ),
        Evidence(
            source="crew.get_crew_roster",
            detail=f"{len(roster)} crew rostered across the affected departures",
        ),
    ]

    return DisruptionEvent(
        type="WEATHER_FOG",
        airport=icao.upper(),
        flight_ids=flight_ids,
        severity=severity,
        confidence=round(confidence, 2),
        eta_minutes=eta_minutes,
        summary=(
            f"Low visibility at {icao.upper()} "
            f"({risk.get('visibility_m') or '?'} m) — "
            f"{len(departures)} departures exposed to ~{FOG_DELAY_MINUTES} min "
            f"of low-visibility procedure delay"
        ),
        evidence=evidence,
    )


def dedupe_hash(event: DisruptionEvent, *, bucket: datetime | None = None) -> str:
    """Stable hash so the same event in the same hour isn't opened twice.

    Section 8 calls for a dedupe hash on detection; Phase 1 buckets by hour,
    which is enough to stop a double-click on "Inject" creating two rows.
    """
    bucket = bucket or datetime.now(timezone.utc)
    parts = [
        event.type,
        event.airport,
        bucket.strftime("%Y%m%d%H"),
        ",".join(str(i) for i in sorted(event.flight_ids)),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _forecast_detail(forecast: dict[str, Any]) -> str:
    hourly = forecast.get("hourly") or []
    if not hourly:
        return forecast.get("error") or "no hourly forecast available"
    visibilities = [h["visibility_m"] for h in hourly if h.get("visibility_m") is not None]
    if not visibilities:
        return f"{len(hourly)} hours returned, no visibility field"
    return (
        f"next {len(hourly)} h visibility min {min(visibilities):.0f} m / "
        f"max {max(visibilities):.0f} m"
    )
