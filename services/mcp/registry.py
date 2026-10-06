"""Tool registry and the agent-to-tool permission matrix.

Phase 0 artefact. In Phase 3 the six modules in this package become real
FastMCP servers behind a gateway, and *this table* becomes the gateway's
allowlist. Until then the same table is enforced in-process by
`services.mcp.registry.check_permission`, so an agent reaching for a tool it
shouldn't have fails here rather than silently working now and breaking later.

Tool type legend (architecture doc section 7):
  R = read, A = write auto-allowed, H = write requiring human approval.
"""

from __future__ import annotations

from dataclasses import dataclass

Domain = str
AgentName = str


@dataclass(frozen=True)
class ToolSpec:
    domain: Domain
    name: str
    type: str  # "R" | "A" | "H"

    @property
    def qualified_name(self) -> str:
        return f"{self.domain}.{self.name}"


def _specs(domain: str, **tools: str) -> dict[str, ToolSpec]:
    return {
        f"{domain}.{name}": ToolSpec(domain, name, kind)
        for name, kind in tools.items()
    }


#: Every tool the Phase 1 modules expose, tagged as the architecture doc tags it.
TOOLS: dict[str, ToolSpec] = {
    **_specs(
        "weather",
        get_metar="R",
        get_taf="R",
        get_forecast="R",
        assess_weather_risk="R",
    ),
    **_specs(
        "flightops",
        get_schedule="R",
        get_fleet_status="R",
        get_maintenance_status="R",
        get_aircraft_availability="R",
        get_downstream_rotation="R",
        retime_flight="H",
        swap_aircraft="H",
        cancel_flight="H",
    ),
    **_specs(
        "aodb",
        get_airport_info="R",
        get_gate_assignments="R",
        get_airport_congestion="R",
        reassign_gate="A",
    ),
    **_specs(
        "pss",
        get_affected_passengers="R",
        get_connections_at_risk="R",
        get_inventory="R",
        rebook_passengers="H",
        issue_voucher="A",
    ),
    **_specs(
        "crew",
        get_crew_roster="R",
        check_duty_limits="R",
        find_standby_crew="R",
        assign_crew="H",
    ),
    **_specs(
        "notify",
        get_customer_profile="R",
        send_email="A",
        send_telegram="A",
        log_communication="A",
    ),
}


#: Architecture doc section 10. Values are the tool types each agent may use
#: per domain; a domain absent from an agent's entry is denied outright.
PERMISSIONS: dict[AgentName, dict[Domain, set[str]]] = {
    "detection": {
        "weather": {"R"},
        "flightops": {"R"},
        "aodb": {"R"},
        "crew": {"R"},
    },
    "impact": {
        "weather": {"R"},
        "flightops": {"R"},
        "aodb": {"R"},
        "pss": {"R"},
        "crew": {"R"},
    },
    "recovery": {
        "weather": {"R"},
        "flightops": {"R"},
        "aodb": {"R"},
        "pss": {"R"},
        "crew": {"R"},
    },
    "coordination": {
        "flightops": {"R", "H"},
        "aodb": {"R", "A"},
        "pss": {"R", "H"},
        "crew": {"R", "H", "A"},
    },
    "passenger": {
        "flightops": {"R"},
        "pss": {"R", "A", "H"},
        "notify": {"R", "A"},
    },
}


class PermissionDenied(PermissionError):
    """An agent called a tool outside its allowlist."""


class UnknownTool(KeyError):
    """A tool name that isn't in the registry."""


def check_permission(agent: AgentName, qualified_tool: str) -> ToolSpec:
    """Raise unless `agent` is allowed to call `qualified_tool`."""
    spec = TOOLS.get(qualified_tool)
    if spec is None:
        raise UnknownTool(f"unknown tool {qualified_tool!r}")

    allowed = PERMISSIONS.get(agent)
    if allowed is None:
        raise PermissionDenied(f"unknown agent {agent!r}")

    types = allowed.get(spec.domain)
    if not types or spec.type not in types:
        raise PermissionDenied(
            f"agent {agent!r} may not call {qualified_tool!r} (type {spec.type})"
        )
    return spec


def tools_for(agent: AgentName) -> list[str]:
    """Every tool `agent` is allowed to call — used by the Phase 4 matrix tests."""
    allowed = PERMISSIONS.get(agent, {})
    return sorted(
        name
        for name, spec in TOOLS.items()
        if spec.type in allowed.get(spec.domain, set())
    )
