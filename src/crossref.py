"""
Cross-reference OTel telemetry with Azure Monitor signals.

Joins conversation_events / connector_calls (OTel) with dependency failures,
exceptions, and alerts (Azure Monitor) on ConversationId.  Each row is
classified with one of four flags:

  BOTH_FLAGS    — OTel shows a failed connector call AND Azure Monitor fired
                  an exception or alert for the same conversation
  MONITOR_ONLY  — Azure Monitor failure/alert with no matching OTel conversation
  OTEL_ONLY     — OTel connector failure with no matching Azure Monitor signal
  CLEAN         — both sides look healthy
"""

from __future__ import annotations

FLAG_BOTH = "BOTH_FLAGS"
FLAG_MONITOR_ONLY = "MONITOR_ONLY"
FLAG_OTEL_ONLY = "OTEL_ONLY"
FLAG_CLEAN = "CLEAN"

# Lower number = higher severity, used for sorting
FLAG_SEVERITY: dict[str, int] = {
    FLAG_BOTH: 0,
    FLAG_MONITOR_ONLY: 1,
    FLAG_OTEL_ONLY: 2,
    FLAG_CLEAN: 3,
}


def build_crossref(
    otel_events: list[dict],
    otel_connector_calls: list[dict],
    az_dependency_failures: list[dict],
    az_exceptions: list[dict],
    az_alerts: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Build health and cross-reference tables.

    Returns:
        health_detail   — one row per conversation with merged Azure Monitor signals
        crossref_summary — subset of health_detail where flag != CLEAN, sorted by severity
    """
    # -----------------------------------------------------------------
    # Index OTel data by conversation_id
    # -----------------------------------------------------------------
    otel_conv_ids: set[str] = {
        e.get("ConversationId") or e.get("conversation_id", "")
        for e in otel_events
        if e.get("ConversationId") or e.get("conversation_id")
    }

    # Failed connector calls keyed by conversation_id
    otel_failed: dict[str, list[dict]] = {}
    for c in otel_connector_calls:
        conv = c.get("ConversationId") or c.get("conversation_id", "")
        if not conv:
            continue
        if not (c.get("Success") or c.get("success")):
            otel_failed.setdefault(conv, []).append(c)

    # First event per conversation for metadata
    otel_first: dict[str, dict] = {}
    for e in sorted(otel_events, key=lambda x: x.get("Timestamp") or x.get("timestamp") or ""):
        conv = e.get("ConversationId") or e.get("conversation_id", "")
        if conv and conv not in otel_first:
            otel_first[conv] = e

    # -----------------------------------------------------------------
    # Index Azure Monitor data by ConversationId (falls back to OperationId)
    # -----------------------------------------------------------------
    az_dep_by_conv: dict[str, list[dict]] = {}
    for d in az_dependency_failures:
        conv = d.get("ConversationId") or d.get("OperationId", "")
        if conv:
            az_dep_by_conv.setdefault(conv, []).append(d)

    az_exc_by_conv: dict[str, list[dict]] = {}
    for ex in az_exceptions:
        conv = ex.get("ConversationId") or ex.get("OperationId", "")
        if conv:
            az_exc_by_conv.setdefault(conv, []).append(ex)

    az_alert_by_conv: dict[str, list[dict]] = {}
    for a in az_alerts:
        conv = a.get("agent_id", "")  # alerts tag on agent_id, not conv
        if conv:
            az_alert_by_conv.setdefault(conv, []).append(a)

    # All conversation IDs across both sides
    all_conv_ids = otel_conv_ids | set(az_dep_by_conv) | set(az_exc_by_conv)

    # -----------------------------------------------------------------
    # Build health_detail rows
    # -----------------------------------------------------------------
    health_detail: list[dict] = []

    for conv_id in all_conv_ids:
        if not conv_id:
            continue

        first_event = otel_first.get(conv_id, {})
        agent_id = (
            first_event.get("AgentId")
            or _extract_prop(first_event, "gen_ai.agent.id")
            or ""
        )
        agent_name = (
            first_event.get("AgentName")
            or _extract_prop(first_event, "gen_ai.agent.name")
            or ""
        )
        env_id = (
            first_event.get("EnvId")
            or _extract_prop(first_event, "gen_ai.environment.id")
            or ""
        )
        invocation_time = str(
            first_event.get("Timestamp") or first_event.get("timestamp") or ""
        )[:19]

        deps = az_dep_by_conv.get(conv_id, [])
        excs = az_exc_by_conv.get(conv_id, [])
        first_dep = deps[0] if deps else {}
        first_exc = excs[0] if excs else {}

        # Alerts: look up by agent_id since they're not per-conversation
        alerts = az_alert_by_conv.get(agent_id, [])
        first_alert = alerts[0] if alerts else {}

        otel_failed_calls = otel_failed.get(conv_id, [])
        otel_dlp_signal = bool(otel_failed_calls)
        az_has_signal = bool(deps or excs or alerts)

        if otel_dlp_signal and az_has_signal:
            flag = FLAG_BOTH
        elif az_has_signal and conv_id not in otel_conv_ids:
            flag = FLAG_MONITOR_ONLY
        elif otel_dlp_signal and not az_has_signal:
            flag = FLAG_OTEL_ONLY
        else:
            flag = FLAG_CLEAN

        health_detail.append({
            "conversation_id": conv_id,
            "agent_id": agent_id or first_dep.get("AgentId", ""),
            "agent_name": agent_name or first_dep.get("AgentName", ""),
            "env_id": env_id or first_dep.get("EnvId", ""),
            "invocation_time": invocation_time,
            "dependency_name": first_dep.get("DependencyName", ""),
            "dependency_result_code": first_dep.get("ResultCode", ""),
            "dependency_success": False if deps else None,
            "exception_type": first_exc.get("ExceptionType", ""),
            "exception_message": first_exc.get("ExceptionMessage", ""),
            "duration_ms": first_dep.get("DurationMs"),
            "alert_fired": bool(alerts),
            "alert_name": first_alert.get("alert_name", ""),
            "flag": flag,
            "otel_dlp_signal": otel_dlp_signal,
            "az_exception_type": first_exc.get("ExceptionType", ""),
            "az_alert_name": first_alert.get("alert_name", ""),
        })

    health_detail.sort(key=lambda r: r.get("invocation_time") or "", reverse=True)

    crossref_summary = sorted(
        [r for r in health_detail if r["flag"] != FLAG_CLEAN],
        key=lambda r: FLAG_SEVERITY.get(r["flag"], 99),
    )

    return health_detail, crossref_summary


def _extract_prop(event: dict, key: str) -> str:
    """Try to pull a value from the Properties JSON blob."""
    import json
    props_raw = event.get("Properties") or event.get("properties") or ""
    if not props_raw:
        return ""
    try:
        props = json.loads(props_raw) if isinstance(props_raw, str) else props_raw
        return str(props.get(key, ""))
    except Exception:
        return ""
