"""The disruption state machine, as `if` guards.

Architecture doc section 4, trimmed to the Phase 1 happy path plus the two
error exits we actually need (`NEEDS_HUMAN`, `CLOSED`). Phase 3 replaces this
with a LangGraph graph over the *same* states — so keep the names stable.
"""

from __future__ import annotations

from services.core.models import Disruption, utcnow

DETECTED = "DETECTED"
ASSESSED = "ASSESSED"
PLANNED = "PLANNED"
PENDING_APPROVAL = "PENDING_APPROVAL"
APPROVED = "APPROVED"
EXECUTING = "EXECUTING"
COMPLETED = "COMPLETED"
NEEDS_HUMAN = "NEEDS_HUMAN"
CLOSED = "CLOSED"

TERMINAL = {COMPLETED, CLOSED}

#: Allowed transitions. Anything not listed here raises.
ALLOWED: dict[str, set[str]] = {
    DETECTED: {ASSESSED, NEEDS_HUMAN, CLOSED},
    ASSESSED: {PLANNED, NEEDS_HUMAN, CLOSED},
    # PLANNED -> ASSESSED is the replan rewind: a rejected plan goes back to
    # the Recovery agent with the same impact assessment.
    PLANNED: {PENDING_APPROVAL, ASSESSED, NEEDS_HUMAN, CLOSED},
    PENDING_APPROVAL: {APPROVED, PLANNED, CLOSED},
    APPROVED: {EXECUTING, NEEDS_HUMAN},
    EXECUTING: {COMPLETED, NEEDS_HUMAN},
    COMPLETED: set(),
    NEEDS_HUMAN: {PLANNED, CLOSED},
    CLOSED: set(),
}


class TransitionError(RuntimeError):
    """Raised when a caller tries an illegal state transition."""


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, set())


def transition(disruption: Disruption, target: str, note: str | None = None) -> None:
    """Move a disruption to `target`, appending to its timeline.

    This is check #5 from architecture doc section 10 ("cannot EXECUTE before
    APPROVED"), enforced in the only place that writes `disruptions.state`.
    """
    current = disruption.state
    if current == target:
        return
    if not can_transition(current, target):
        raise TransitionError(
            f"disruption {disruption.id}: illegal transition {current} -> {target}"
        )

    disruption.state = target
    disruption.updated_at = utcnow()
    entry = {"state": target, "at": utcnow().isoformat()}
    if note:
        entry["note"] = note
    # Reassign rather than append: JSON columns need a new object to be dirty.
    disruption.timeline = [*(disruption.timeline or []), entry]
