"""Disruption Orchestrator — Streamlit control room (Phase 1).

Run it (from the repo root, with the API already up):

    streamlit run apps/dashboard/app.py

One page, everything on it: the disruption table, the impact numbers, the
recovery options, the one Approve/Reject gate, and the notifications that got
drafted. Refresh is **manual** — one button — exactly as Phase 1 specifies.
Phase 2 swaps that button for `streamlit-autorefresh`; nothing else here has
to change, because both are polling the same REST endpoints.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

# Allow `streamlit run apps/dashboard/app.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_client as api  # noqa: E402

st.set_page_config(
    page_title="Disruption Orchestrator",
    page_icon="🛫",
    layout="wide",
)

STATE_COLOURS = {
    "DETECTED": "🔵",
    "ASSESSED": "🔵",
    "PLANNED": "🟣",
    "PENDING_APPROVAL": "🟠",
    "APPROVED": "🟢",
    "EXECUTING": "🟢",
    "COMPLETED": "✅",
    "NEEDS_HUMAN": "🔴",
    "CLOSED": "⚪",
}
SEVERITY_COLOURS = {
    "LOW": "🟢",
    "MEDIUM": "🟡",
    "HIGH": "🟠",
    "CRITICAL": "🔴",
}
#: States the orchestrator is still working through — the hint to press Refresh.
IN_FLIGHT = {"DETECTED", "ASSESSED", "PLANNED", "APPROVED", "EXECUTING"}

KIND_LABELS = {
    "RETIME": "⏱️ Retime",
    "SWAP_AIRCRAFT": "✈️ Swap aircraft",
    "CANCEL": "⛔ Cancel",
}


# --------------------------------------------------------------------------
# Sidebar: connection, health, demo controls
# --------------------------------------------------------------------------


def sidebar() -> str:
    st.sidebar.title("🛫 Orchestrator")
    st.sidebar.caption("Phase 1 — Fog at DEL, one straight line")

    base_url = st.sidebar.text_input(
        "API base URL",
        value=st.session_state.get("base_url", api.DEFAULT_BASE_URL),
        help="The FastAPI backend. Start it with `uvicorn services.api.main:app --reload`.",
    )
    st.session_state["base_url"] = base_url

    st.sidebar.divider()

    # --- Health ---
    try:
        info = api.health(base_url)
    except api.ApiError as exc:
        st.sidebar.error(f"Backend unreachable\n\n{exc.detail}")
        st.sidebar.info(
            "Start the API first:\n\n"
            "```\nuvicorn services.api.main:app --reload\n```"
        )
        st.stop()

    st.sidebar.subheader("System")
    st.sidebar.write(
        f"**Database** · {info['database_kind']} ({info['database']})"
    )
    llm = info["llm_mode"]
    if llm == "claude":
        st.sidebar.write("**Agents** · Claude (live)")
    else:
        st.sidebar.write("**Agents** · rule-based stubs")
        st.sidebar.caption(
            "No `ANTHROPIC_API_KEY`, so Recovery and Comms use deterministic "
            "templates. The loop still runs end to end."
        )
    st.sidebar.write(f"**Weather feed** · {info['weather_feed']}")

    seeded = info["seeded"]
    if seeded["flights"] == 0:
        st.sidebar.error("Database is not seeded.\n\nRun `python -m db.seed`.")
    else:
        st.sidebar.caption(
            f"{seeded['flights']} flights · {seeded['aircraft']} tails · "
            f"{seeded['crew']} crew · {seeded['pnrs']} PNRs · "
            f"{seeded['airports']} airports"
        )

    st.sidebar.divider()
    st.sidebar.subheader("Demo controls")

    if st.sidebar.button(
        "🌫️ Inject Fog at DEL", type="primary", width="stretch"
    ):
        try:
            created = api.inject(base_url)
            st.session_state["selected_id"] = created["id"]
            st.session_state["flash"] = (
                "success",
                f"Disruption #{created['id']} detected. The orchestrator is "
                "assessing impact and planning — press Refresh to follow it.",
            )
        except api.ApiError as exc:
            st.session_state["flash"] = ("error", exc.detail)
        st.rerun()

    # Phase 1 is manual refresh on purpose. Streamlit reruns the whole script
    # on any interaction, so this button *is* the poll.
    if st.sidebar.button("🔄 Refresh", width="stretch"):
        st.rerun()

    with st.sidebar.expander("Reset demo"):
        st.caption(
            "Deletes every disruption and undoes the scenario's changes to the "
            "schedule. The seeded fleet, roster and PNRs are kept."
        )
        if st.button("Reset", width="stretch"):
            try:
                api.reset_demo(base_url)
                st.session_state.pop("selected_id", None)
                st.session_state["flash"] = ("success", "Demo reset.")
            except api.ApiError as exc:
                st.session_state["flash"] = ("error", exc.detail)
            st.rerun()

    st.sidebar.divider()
    st.sidebar.caption(
        "Phase 1 has no login. Every action is recorded against the name below."
    )
    st.session_state["actor"] = st.sidebar.text_input(
        "Acting as", value=st.session_state.get("actor", "demo-user")
    )

    return base_url


# --------------------------------------------------------------------------
# Disruption table
# --------------------------------------------------------------------------


def disruption_table(rows: list[dict]) -> None:
    frame = pd.DataFrame(
        [
            {
                "ID": r["id"],
                "State": f"{STATE_COLOURS.get(r['state'], '⚪')} {r['state']}",
                "Severity": f"{SEVERITY_COLOURS.get(r['severity'], '⚪')} {r['severity']}",
                "Type": r["type"],
                "Airport": r["airport"],
                "Flight": r["flight_no"] or "—",
                "Pax": r["pax_count"] if r["pax_count"] is not None else "—",
                "Priority": (
                    f"{r['priority_score']:.2f}"
                    if r["priority_score"] is not None
                    else "—"
                ),
                "Options": r["option_count"],
                "Notifs": r["notification_count"],
                "Approved by": r["approved_by"] or "—",
                "Opened": _ago(r["created_at"]),
            }
            for r in rows
        ]
    )
    st.dataframe(frame, width="stretch", hide_index=True)


# --------------------------------------------------------------------------
# Detail panes
# --------------------------------------------------------------------------


def impact_pane(detail: dict) -> None:
    impact = detail.get("impact")
    if not impact:
        st.info(
            "Impact assessment has not run yet. Press **🔄 Refresh** in the "
            "sidebar."
        )
        return

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Passengers", impact["pax_count"])
    c2.metric("VIPs", impact["vip_count"])
    c3.metric("Missed connections", impact["missed_connections"])
    c4.metric("Knock-on flights", impact["knock_on_flights"])
    c5.metric("Stand conflicts", impact["gate_conflicts"])
    c6.metric("Priority", f"{impact['priority_score']:.3f}")

    if impact["crew_legal_breach"]:
        st.warning(
            "⚠️ **Crew FTL breach** at the expected delay — any option that "
            "still operates the flight needs standby crew, or it is discarded "
            "as illegal."
        )

    narrative = (detail.get("note") or "").strip()
    if narrative:
        st.caption(narrative)

    data = impact.get("detail") or {}

    with st.expander("How the priority score was built"):
        terms = data.get("priority_terms") or {}
        if terms:
            st.caption(
                "Architecture doc section 8: `0.35·pax + 0.20·connections + "
                "0.15·vip_ratio + 0.15·knock_on + 0.10·crew_breach + "
                "0.05·urgency`, each term normalised to 0-1."
            )
            st.dataframe(
                pd.DataFrame(
                    [{"Term": k, "Normalised value": v} for k, v in terms.items()]
                ),
                width="stretch",
                hide_index=True,
            )

    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("**Connections at risk**")
        at_risk = data.get("connections_at_risk") or []
        if at_risk:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "PNR": c["record_locator"],
                            "Pax": c["pax_count"],
                            "Tier": c["tier"],
                            "Inbound": c["inbound"],
                            "Onward": c["onward"],
                            "Available": f"{c['available_minutes']} min",
                            "Required MCT": f"{c['required_mct_minutes']} min",
                        }
                        for c in at_risk
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("No connection breaks at the assumed delay.")

    with col_b:
        st.markdown("**Crew duty checks**")
        checks = data.get("crew_checks") or []
        if checks:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Flight": c["flight_no"],
                            "Breach": "yes" if c["breach"] else "no",
                            "Max legal delay": (
                                f"{c['max_legal_delay_minutes']} min"
                                if c["max_legal_delay_minutes"] is not None
                                else "—"
                            ),
                            "Reason": "; ".join(c["reasons"]) or "within limits",
                        }
                        for c in checks
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("No crew rostered on the affected flights.")


def options_pane(base_url: str, detail: dict) -> None:
    options = detail.get("options") or []
    if not options:
        st.info(
            "Recovery options have not been generated yet. Press "
            "**🔄 Refresh** in the sidebar."
        )
        return

    st.caption(
        "Ranked by the section 8 option score: "
        "`0.30·(1−cost) + 0.30·(1−pax) + 0.20·feasibility + "
        "0.10·(1−delay) + 0.10·crew_legal`. Options that fail a hard "
        "constraint (crew legality, curfew, a tail that isn't free) are "
        "discarded before ranking and never appear here."
    )

    for option in options:
        label = KIND_LABELS.get(option["kind"], option["kind"])
        header = f"{label} — {option['label']}"
        if option["recommended"]:
            header += "   ⭐ recommended"

        with st.container(border=True):
            st.markdown(f"#### {header}")
            st.write(option["summary"])

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Score", f"{option['score']:.3f}")
            m2.metric("Cost", f"${option['cost_usd']:,.0f}")
            m3.metric("Delay", f"{option['delay_minutes']} min")
            m4.metric("Pax impacted", option["pax_impacted"])
            m5.metric("Feasibility", f"{option['feasibility']:.0%}")

            flags = []
            flags.append(
                "✅ crew legal" if option["crew_legal"] else "⚠️ crew not legal"
            )
            flags.append(
                "✅ regulatory ok"
                if option["regulatory_ok"]
                else "⛔ regulatory check failed"
            )
            st.caption(" · ".join(flags))

            if option.get("rationale"):
                with st.expander("Rationale and guardrail notes"):
                    st.write(option["rationale"])

            with st.expander(f"Actions ({len(option['actions'])})"):
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Tool": a.get("tool"),
                                "Flight": a.get("flight_id"),
                                "Arguments": str(a.get("args") or {}),
                            }
                            for a in option["actions"]
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                )


def approval_pane(base_url: str, detail: dict) -> None:
    """The one HITL gate."""
    options = detail.get("options") or []
    approvable = [o for o in options if o["regulatory_ok"]]
    if not approvable:
        st.error("No option passed its regulatory check — nothing can be approved.")
        return

    default_index = next(
        (i for i, o in enumerate(approvable) if o["recommended"]), 0
    )
    chosen = st.radio(
        "Option to execute",
        options=approvable,
        index=default_index,
        format_func=lambda o: (
            f"{KIND_LABELS.get(o['kind'], o['kind'])} · {o['label']} — "
            f"${o['cost_usd']:,.0f}, {o['delay_minutes']} min, "
            f"{o['pax_impacted']} pax affected (score {o['score']:.3f})"
        ),
        key=f"choice_{detail['id']}",
    )

    comment = st.text_input(
        "Comment (recorded on the disruption)",
        key=f"comment_{detail['id']}",
        placeholder="e.g. Weather window confirmed with ATC",
    )

    actor = st.session_state.get("actor", "demo-user")
    c1, c2, c3 = st.columns([1, 1, 2])

    if c1.button("✅ Approve", type="primary", width="stretch"):
        try:
            api.approve(
                base_url,
                detail["id"],
                option_id=chosen["id"],
                comment=comment,
                approved_by=actor,
            )
            st.session_state["flash"] = (
                "success",
                f"Approved: {chosen['label']}. Coordination and Passenger "
                "Comms are running — press Refresh to watch them finish.",
            )
        except api.ApiError as exc:
            st.session_state["flash"] = ("error", exc.detail)
        st.rerun()

    replan = c3.checkbox(
        "Ask for a new plan instead of closing",
        value=True,
        key=f"replan_{detail['id']}",
        help=(
            "On: the disruption goes back to the Recovery agent for fresh "
            "options. Off: the disruption is closed."
        ),
    )

    if c2.button("⛔ Reject", width="stretch"):
        try:
            api.reject(
                base_url,
                detail["id"],
                reason=comment,
                request_replan=replan,
                rejected_by=actor,
            )
            st.session_state["flash"] = (
                "success",
                "Plan rejected — regenerating options."
                if replan
                else "Plan rejected and disruption closed.",
            )
        except api.ApiError as exc:
            st.session_state["flash"] = ("error", exc.detail)
        st.rerun()


def execution_pane(detail: dict) -> None:
    log = detail.get("execution_log") or []
    if not log:
        st.caption("Nothing executed yet.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Status": {"DONE": "✅", "SKIPPED": "➖", "BLOCKED": "⛔"}.get(
                        e.get("status"), "•"
                    )
                    + " "
                    + str(e.get("status")),
                    "Team": e.get("team"),
                    "Action": e.get("action"),
                    "Detail": e.get("detail"),
                }
                for e in log
            ]
        ),
        width="stretch",
        hide_index=True,
    )


def notifications_pane(detail: dict) -> None:
    notifications = detail.get("notifications") or []
    if not notifications:
        st.caption(
            "No notifications drafted yet — they are written once the approved "
            "option has been executed."
        )
        return

    st.info(
        "📭 **Logged, not sent.** Phase 1 writes every message to the "
        "`notifications` table with `status='logged'` and prints it to the API "
        "console. Nothing leaves the app until a Resend or Telegram key is "
        "wired up in Phase 4.",
        icon="📭",
    )

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "PNR": n["pnr_locator"] or "—",
                    "Channel": n["channel"],
                    "Recipient": n["recipient"] or "—",
                    "Subject": n["subject"] or "—",
                    "Offer": n["offer"] or "—",
                    "Status": n["status"],
                }
                for n in notifications
            ]
        ),
        width="stretch",
        hide_index=True,
    )

    st.markdown("**Read a drafted message**")
    choice = st.selectbox(
        "Message",
        options=notifications,
        format_func=lambda n: (
            f"{n['pnr_locator'] or 'ops'} · {n['channel']} · "
            f"{(n['subject'] or n['body'])[:70]}"
        ),
        label_visibility="collapsed",
        key=f"notif_{detail['id']}",
    )
    if choice:
        if choice["subject"]:
            st.markdown(f"**Subject:** {choice['subject']}")
        if choice["recipient"]:
            st.caption(f"To: {choice['recipient']} · channel: {choice['channel']}")
        st.text(choice["body"])
        if choice["offer"]:
            st.success(f"Entitlement offered: {choice['offer']}")


def evidence_pane(detail: dict) -> None:
    evidence = detail.get("evidence") or []
    if evidence:
        st.markdown("**Detection evidence**")
        st.caption(
            "What the Detection agent actually read. Weather is a live, "
            "keyless call; a `demo_override` source means the scenario "
            "injected the fog rather than Delhi genuinely being fogged in."
        )
        st.dataframe(
            pd.DataFrame(
                [{"Source": e["source"], "Detail": e["detail"]} for e in evidence]
            ),
            width="stretch",
            hide_index=True,
        )

    flights = detail.get("affected_flights") or []
    if flights:
        st.markdown("**Affected flights**")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Flight": f["flight_no"],
                        "Route": f"{f['origin']} → {f['destination']}",
                        "STD": _hhmm(f["std"]),
                        "ETD": _hhmm(f["etd"]),
                        "Delay": f"{f['delay_minutes']} min",
                        "Status": f["status"],
                        "Gate": f["gate"] or "—",
                        "Tail": _tail(f),
                    }
                    for f in flights
                ]
            ),
            width="stretch",
            hide_index=True,
        )


def timeline_pane(detail: dict) -> None:
    timeline = detail.get("timeline") or []
    if not timeline:
        return
    st.markdown("**State timeline**")
    for entry in timeline:
        icon = STATE_COLOURS.get(entry["state"], "⚪")
        when = _hhmmss(entry.get("at"))
        note = entry.get("note") or ""
        st.markdown(f"{icon} **{entry['state']}** · `{when}`  \n{note}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    base_url = sidebar()

    st.title("Disruption Management & Recovery Orchestrator")

    flash = st.session_state.pop("flash", None)
    if flash:
        level, message = flash
        getattr(st, level)(message)

    try:
        rows = api.list_disruptions(base_url)
    except api.ApiError as exc:
        st.error(exc.detail)
        return

    if not rows:
        st.info(
            "No disruptions yet. Press **🌫️ Inject Fog at DEL** in the sidebar "
            "to run the scenario."
        )
        st.caption(
            "The run goes: Detection → Impact → Recovery → **your approval** → "
            "Coordination → Passenger Comms."
        )
        return

    st.subheader("Disruptions")
    disruption_table(rows)

    ids = [r["id"] for r in rows]
    selected = st.session_state.get("selected_id")
    if selected not in ids:
        selected = ids[0]

    by_id = {r["id"]: r for r in rows}
    chosen_id = st.selectbox(
        "Disruption to inspect",
        options=ids,
        index=ids.index(selected),
        format_func=lambda i: (
            f"#{i} · {by_id[i]['type']} at {by_id[i]['airport']} · "
            f"{by_id[i]['state']}"
        ),
    )
    st.session_state["selected_id"] = chosen_id

    try:
        detail = api.get_disruption(base_url, chosen_id)
    except api.ApiError as exc:
        st.error(exc.detail)
        return

    st.divider()

    head_l, head_r = st.columns([3, 1])
    with head_l:
        st.header(
            f"{STATE_COLOURS.get(detail['state'], '⚪')} #{detail['id']} · "
            f"{detail['type']} at {detail['airport']}"
        )
        st.caption(
            f"{SEVERITY_COLOURS.get(detail['severity'], '')} "
            f"{detail['severity']} · detection confidence "
            f"{detail['confidence']:.0%} · impact begins in "
            f"~{detail['eta_minutes']} min"
            + (f" · approved by {detail['approved_by']}" if detail["approved_by"] else "")
        )
    with head_r:
        st.metric("State", detail["state"])

    if detail["state"] in IN_FLIGHT:
        st.warning(
            f"The orchestrator is working on this ({detail['state']}). "
            "Press **🔄 Refresh** in the sidebar to see the next step.",
            icon="⏳",
        )
    elif detail["state"] == "NEEDS_HUMAN":
        st.error(
            f"The run stopped and needs a human: {detail.get('note') or 'no detail'}",
            icon="🛑",
        )

    # The approval gate goes first — it is the only thing a controller must act on.
    if detail["state"] == "PENDING_APPROVAL":
        st.subheader("⏸️ Awaiting your approval")
        approval_pane(base_url, detail)
        st.divider()

    tabs = st.tabs(
        [
            "📊 Impact",
            f"🛠️ Recovery options ({len(detail.get('options') or [])})",
            "⚙️ Execution",
            f"✉️ Notifications ({len(detail.get('notifications') or [])})",
            "🔍 Evidence & flights",
            "🕒 Timeline",
        ]
    )
    with tabs[0]:
        impact_pane(detail)
    with tabs[1]:
        options_pane(base_url, detail)
    with tabs[2]:
        execution_pane(detail)
    with tabs[3]:
        notifications_pane(detail)
    with tabs[4]:
        evidence_pane(detail)
    with tabs[5]:
        timeline_pane(detail)


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _tail(flight: dict) -> str:
    """Operating tail, showing the swap when recovery changed it."""
    operating = flight.get("registration")
    scheduled = flight.get("scheduled_registration")
    if not operating:
        return "—"
    if scheduled and scheduled != operating:
        return f"{operating} (was {scheduled})"
    return operating


def _hhmm(value: str | None) -> str:
    parsed = _parse(value)
    return f"{parsed:%H:%M}Z" if parsed else "—"


def _hhmmss(value: str | None) -> str:
    parsed = _parse(value)
    return f"{parsed:%H:%M:%S}Z" if parsed else "—"


def _ago(value: str | None) -> str:
    parsed = _parse(value)
    if parsed is None:
        return "—"
    seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{int(seconds // 3600)}h ago"


main()
