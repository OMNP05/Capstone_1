"""Claude access for the agent steps.

One function, `structured`, implements check #6 from architecture doc section
10: Claude's output must parse into the Pydantic schema; on failure retry once
with the validation error attached; if it still fails, raise so the caller can
mark the disruption `NEEDS_HUMAN`.

When `ANTHROPIC_API_KEY` is unset, `structured` calls the caller's `stub`
instead. The signature is identical either way, so swapping a key in is the
only change needed to go live.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from services.core.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

#: Reasoning steps (Detection / Impact / Recovery / Coordination).
AGENT_MODEL = settings.agent_model
#: Comms drafts — short, high volume, latency-sensitive.
COMMS_MODEL = settings.comms_model

_client = None


class AgentOutputError(RuntimeError):
    """Claude produced output that would not validate, twice."""


def llm_mode() -> str:
    return "claude" if settings.llm_enabled else "stub"


def _get_client():
    global _client
    if _client is None:
        import anthropic  # imported lazily: optional when running on stubs

        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def structured(
    schema: type[T],
    *,
    system: str,
    prompt: str,
    stub: Callable[[], T],
    model: str | None = None,
    max_tokens: int = 4096,
) -> tuple[T, str]:
    """Return `(validated_output, source)` where source is "claude" or "stub".

    `stub` is the deterministic fallback for this specific agent step. It runs
    when there's no API key — not when Claude fails, because a silent
    downgrade mid-run would hide a real problem.
    """
    if not settings.llm_enabled:
        return stub(), "stub"

    model = model or AGENT_MODEL
    messages: list[dict] = [{"role": "user", "content": prompt}]

    for attempt in (1, 2):
        try:
            response = _get_client().messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_format=schema,
            )
        except Exception as exc:
            # Transport / API failure: the SDK already retried 429s and 5xxs.
            raise AgentOutputError(f"Claude call failed: {type(exc).__name__}: {exc}") from exc

        parsed = response.parsed_output
        if isinstance(parsed, schema):
            return parsed, "claude"

        # output_format normally guarantees a valid instance; if the SDK hands
        # back something else, re-validate so we get a usable error message.
        try:
            return schema.model_validate(parsed), "claude"
        except ValidationError as exc:
            if attempt == 2:
                raise AgentOutputError(
                    f"{schema.__name__} validation failed twice: {exc}"
                ) from exc
            log.warning("%s validation failed, retrying once", schema.__name__)
            messages += [
                {"role": "assistant", "content": str(parsed)},
                {
                    "role": "user",
                    "content": (
                        "That output failed schema validation:\n"
                        f"{exc}\n\nReturn corrected JSON matching the schema exactly."
                    ),
                },
            ]

    raise AgentOutputError("unreachable")
