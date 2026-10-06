"""Phase 1 data model.

This is the trimmed version of architecture doc section 11: `airports,
aircraft, flights, crew, pnrs, passengers, disruptions, impacts,
recovery_options, notifications`. Deferred to Phase 2 (per the roadmap):
`gates`, `crew_assignments`, `segments`, `approvals`, `actions`, `audit_log`.

Two Phase 1 shortcuts are marked inline so the Phase 2 work is obvious:
  * crew rostering lives on `crew.assigned_flight_id` instead of a
    `crew_assignments` join table;
  * executed actions are appended to `disruptions.execution_log` (JSON)
    instead of an `actions` table.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator):
    """A timestamp that is always tz-aware UTC on the way out.

    Postgres round-trips `timestamptz` fine, but SQLite stores no offset and
    hands back naive datetimes — which blows up the first time you subtract
    one from `datetime.now(timezone.utc)`. Normalising in the type keeps every
    call site (agents, tools, schemas) free of timezone defensiveness, and
    keeps the local-SQLite fallback behaving like Neon.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------


class Airport(Base):
    """Loaded once from the OurAirports CSV (keyless) by db/seed.py."""

    __tablename__ = "airports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    icao: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    iata: Mapped[str | None] = mapped_column(String(4), index=True)
    name: Mapped[str] = mapped_column(String(160))
    city: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(4))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    # Phase 1 congestion model: declared movements/hour capacity, seeded.
    hourly_capacity: Mapped[int] = mapped_column(Integer, default=40)


class Aircraft(Base):
    __tablename__ = "aircraft"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    registration: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    type_code: Mapped[str] = mapped_column(String(8))
    seats: Mapped[int] = mapped_column(Integer)
    base_icao: Mapped[str] = mapped_column(String(8))
    location_icao: Mapped[str] = mapped_column(String(8))
    # Synthetic in every phase — no free API exposes MEL/maintenance status.
    status: Mapped[str] = mapped_column(String(20), default="SERVICEABLE")
    mel_note: Mapped[str | None] = mapped_column(Text)

    flights: Mapped[list["Flight"]] = relationship(
        back_populates="aircraft", foreign_keys="Flight.aircraft_id"
    )

    @property
    def is_available(self) -> bool:
        return self.status == "SERVICEABLE"


class Flight(Base):
    __tablename__ = "flights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    flight_no: Mapped[str] = mapped_column(String(10), index=True)
    origin: Mapped[str] = mapped_column(String(8), index=True)
    destination: Mapped[str] = mapped_column(String(8), index=True)
    std: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    sta: Mapped[datetime] = mapped_column(UtcDateTime)
    etd: Mapped[datetime] = mapped_column(UtcDateTime)
    eta: Mapped[datetime] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(String(20), default="SCHEDULED", index=True)
    delay_minutes: Mapped[int] = mapped_column(Integer, default=0)
    # Phase 1: a plain string, not a FK to a `gates` table (Phase 2).
    gate: Mapped[str | None] = mapped_column(String(8))
    # The tail currently operating the leg. A `swap_aircraft` changes this.
    aircraft_id: Mapped[int | None] = mapped_column(ForeignKey("aircraft.id"))
    # The tail the schedule was *planned* with. Never changed by recovery, so
    # the UI can show "swapped from X to Y" and `/demo/reset` can put the
    # fleet back without reseeding.
    scheduled_aircraft_id: Mapped[int | None] = mapped_column(
        ForeignKey("aircraft.id")
    )

    aircraft: Mapped[Aircraft | None] = relationship(
        back_populates="flights", foreign_keys=[aircraft_id]
    )
    scheduled_aircraft: Mapped[Aircraft | None] = relationship(
        foreign_keys=[scheduled_aircraft_id]
    )
    pnrs: Mapped[list["Pnr"]] = relationship(
        back_populates="flight", foreign_keys="Pnr.flight_id"
    )
    crew: Mapped[list["Crew"]] = relationship(
        back_populates="assigned_flight", foreign_keys="Crew.assigned_flight_id"
    )


class Crew(Base):
    __tablename__ = "crew"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crew_code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(4))  # CP | FO | CC
    base_icao: Mapped[str] = mapped_column(String(8))
    qualified_types: Mapped[str] = mapped_column(String(64))  # comma separated
    on_standby: Mapped[bool] = mapped_column(Boolean, default=False)
    # FTL inputs (synthetic, seeded). Phase 1 checks these in Python.
    duty_start: Mapped[datetime | None] = mapped_column(UtcDateTime)
    duty_minutes_so_far: Mapped[int] = mapped_column(Integer, default=0)
    max_duty_minutes: Mapped[int] = mapped_column(Integer, default=780)  # 13h
    rest_hours_before_duty: Mapped[float] = mapped_column(Float, default=12.0)
    # Phase 1 shortcut: replaced by `crew_assignments` in Phase 2.
    assigned_flight_id: Mapped[int | None] = mapped_column(ForeignKey("flights.id"))
    # The roster as published, so `/demo/reset` can undo a crew call-out or a
    # cancellation's crew release. Same scheduled-vs-operating split as
    # `Flight.scheduled_aircraft_id`.
    scheduled_flight_id: Mapped[int | None] = mapped_column(
        ForeignKey("flights.id")
    )

    assigned_flight: Mapped[Flight | None] = relationship(
        back_populates="crew", foreign_keys=[assigned_flight_id]
    )
    scheduled_flight: Mapped[Flight | None] = relationship(
        foreign_keys=[scheduled_flight_id]
    )

    def qualified_for(self, type_code: str) -> bool:
        return type_code in {t.strip() for t in self.qualified_types.split(",")}


# --------------------------------------------------------------------------
# Passenger side (synthetic in every phase — no free PSS API exists)
# --------------------------------------------------------------------------


class Pnr(Base):
    __tablename__ = "pnrs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_locator: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    flight_id: Mapped[int] = mapped_column(ForeignKey("flights.id"), index=True)
    tier: Mapped[str] = mapped_column(String(10), default="BASE")
    fare_class: Mapped[str] = mapped_column(String(2), default="Y")
    pax_count: Mapped[int] = mapped_column(Integer, default=1)
    special_needs: Mapped[str | None] = mapped_column(String(40))
    # Onward connection, if any — drives missed-connection counting.
    connection_flight_id: Mapped[int | None] = mapped_column(ForeignKey("flights.id"))
    connection_mct_minutes: Mapped[int] = mapped_column(Integer, default=60)
    status: Mapped[str] = mapped_column(String(16), default="BOOKED")
    rebooked_flight_id: Mapped[int | None] = mapped_column(ForeignKey("flights.id"))

    flight: Mapped[Flight] = relationship(
        back_populates="pnrs", foreign_keys=[flight_id]
    )
    connection_flight: Mapped[Flight | None] = relationship(
        foreign_keys=[connection_flight_id]
    )
    rebooked_flight: Mapped[Flight | None] = relationship(
        foreign_keys=[rebooked_flight_id]
    )
    passengers: Mapped[list["Passenger"]] = relationship(
        back_populates="pnr", cascade="all, delete-orphan"
    )


class Passenger(Base):
    __tablename__ = "passengers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pnr_id: Mapped[int] = mapped_column(ForeignKey("pnrs.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(32))
    language: Mapped[str] = mapped_column(String(8), default="en")
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_email: Mapped[bool] = mapped_column(Boolean, default=True)

    pnr: Mapped[Pnr] = relationship(back_populates="passengers")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


class Disruption(Base):
    __tablename__ = "disruptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(12), default="MEDIUM")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    eta_minutes: Mapped[int] = mapped_column(Integer, default=0)
    airport: Mapped[str | None] = mapped_column(String(8), index=True)

    # Primary flight plus the full affected set (DisruptionEvent.flights[]).
    flight_id: Mapped[int | None] = mapped_column(ForeignKey("flights.id"))
    flight_ids: Mapped[list] = mapped_column(JSON, default=list)

    state: Mapped[str] = mapped_column(String(24), default="DETECTED", index=True)
    evidence: Mapped[list] = mapped_column(JSON, default=list)

    # Phase 1 HITL: a boolean plus a name. Phase 3 replaces this with an
    # HMAC-signed, TTL-bound approval token.
    pending: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_option_id: Mapped[int | None] = mapped_column(
        ForeignKey("recovery_options.id")
    )
    decision_comment: Mapped[str | None] = mapped_column(Text)

    # Phase 1 shortcut for the deferred `actions` table.
    execution_log: Mapped[list] = mapped_column(JSON, default=list)
    # Human-readable state trail; the `audit_log` table lands in Phase 2.
    timeline: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text)

    dedupe_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow
    )

    flight: Mapped[Flight | None] = relationship(foreign_keys=[flight_id])
    impact: Mapped["Impact | None"] = relationship(
        back_populates="disruption", uselist=False, cascade="all, delete-orphan"
    )
    options: Mapped[list["RecoveryOption"]] = relationship(
        back_populates="disruption",
        cascade="all, delete-orphan",
        foreign_keys="RecoveryOption.disruption_id",
    )
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="disruption", cascade="all, delete-orphan"
    )


class Impact(Base):
    __tablename__ = "impacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    disruption_id: Mapped[int] = mapped_column(
        ForeignKey("disruptions.id"), unique=True, index=True
    )
    pax_count: Mapped[int] = mapped_column(Integer, default=0)
    vip_count: Mapped[int] = mapped_column(Integer, default=0)
    missed_connections: Mapped[int] = mapped_column(Integer, default=0)
    knock_on_flights: Mapped[int] = mapped_column(Integer, default=0)
    crew_legal_breach: Mapped[bool] = mapped_column(Boolean, default=False)
    gate_conflicts: Mapped[int] = mapped_column(Integer, default=0)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow
    )

    disruption: Mapped[Disruption] = relationship(back_populates="impact")


class RecoveryOption(Base):
    __tablename__ = "recovery_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    disruption_id: Mapped[int] = mapped_column(
        ForeignKey("disruptions.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(24))  # RETIME | SWAP_AIRCRAFT | CANCEL
    label: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(Text)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    delay_minutes: Mapped[int] = mapped_column(Integer, default=0)
    pax_impacted: Mapped[int] = mapped_column(Integer, default=0)
    feasibility: Mapped[float] = mapped_column(Float, default=0.5)
    regulatory_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    crew_legal: Mapped[bool] = mapped_column(Boolean, default=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str | None] = mapped_column(Text)
    recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow
    )

    disruption: Mapped[Disruption] = relationship(
        back_populates="options", foreign_keys=[disruption_id]
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    disruption_id: Mapped[int] = mapped_column(
        ForeignKey("disruptions.id"), index=True
    )
    passenger_id: Mapped[int | None] = mapped_column(ForeignKey("passengers.id"))
    pnr_locator: Mapped[str | None] = mapped_column(String(8))
    recipient: Mapped[str | None] = mapped_column(String(160))
    channel: Mapped[str] = mapped_column(String(16), default="email")
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    offer: Mapped[str | None] = mapped_column(String(120))
    # Phase 1: always 'logged'. Phase 4 flips this to 'sent' via Resend/Telegram.
    status: Mapped[str] = mapped_column(String(12), default="logged")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow
    )

    disruption: Mapped[Disruption] = relationship(back_populates="notifications")
    passenger: Mapped[Passenger | None] = relationship()
