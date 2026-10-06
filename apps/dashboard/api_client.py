"""Thin HTTP client for the FastAPI backend.

Deliberately dumb: one function per route, no caching, no retries beyond
httpx's defaults. Streamlit reruns the whole script on every interaction, so
"refresh" is just another rerun — caching here would only fight that.

These calls run in the Streamlit *server* process, not the browser, so the
backend's CORS policy is irrelevant to them.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000/api/v1")
TIMEOUT = 30.0


class ApiError(RuntimeError):
    """A non-2xx response, carrying the backend's `detail` message."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _request(method: str, base_url: str, path: str, **kwargs: Any) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise ApiError(0, f"cannot reach the API at {url} ({type(exc).__name__})") from exc

    if response.status_code >= 400:
        detail = response.text
        try:
            detail = response.json().get("detail", detail)
        except Exception:  # noqa: BLE001 — non-JSON error body
            pass
        raise ApiError(response.status_code, str(detail))

    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def health(base_url: str) -> dict[str, Any]:
    return _request("GET", base_url, "/health")


def list_disruptions(base_url: str) -> list[dict[str, Any]]:
    return _request("GET", base_url, "/disruptions")


def get_disruption(base_url: str, disruption_id: int) -> dict[str, Any]:
    return _request("GET", base_url, f"/disruptions/{disruption_id}")


def inject(base_url: str, scenario: str = "fog_delhi") -> dict[str, Any]:
    return _request("POST", base_url, "/demo/inject", json={"scenario": scenario})


def approve(
    base_url: str,
    disruption_id: int,
    *,
    option_id: int | None,
    comment: str | None,
    approved_by: str,
) -> dict[str, Any]:
    return _request(
        "POST",
        base_url,
        f"/disruptions/{disruption_id}/approve",
        json={
            "option_id": option_id,
            "comment": comment or None,
            "approved_by": approved_by,
        },
    )


def reject(
    base_url: str,
    disruption_id: int,
    *,
    reason: str | None,
    request_replan: bool,
    rejected_by: str,
) -> dict[str, Any]:
    return _request(
        "POST",
        base_url,
        f"/disruptions/{disruption_id}/reject",
        json={
            "reason": reason or None,
            "request_replan": request_replan,
            "rejected_by": rejected_by,
        },
    )


def reset_demo(base_url: str) -> None:
    _request("POST", base_url, "/demo/reset")
