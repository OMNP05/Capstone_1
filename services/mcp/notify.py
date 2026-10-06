"""notify domain — CRM lookup and message delivery.

Phase 1 sends nothing. `send_email` / `send_telegram` write a row to
`notifications` with `status='logged'` and print to the console. The function
signatures are what the Resend / Telegram-backed versions will have, so
Phase 4 swaps the body and nothing above this module changes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from services.core.config import settings
from services.core.models import Notification, Passenger


def get_customer_profile(db: Session, passenger_id: int) -> dict[str, Any] | None:
    """`R` tool: name, language, channel preference, tier and consent."""
    passenger = db.get(Passenger, passenger_id)
    if passenger is None:
        return None
    pnr = passenger.pnr
    return {
        "passenger_id": passenger.id,
        "name": passenger.name,
        "email": passenger.email,
        "phone": passenger.phone,
        "language": passenger.language,
        "is_vip": passenger.is_vip,
        "consent_email": passenger.consent_email,
        "tier": pnr.tier if pnr else "BASE",
        "record_locator": pnr.record_locator if pnr else None,
        "special_needs": pnr.special_needs if pnr else None,
        "preferred_channel": "email",
    }


def send_email(
    db: Session,
    *,
    disruption_id: int,
    passenger_id: int | None,
    recipient: str,
    subject: str,
    body: str,
    pnr_locator: str | None = None,
    offer: str | None = None,
) -> Notification:
    """`A` tool: deliver by email.

    Phase 1: logged only. `RESEND_API_KEY` is read here purely so the log line
    tells you *why* nothing was sent.
    """
    return _log(
        db,
        disruption_id=disruption_id,
        passenger_id=passenger_id,
        recipient=recipient,
        channel="email",
        subject=subject,
        body=body,
        pnr_locator=pnr_locator,
        offer=offer,
        reason=(
            "RESEND_API_KEY set but real send lands in Phase 4"
            if settings.resend_api_key
            else "no RESEND_API_KEY — logged, not sent"
        ),
    )


def send_telegram(
    db: Session,
    *,
    disruption_id: int,
    passenger_id: int | None,
    recipient: str,
    body: str,
    pnr_locator: str | None = None,
    offer: str | None = None,
) -> Notification:
    """`A` tool: deliver by Telegram. Phase 1: logged only."""
    return _log(
        db,
        disruption_id=disruption_id,
        passenger_id=passenger_id,
        recipient=recipient,
        channel="telegram",
        subject=None,
        body=body,
        pnr_locator=pnr_locator,
        offer=offer,
        reason=(
            "TELEGRAM_BOT_TOKEN set but real send lands in Phase 4"
            if settings.telegram_bot_token
            else "no TELEGRAM_BOT_TOKEN — logged, not sent"
        ),
    )


def log_communication(
    db: Session,
    *,
    disruption_id: int,
    channel: str,
    body: str,
    recipient: str | None = None,
    subject: str | None = None,
) -> Notification:
    """`A` tool: a CRM log entry with no delivery attempt (e.g. ops broadcast)."""
    return _log(
        db,
        disruption_id=disruption_id,
        passenger_id=None,
        recipient=recipient,
        channel=channel,
        subject=subject,
        body=body,
        pnr_locator=None,
        offer=None,
        reason="crm log entry",
    )


def _log(
    db: Session,
    *,
    disruption_id: int,
    passenger_id: int | None,
    recipient: str | None,
    channel: str,
    subject: str | None,
    body: str,
    pnr_locator: str | None,
    offer: str | None,
    reason: str,
) -> Notification:
    notification = Notification(
        disruption_id=disruption_id,
        passenger_id=passenger_id,
        pnr_locator=pnr_locator,
        recipient=recipient,
        channel=channel,
        subject=subject,
        body=body,
        offer=offer,
        status="logged",
    )
    db.add(notification)
    db.flush()
    print(
        f"[notify] {channel} -> {recipient or 'ops'} "
        f"({pnr_locator or 'n/a'}): {subject or body[:60]!r} [{reason}]"
    )
    return notification
