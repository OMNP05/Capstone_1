"""Passenger Communication agent — draft and "send" the notifications.

Haiku drafts one message per affected PNR; `notify.send_email` writes it to
`notifications` with `status='logged'` and prints it. Nothing leaves the app
in Phase 1.

Rules enforced in Python, not by the model (section 8): consent, 30-minute
dedupe, tier-aware offers, and the delay thresholds that decide whether an
entitlement exists at all. The model writes prose; it does not decide who
gets compensated.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from services.agents.llm import COMMS_MODEL, structured
from services.core.models import Disruption, Notification, Pnr, RecoveryOption, utcnow
from services.core.schemas import NotificationDraft
from services.mcp import notify, pss
from services.mcp.registry import check_permission

AGENT = "passenger"

#: Don't message the same PNR twice inside this window.
DEDUPE_MINUTES = 30
#: Cap on how many PNRs get an individually drafted message in one run.
#: Keeps a Phase 1 demo from making 90 LLM calls; the rest get the template.
MAX_LLM_DRAFTS = 12

SYSTEM = """You write passenger notifications for an airline operations \
centre. One message, for one booking.

Requirements:
- Plain, calm, specific. No apology theatre, no marketing language.
- State what happened, what it means for this passenger's journey, and what \
happens next. Lead with the change to their flight.
- Use ONLY the facts given. Never invent a new departure time, gate, \
compensation amount or policy.
- If an entitlement is supplied, mention it in one sentence. If none is \
supplied, do not mention compensation, vouchers or refunds at all.
- Address the passenger by first name. 90-140 words in the body.
- Subject line under 80 characters, no exclamation marks.
- Do not include booking references, emails or phone numbers in the body."""


def notify_passengers(
    db: Session, disruption: Disruption, option: RecoveryOption
) -> tuple[list[Notification], str]:
    """Draft and log one notification per affected PNR.

    Returns `(notifications, source)` where source is "claude", "stub" or
    "mixed" — the UI shows which, so a demo never silently claims an LLM
    wrote something it didn't.
    """
    flight_ids = [int(i) for i in (disruption.flight_ids or [])]
    if disruption.flight_id and disruption.flight_id not in flight_ids:
        flight_ids.insert(0, disruption.flight_id)
    # Phase 1: only the primary flight's passengers are messaged. The other
    # departures in the event are retimed but not individually contacted.
    primary_ids = [disruption.flight_id] if disruption.flight_id else flight_ids[:1]

    check_permission(AGENT, "pss.get_affected_passengers")
    pnrs = db.scalars(
        select(Pnr)
        .options(selectinload(Pnr.passengers))
        .where(Pnr.flight_id.in_(primary_ids))
        .order_by(Pnr.record_locator)
    ).all()

    flight = disruption.flight
    delay_minutes = option.delay_minutes
    cancelled = option.kind == "CANCEL"

    created: list[Notification] = []
    sources: set[str] = set()
    drafted = 0

    for pnr in pnrs:
        if _recently_notified(db, disruption.id, pnr.record_locator):
            continue

        # Entitlement is decided by rule, before the model sees anything.
        check_permission(AGENT, "pss.issue_voucher")
        voucher = pss.issue_voucher(
            pnr.record_locator,
            delay_minutes=delay_minutes if not cancelled else 0,
        )

        for passenger in pnr.passengers:
            check_permission(AGENT, "notify.get_customer_profile")
            profile = notify.get_customer_profile(db, passenger.id)
            if profile is None:
                continue
            # Consent check (#11) — no consent, no message.
            if not profile["consent_email"]:
                continue

            facts = _facts(
                profile=profile,
                flight=flight,
                option=option,
                pnr=pnr,
                voucher=voucher,
                disruption=disruption,
            )

            if drafted < MAX_LLM_DRAFTS:
                draft, source = structured(
                    NotificationDraft,
                    system=SYSTEM,
                    prompt=_prompt(facts),
                    stub=lambda f=facts: _stub(f),
                    model=COMMS_MODEL,
                    max_tokens=1024,
                )
                drafted += 1
            else:
                draft, source = _stub(facts), "stub"
            sources.add(source)

            check_permission(AGENT, "notify.send_email")
            created.append(
                notify.send_email(
                    db,
                    disruption_id=disruption.id,
                    passenger_id=passenger.id,
                    recipient=profile["email"],
                    subject=draft.subject,
                    body=draft.body,
                    pnr_locator=pnr.record_locator,
                    offer=draft.offer or (voucher["text"] if voucher else None),
                )
            )

    # One ops-channel summary alongside the passenger messages.
    check_permission(AGENT, "notify.log_communication")
    notify.log_communication(
        db,
        disruption_id=disruption.id,
        channel="ops",
        recipient="ops-bridge",
        subject=f"Disruption {disruption.id} comms complete",
        body=(
            f"{len(created)} passenger notification(s) drafted and logged for "
            f"{option.label}. Delivery is disabled in Phase 1 "
            "(no Resend/Telegram key wired)."
        ),
    )

    source = (
        "mixed" if len(sources) > 1 else (sources.pop() if sources else "stub")
    )
    return created, source


# --------------------------------------------------------------------------
# Fact pack and templates
# --------------------------------------------------------------------------


def _facts(
    *,
    profile: dict[str, Any],
    flight: Any,
    option: RecoveryOption,
    pnr: Pnr,
    voucher: dict[str, Any] | None,
    disruption: Disruption,
) -> dict[str, Any]:
    cancelled = option.kind == "CANCEL"
    rebooked_to = None
    if pnr.rebooked_flight_id and pnr.rebooked_flight:
        rebooked_to = {
            "flight_no": pnr.rebooked_flight.flight_no,
            "etd": pnr.rebooked_flight.etd.isoformat(),
        }

    return {
        "first_name": profile["name"].split()[0],
        "tier": profile["tier"],
        "is_vip": profile["is_vip"],
        "language": profile["language"],
        "special_needs": profile.get("special_needs"),
        "flight_no": flight.flight_no if flight else "your flight",
        "route": f"{flight.origin} to {flight.destination}" if flight else "",
        "original_departure": flight.std.strftime("%H:%M UTC") if flight else "",
        "new_departure": (
            flight.etd.strftime("%H:%M UTC") if flight and not cancelled else None
        ),
        "cancelled": cancelled,
        "delay_minutes": option.delay_minutes,
        "cause": "dense fog and low visibility at the departure airport",
        "airport": disruption.airport,
        "action_taken": option.label,
        "rebooked_to": rebooked_to,
        "pnr_status": pnr.status,
        "entitlement": voucher["text"] if voucher else None,
    }


def _prompt(facts: dict[str, Any]) -> str:
    import json

    return (
        "Write one passenger notification from these facts:\n"
        f"{json.dumps(facts, indent=2, default=str)}\n\n"
        "Set `offer` to a one-line restatement of `entitlement`, or null if "
        "`entitlement` is null."
    )


def _stub(facts: dict[str, Any]) -> NotificationDraft:
    """Deterministic template used when there's no ANTHROPIC_API_KEY."""
    name = facts["first_name"]
    flight_no = facts["flight_no"]

    if facts["cancelled"]:
        subject = f"{flight_no} cancelled — your rebooking details"
        if facts["rebooked_to"]:
            middle = (
                f"We have moved you to {facts['rebooked_to']['flight_no']}, "
                f"departing {facts['rebooked_to']['etd'][11:16]} UTC. Your seat "
                "is confirmed and no action is needed from you."
            )
        else:
            middle = (
                "We are working to place you on the next available service and "
                "will confirm within the next two hours. If you prefer not to "
                "travel, a full refund is available."
            )
        body = (
            f"Dear {name},\n\n"
            f"{flight_no} ({facts['route']}) has been cancelled because of "
            f"{facts['cause']}. {middle}\n\n"
        )
    else:
        # ASCII only in the subject: it gets printed to the console during a
        # demo, and Windows terminals mangle an em-dash.
        subject = (
            f"{flight_no} delayed to {facts['new_departure']} - "
            "low visibility at departure"
        )
        body = (
            f"Dear {name},\n\n"
            f"{flight_no} ({facts['route']}) will now depart at "
            f"{facts['new_departure']}, about {facts['delay_minutes']} minutes "
            f"later than scheduled, because of {facts['cause']}. "
            "Your booking and seat are unchanged, and your onward connections "
            "are being protected where possible.\n\n"
            "Please stay near the departure gate and watch the screens — the "
            "visibility can improve quickly and we may board earlier than the "
            "revised time.\n\n"
        )

    if facts["entitlement"]:
        body += f"{facts['entitlement']}\n\n"
    body += "We are sorry for the disruption to your plans.\n\nAirline Operations"

    return NotificationDraft(
        subject=subject[:120],
        body=body,
        offer=(facts["entitlement"] or None),
    )


def _recently_notified(db: Session, disruption_id: int, locator: str) -> bool:
    cutoff = utcnow() - timedelta(minutes=DEDUPE_MINUTES)
    existing = db.scalar(
        select(Notification.id).where(
            Notification.disruption_id == disruption_id,
            Notification.pnr_locator == locator,
            Notification.created_at >= cutoff,
        )
    )
    return existing is not None
