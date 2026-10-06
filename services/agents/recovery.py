"""Recovery agent — propose 2-3 options.

This is where Claude earns its place: given a context pack of real numbers,
write plausible recovery options with a cost/delay/pax-impact estimate each.

Everything load-bearing stays in Python:
  * the context pack is assembled from `R` tools (availability, standby crew,
    alternate inventory) — Claude never queries anything itself;
  * hard constraints (crew FTL, seat counts, tail availability) are checked
    *after* Claude answers, in `_apply_guardrails`, and an option that fails
    regulatory check is discarded rather than ranked (section 8);
  * the option score is the section 8 formula, computed here.

Without `ANTHROPIC_API_KEY` the `_stub` path returns the same option shapes
built from the same numbers, so the loop still runs end to end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from services.agents.llm import AGENT_MODEL, structured
from services.core.models import Flight
from services.core.schemas import (
    RecoveryAction,
    RecoveryOptionOut,
    RecoveryPlan,
)
from services.mcp import crew, flightops, pss
from services.mcp.registry import check_permission

AGENT = "recovery"


@dataclass
class ScoredOption:
    """An option plus the verdicts Python assigned it.

    The LLM never sets these four fields — guardrails and the section 8 score
    do, which is why they live outside the Pydantic output schema.
    """

    option: RecoveryOptionOut
    score: float
    regulatory_ok: bool
    crew_legal: bool


SYSTEM = """You are the Recovery Planning agent in an airline disruption \
management system. Given a disruption and a context pack of verified \
operational data, propose 2 or 3 distinct recovery options.

Rules:
- Use ONLY the flights, tails, crew and inventory listed in the context pack. \
Never invent a registration, flight number or crew code.
- Each option must be a genuinely different strategy. Prefer one RETIME, one \
SWAP_AIRCRAFT (only if a spare tail is listed), and one CANCEL.
- CRITICAL: if `crew_legality_at_expected_delay.breach` is true, then every \
option that still operates the flight (RETIME or SWAP_AIRCRAFT) MUST include a \
`crew.assign_crew` action naming crew from `standby_crew`. The rostered crew \
would exceed their duty limit, so an option without a crew swap is illegal \
and will be discarded. Alternatively keep the option's `delay_minutes` at or \
below `max_legal_delay_minutes`.
- Cost estimates in USD must be defensible from the numbers given: delay \
compensation, rebooking, crew overtime, lost revenue on a cancellation.
- `delay_minutes` is the delay the option *results in*, not the delay avoided. \
For CANCEL, use 0.
- `pax_impacted` means passengers whose journey is materially changed \
(rebooked, misconnected or stranded) — not everyone on board.
- `feasibility` is 0-1: how confident you are the option can actually be \
executed with the listed resources in the time available.
- Each action's `tool` must be one of: flightops.retime_flight, \
flightops.swap_aircraft, flightops.cancel_flight, pss.rebook_passengers, \
aodb.reassign_gate, crew.assign_crew.
- Keep `summary` under 200 characters; put the reasoning in `rationale`."""


def build_context(
    db: Session, *, flight_ids: list[int], airport: str, delay_minutes: int,
    impact_detail: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the read-only context pack Claude reasons over."""
    primary = db.get(Flight, flight_ids[0]) if flight_ids else None
    if primary is None:
        return {}

    window_start = primary.etd
    window_end = primary.etd + timedelta(minutes=delay_minutes + 120)

    check_permission(AGENT, "flightops.get_aircraft_availability")
    spare_tails = flightops.get_aircraft_availability(
        db, at_icao=airport, window_start=window_start, window_end=window_end
    )

    check_permission(AGENT, "flightops.get_maintenance_status")
    maintenance = flightops.get_maintenance_status(db)

    type_code = primary.aircraft.type_code if primary.aircraft else "A320"
    check_permission(AGENT, "crew.find_standby_crew")
    standby = crew.find_standby_crew(db, at_icao=airport, type_code=type_code)

    check_permission(AGENT, "pss.get_inventory")
    # Only the primary flight is excluded. The other flights in the event are
    # delayed, not cancelled, so they are still legitimate places to
    # reaccommodate passengers — excluding the whole event would leave a
    # cancellation with nowhere to put anyone.
    inventory = pss.get_inventory(
        db,
        origin=primary.origin,
        destination=primary.destination,
        after=window_start,
        before=window_start + timedelta(hours=24),
        exclude_flight_ids=[primary.id],
    )

    check_permission(AGENT, "pss.get_affected_passengers")
    pax = pss.get_affected_passengers(db, [primary.id])

    # Connection exposure for the *primary* flight only. The impact report
    # counts it across the whole event, which would overstate the cost of an
    # option that only touches one flight — and make options incomparable.
    check_permission(AGENT, "pss.get_connections_at_risk")
    primary_connections = pss.get_connections_at_risk(
        db, [primary.id], delay_minutes=delay_minutes
    )

    # Crew legality for the primary flight at the expected delay. If this
    # breaches, any option that keeps flying needs a crew swap to be legal.
    check_permission(AGENT, "crew.check_duty_limits")
    duty = crew.check_duty_limits(db, primary.id, delay_minutes=delay_minutes)

    return {
        "primary_flight": {
            "id": primary.id,
            "flight_no": primary.flight_no,
            "route": f"{primary.origin}-{primary.destination}",
            "std": primary.std.isoformat(),
            "etd": primary.etd.isoformat(),
            "registration": primary.aircraft.registration if primary.aircraft else None,
            "type_code": type_code,
            "seats": primary.aircraft.seats if primary.aircraft else None,
            "booked_pax": pax["pax_count"],
            "gate": primary.gate,
        },
        "other_affected_flight_ids": flight_ids[1:],
        "expected_delay_minutes": delay_minutes,
        "impact": {
            "event_pax_count": impact_detail.get("pnr_count"),
            "event_connection_pax": impact_detail.get("connection_pax"),
            "primary_flight_connection_pax": primary_connections["pax_count"],
            "by_tier": impact_detail.get("by_tier"),
        },
        "crew_legality_at_expected_delay": {
            "breach": duty["breach"],
            "max_legal_delay_minutes": duty["max_legal_delay_minutes"],
            "reasons": [r for v in duty["crew"] for r in v["reasons"]][:4],
            "rostered_crew_codes": [v["crew_code"] for v in duty["crew"]],
        },
        "spare_tails_at_airport": spare_tails,
        "aircraft_with_open_defects": maintenance,
        "standby_crew": [
            {"crew_code": c["crew_code"], "role": c["role"]} for c in standby
        ],
        "alternate_flights": inventory,
    }


def plan(
    db: Session,
    *,
    flight_ids: list[int],
    airport: str,
    delay_minutes: int,
    impact_detail: dict[str, Any],
    disruption_summary: str,
) -> tuple[list[ScoredOption], str, dict[str, Any]]:
    """Return `(scored_options, source, context)`.

    `source` is "claude" or "stub". Options that fail a hard constraint are
    dropped before scoring, so the caller can assume every option returned is
    executable.
    """
    context = build_context(
        db,
        flight_ids=flight_ids,
        airport=airport,
        delay_minutes=delay_minutes,
        impact_detail=impact_detail,
    )
    if not context:
        return [], "stub", {}

    prompt = (
        f"Disruption: {disruption_summary}\n\n"
        f"Context pack (verified operational data):\n"
        f"{json.dumps(context, indent=2, default=str)}\n\n"
        "Propose 2-3 recovery options."
    )

    plan_out, source = structured(
        RecoveryPlan,
        system=SYSTEM,
        prompt=prompt,
        stub=lambda: _stub(context, delay_minutes),
        model=AGENT_MODEL,
        max_tokens=4096,
    )

    scored = _apply_guardrails(db, plan_out.options, context, airport)
    return scored, source, context


# --------------------------------------------------------------------------
# Guardrails and scoring — always Python
# --------------------------------------------------------------------------

ALLOWED_TOOLS = {
    "flightops.retime_flight",
    "flightops.swap_aircraft",
    "flightops.cancel_flight",
    "pss.rebook_passengers",
    "aodb.reassign_gate",
    "crew.assign_crew",
}


def _apply_guardrails(
    db: Session,
    options: list[RecoveryOptionOut],
    context: dict[str, Any],
    airport: str,
) -> list[ScoredOption]:
    """Mark each option regulatory_ok / crew_legal, then score and rank.

    Returns only the options that survive. An option is rejected outright
    when it names a resource that doesn't exist or a tool it may not use —
    i.e. when the model has drifted from the context pack.
    """
    primary = context["primary_flight"]
    flight = db.get(Flight, primary["id"])
    spare_regs = {t["registration"] for t in context["spare_tails_at_airport"]}
    standby_codes = {c["crew_code"] for c in context["standby_crew"]}

    survivors: list[tuple[RecoveryOptionOut, bool, bool]] = []
    for option in options:
        regulatory_ok = True
        crew_legal = True
        problems: list[str] = []

        for action in option.actions:
            if action.tool not in ALLOWED_TOOLS:
                regulatory_ok = False
                problems.append(f"unknown tool {action.tool}")
                continue
            if action.tool == "flightops.swap_aircraft":
                reg = action.args.get("registration")
                if reg not in spare_regs:
                    regulatory_ok = False
                    problems.append(f"tail {reg} is not an available spare")
                else:
                    tail = next(
                        t for t in context["spare_tails_at_airport"]
                        if t["registration"] == reg
                    )
                    if tail["seats"] < primary["booked_pax"]:
                        # Not fatal — it means an overflow rebook, which is a
                        # legitimate plan. Record it against feasibility.
                        problems.append(
                            f"{reg} seats {tail['seats']} < "
                            f"{primary['booked_pax']} booked (overflow rebook)"
                        )
            if action.tool == "crew.assign_crew":
                codes = action.args.get("crew_codes") or []
                unknown = [c for c in codes if c not in standby_codes]
                if unknown:
                    regulatory_ok = False
                    problems.append(f"crew {unknown} not on standby")

        # Crew legality under this option's resulting delay.
        if flight is not None and option.kind != "CANCEL":
            check = crew.check_duty_limits(
                db, flight.id, delay_minutes=option.delay_minutes
            )
            if check["breach"]:
                replaces_crew = any(
                    a.tool == "crew.assign_crew" for a in option.actions
                )
                # A crew swap resolves the breach, so the option stays legal.
                # Without one the option is simply not flyable — discard it
                # rather than rank it (section 8: regulatory_ok=false is never
                # ranked).
                crew_legal = replaces_crew
                if not replaces_crew:
                    regulatory_ok = False
                    problems.append(
                        "crew FTL breach at +"
                        f"{option.delay_minutes} min with no crew swap"
                    )
                else:
                    problems.append(
                        f"rostered crew exceed duty at +{option.delay_minutes} "
                        "min — resolved by standby call-out"
                    )

        # Curfew: no departure may be pushed past 02:00 local at the hub.
        if flight is not None and option.kind == "RETIME":
            new_etd = flight.std + timedelta(minutes=option.delay_minutes)
            if 2 <= new_etd.hour < 5:
                regulatory_ok = False
                problems.append(f"new ETD {new_etd:%H:%MZ} falls inside curfew")

        if problems:
            option.rationale = (
                (option.rationale or "") + " | guardrails: " + "; ".join(problems)
            ).strip()

        if regulatory_ok:
            survivors.append((option, regulatory_ok, crew_legal))

    if not survivors:
        return []

    costs = [o.cost_usd for o, _, _ in survivors]
    paxes = [o.pax_impacted for o, _, _ in survivors]
    delays = [o.delay_minutes for o, _, _ in survivors]

    scored: list[ScoredOption] = []
    for option, regulatory_ok, crew_legal in survivors:
        option.feasibility = round(min(max(option.feasibility, 0.0), 1.0), 2)
        scored.append(
            ScoredOption(
                option=option,
                score=option_score(
                    cost=option.cost_usd,
                    pax_impacted=option.pax_impacted,
                    delay_minutes=option.delay_minutes,
                    feasibility=option.feasibility,
                    crew_legal=crew_legal,
                    cost_range=(min(costs), max(costs)),
                    pax_range=(min(paxes), max(paxes)),
                    delay_range=(min(delays), max(delays)),
                ),
                regulatory_ok=regulatory_ok,
                crew_legal=crew_legal,
            )
        )

    scored.sort(key=lambda s: s.score, reverse=True)
    return scored


def option_score(
    *,
    cost: float,
    pax_impacted: int,
    delay_minutes: int,
    feasibility: float,
    crew_legal: bool,
    cost_range: tuple[float, float],
    pax_range: tuple[int, int],
    delay_range: tuple[int, int],
) -> float:
    """Architecture doc section 8, verbatim.

    `0.30*(1-norm(cost)) + 0.30*(1-norm(pax_impacted)) + 0.20*feasibility
     + 0.10*(1-norm(delay_min)) + 0.10*crew_legal`

    Normalisation is min-max across the candidate set, so the score ranks
    options against each other rather than against an absolute scale.
    """
    score = (
        0.30 * (1 - _norm(cost, cost_range))
        + 0.30 * (1 - _norm(pax_impacted, pax_range))
        + 0.20 * min(max(feasibility, 0.0), 1.0)
        + 0.10 * (1 - _norm(delay_minutes, delay_range))
        + 0.10 * (1.0 if crew_legal else 0.0)
    )
    return round(min(max(score, 0.0), 1.0), 3)


def _norm(value: float, span: tuple[float, float]) -> float:
    low, high = span
    if high <= low:
        return 0.0
    return min(max((value - low) / (high - low), 0.0), 1.0)


# --------------------------------------------------------------------------
# Deterministic fallback (no ANTHROPIC_API_KEY)
# --------------------------------------------------------------------------


def _stub(context: dict[str, Any], delay_minutes: int) -> RecoveryPlan:
    """Two or three hard-coded option templates with the real numbers plugged in.

    Same schema, same actions, same units as the Claude path — only the prose
    is templated.
    """
    primary = context["primary_flight"]
    fid = primary["id"]
    booked = primary["booked_pax"] or 0
    connections = context["impact"].get("primary_flight_connection_pax") or 0
    spares = context["spare_tails_at_airport"]
    alternates = context["alternate_flights"]

    legality = context.get("crew_legality_at_expected_delay") or {}
    crew_breach = bool(legality.get("breach"))
    standby_codes = [c["crew_code"] for c in context["standby_crew"]]

    def crew_swap_actions() -> list[RecoveryAction]:
        """A crew swap, but only when the delay actually busts duty.

        Mirrors the rule given to Claude in SYSTEM: an option that keeps
        operating with illegal crew is discarded by the guardrails, so the
        fallback has to solve the same problem the same way.
        """
        if not (crew_breach and standby_codes):
            return []
        return [
            RecoveryAction(
                tool="crew.assign_crew",
                flight_id=fid,
                args={"crew_codes": standby_codes[:2]},
            )
        ]

    options: list[RecoveryOptionOut] = []

    # 1. Retime and hold the aircraft.
    retime_actions = [
        RecoveryAction(
            tool="flightops.retime_flight",
            flight_id=fid,
            args={"delay_minutes": delay_minutes},
        ),
        RecoveryAction(tool="aodb.reassign_gate", flight_id=fid, args={}),
        *crew_swap_actions(),
    ]
    options.append(
        RecoveryOptionOut(
            kind="RETIME",
            label=f"Retime {primary['flight_no']} by {delay_minutes} min",
            summary=(
                f"Hold {primary['flight_no']} until the low-visibility period "
                f"clears; protect {connections} connecting passengers on arrival."
            ),
            actions=retime_actions,
            cost_usd=round(
                booked * 22.0
                + delay_minutes * 38.0
                + (4200.0 if crew_breach else 0.0),
                2,
            ),
            delay_minutes=delay_minutes,
            pax_impacted=connections,
            feasibility=0.86 if not crew_breach else 0.74,
            rationale=(
                "Lowest-cost option: aircraft stays on its own rotation. Cost "
                "is delay compensation plus ground time"
                + (
                    "; the rostered crew would exceed their duty limit at this "
                    "delay, so standby crew are called out."
                    if crew_breach
                    else "."
                )
            ),
        )
    )

    # 2. Swap to a spare tail, if one is actually free.
    if spares:
        tail = max(spares, key=lambda t: t["seats"])
        shortfall = max(booked - tail["seats"], 0)
        target = alternates[0] if alternates else None
        swap_delay = max(delay_minutes // 3, 30)
        # A shorter delay may stay inside the crew's duty limit on its own —
        # only call out standby crew if it genuinely doesn't.
        max_legal = legality.get("max_legal_delay_minutes")
        swap_needs_crew = crew_breach and (
            max_legal is None or swap_delay > max_legal
        )
        actions = [
            RecoveryAction(
                tool="flightops.swap_aircraft",
                flight_id=fid,
                args={"registration": tail["registration"]},
            ),
            RecoveryAction(
                tool="flightops.retime_flight",
                flight_id=fid,
                args={"delay_minutes": swap_delay},
            ),
            *(crew_swap_actions() if swap_needs_crew else []),
        ]
        if shortfall and target:
            actions.append(
                RecoveryAction(
                    tool="pss.rebook_passengers",
                    flight_id=fid,
                    args={"target_flight_id": target["flight_id"]},
                )
            )
        options.append(
            RecoveryOptionOut(
                kind="SWAP_AIRCRAFT",
                label=f"Swap to {tail['registration']} and depart earlier",
                summary=(
                    f"Move {primary['flight_no']} onto spare tail "
                    f"{tail['registration']} ({tail['seats']} seats) and cut the "
                    f"delay to {swap_delay} min."
                ),
                actions=actions,
                cost_usd=round(
                    booked * 11.0
                    + 7400.0
                    + shortfall * 180.0
                    + (4200.0 if swap_needs_crew else 0.0),
                    2,
                ),
                delay_minutes=swap_delay,
                pax_impacted=shortfall or max(connections // 3, 0),
                feasibility=0.68,
                rationale=(
                    f"Cuts passenger delay sharply. Repositioning "
                    f"{tail['registration']} costs ground handling and crew "
                    "overtime"
                    + (
                        f"; {shortfall} passengers need reaccommodation on the "
                        "smaller cabin."
                        if shortfall
                        else "."
                    )
                ),
            )
        )

    # 3. Cancel and reaccommodate.
    target = alternates[0] if alternates else None
    options.append(
        RecoveryOptionOut(
            kind="CANCEL",
            label=f"Cancel {primary['flight_no']} and reaccommodate",
            summary=(
                f"Cancel {primary['flight_no']}; move {booked} passengers onto "
                + (f"{target['flight_no']}" if target else "the next available service")
                + " and release the slot."
            ),
            actions=[
                RecoveryAction(tool="flightops.cancel_flight", flight_id=fid, args={}),
                RecoveryAction(
                    tool="pss.rebook_passengers",
                    flight_id=fid,
                    args={
                        "target_flight_id": target["flight_id"] if target else None
                    },
                ),
            ],
            cost_usd=round(booked * 165.0 + 12000.0, 2),
            delay_minutes=0,
            pax_impacted=booked,
            feasibility=0.95,
            rationale=(
                "Always executable and relieves pressure on the hub, but every "
                "passenger is re-accommodated and the revenue is lost. Use when "
                "the weather window is not expected to clear."
            ),
        )
    )

    return RecoveryPlan(options=options[:3])
