"""weather domain — the one genuinely live, keyless external feed in Phase 1.

* METAR/TAF from aviationweather.gov (no key)
* Hourly forecast from Open-Meteo (no key)

`demo/inject` can install an override so the Fog-at-DEL scenario is reliable
regardless of the actual weather at Delhi. Overrides are flagged in the
returned payload (`source == "demo_override"`) so nothing silently pretends
injected weather is real.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

AWC_BASE = "https://aviationweather.gov/api/data"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 8.0

# Phase 1 override store: a module global. Phase 3 moves this into Redis so it
# survives across the gateway and the MCP server processes.
_OVERRIDES: dict[str, dict[str, Any]] = {}


@dataclass
class WeatherOverride:
    icao: str
    visibility_m: int
    ceiling_ft: int
    wx: str = "FG"
    note: str = "demo injection"
    raw: str = field(default="")


def set_override(override: WeatherOverride) -> None:
    _OVERRIDES[override.icao.upper()] = {
        "icao": override.icao.upper(),
        "source": "demo_override",
        "raw": override.raw
        or (
            f"{override.icao.upper()} "
            f"{datetime.now(timezone.utc):%d%H%MZ} 00000KT "
            f"{override.visibility_m:04d} {override.wx} "
            f"VV{override.ceiling_ft // 100:03d} 08/08 Q1017"
        ),
        "visibility_m": override.visibility_m,
        "ceiling_ft": override.ceiling_ft,
        "wx": override.wx,
        "wind_kt": 0,
        "temp_c": 8,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "note": override.note,
    }


def clear_overrides() -> None:
    _OVERRIDES.clear()


def active_overrides() -> list[str]:
    return sorted(_OVERRIDES)


# --------------------------------------------------------------------------
# Tools (R)
# --------------------------------------------------------------------------


def get_metar(icao: str) -> dict[str, Any]:
    """Current observation for `icao`, parsed. Falls back gracefully offline."""
    key = icao.upper()
    if key in _OVERRIDES:
        return dict(_OVERRIDES[key])

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                f"{AWC_BASE}/metar",
                params={"ids": key, "format": "json", "taf": "false"},
                headers={"User-Agent": "disruption-orchestrator/0.1"},
            )
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:  # network down, API moved, quota — stay usable
        return {
            "icao": key,
            "source": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "visibility_m": None,
            "ceiling_ft": None,
        }

    if not rows:
        return {"icao": key, "source": "aviationweather.gov", "error": "no report"}

    row = rows[0]
    raw = row.get("rawOb") or ""
    return {
        "icao": key,
        "source": "aviationweather.gov",
        "raw": raw,
        "visibility_m": _visibility_metres(row, raw),
        "ceiling_ft": _ceiling_ft(row),
        "wx": row.get("wxString") or "",
        "wind_kt": row.get("wspd"),
        "temp_c": row.get("temp"),
        "observed_at": row.get("reportTime"),
    }


def get_taf(icao: str, hours: int = 12) -> dict[str, Any]:
    """Terminal forecast for `icao`."""
    key = icao.upper()
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                f"{AWC_BASE}/taf",
                params={"ids": key, "format": "json"},
                headers={"User-Agent": "disruption-orchestrator/0.1"},
            )
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:
        return {
            "icao": key,
            "source": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "periods": [],
        }

    if not rows:
        return {"icao": key, "source": "aviationweather.gov", "periods": []}

    row = rows[0]
    periods = [
        {
            "from": fc.get("timeFrom"),
            "to": fc.get("timeTo"),
            "visibility_m": _as_metres(fc.get("visib")),
            "wx": fc.get("wxString") or "",
        }
        for fc in (row.get("fcsts") or [])[: max(hours // 3, 1)]
    ]
    return {
        "icao": key,
        "source": "aviationweather.gov",
        "raw": row.get("rawTAF") or "",
        "periods": periods,
    }


def get_forecast(lat: float, lon: float, hours: int = 12) -> dict[str, Any]:
    """Hourly visibility / gusts / precipitation from Open-Meteo."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                OPEN_METEO,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "visibility,wind_gusts_10m,precipitation",
                    "forecast_days": 2,
                    "timezone": "UTC",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        return {
            "source": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "hourly": [],
        }

    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])[:hours]
    return {
        "source": "open-meteo",
        "hourly": [
            {
                "time": times[i],
                "visibility_m": _safe_index(hourly.get("visibility"), i),
                "gust_kmh": _safe_index(hourly.get("wind_gusts_10m"), i),
                "precip_mm": _safe_index(hourly.get("precipitation"), i),
            }
            for i in range(len(times))
        ],
    }


# Rule-based thresholds, never left to the LLM (check #7, section 10).
LOW_VIS_CAT_III = 200
LOW_VIS_CAT_II = 400
LOW_VIS_CAT_I = 800
LOW_CEILING_FT = 200


def assess_weather_risk(icao: str, window_hours: int = 6) -> dict[str, Any]:
    """Risk 0-100 for `icao` with the reasons that produced it."""
    metar = get_metar(icao)
    vis = metar.get("visibility_m")
    ceiling = metar.get("ceiling_ft")
    wx = (metar.get("wx") or "").upper()

    score = 0
    reasons: list[str] = []

    if vis is not None:
        if vis <= LOW_VIS_CAT_III:
            score += 70
            reasons.append(f"visibility {vis} m — below CAT III minima")
        elif vis <= LOW_VIS_CAT_II:
            score += 55
            reasons.append(f"visibility {vis} m — CAT III ops only")
        elif vis <= LOW_VIS_CAT_I:
            score += 30
            reasons.append(f"visibility {vis} m — low visibility procedures likely")

    if ceiling is not None and ceiling <= LOW_CEILING_FT:
        score += 15
        reasons.append(f"ceiling {ceiling} ft")

    if "FG" in wx or "BR" in wx:
        score += 15
        reasons.append(f"present weather {wx or 'FG'}")

    if not reasons:
        reasons.append("no low-visibility criteria met")

    score = min(score, 100)
    level = "CRITICAL" if score >= 70 else "HIGH" if score >= 50 else (
        "MEDIUM" if score >= 25 else "LOW"
    )
    return {
        "icao": icao.upper(),
        "risk": score,
        "level": level,
        "reasons": reasons,
        "window_hours": window_hours,
        "metar_source": metar.get("source"),
        "raw": metar.get("raw"),
        "visibility_m": vis,
    }


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

_VIS_SM = re.compile(r"\b(\d+(?:/\d+)?)SM\b")
_VIS_M = re.compile(r"\s(\d{4})\s")


def _as_metres(value: Any) -> int | None:
    """aviationweather.gov reports statute miles; convert to metres."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace("+", "").strip()
        if "/" in value:
            try:
                num, den = value.split("/")
                value = float(num) / float(den)
            except (ValueError, ZeroDivisionError):
                return None
    try:
        return int(round(float(value) * 1609.34))
    except (TypeError, ValueError):
        return None


def _visibility_metres(row: dict[str, Any], raw: str) -> int | None:
    metres = _as_metres(row.get("visib"))
    if metres is not None:
        return metres
    # Fall back to the raw group (non-US stations report metres directly).
    match = _VIS_M.search(raw)
    if match:
        return int(match.group(1))
    match = _VIS_SM.search(raw)
    if match:
        return _as_metres(match.group(1))
    return None


def _ceiling_ft(row: dict[str, Any]) -> int | None:
    clouds = row.get("clouds") or []
    bases = [
        c["base"]
        for c in clouds
        if isinstance(c, dict)
        and c.get("base") is not None
        and c.get("cover") in {"BKN", "OVC", "VV"}
    ]
    return min(bases) if bases else None


def _safe_index(seq: Any, i: int) -> Any:
    if isinstance(seq, list) and i < len(seq):
        return seq[i]
    return None
