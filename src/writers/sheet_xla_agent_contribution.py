from openpyxl.worksheet.worksheet import Worksheet

from src.writers._style import apply_row_style, autofit_columns, write_headers

HEADERS = [
    "Agent Name", "Persona", "Journey",
    "Total Sessions", "Completion %", "Escalation %", "Abandonment %",
]

_FIELDS = [
    "agent_name", "persona_type", "journey_name",
    "total_sessions", "completion_rate_pct", "escalation_rate_pct", "abandonment_rate_pct",
]

_PERSONA_LABELS = {
    "end_user":        "End User",
    "it_support":      "IT Support",
    "hr":              "HR",
    "knowledge_worker": "Knowledge Worker",
    "operations":      "Operations",
}


def write(ws: Worksheet, rows: list[dict]) -> None:
    write_headers(ws, HEADERS)

    if not rows:
        ws.cell(row=2, column=1, value="— No agent contribution data: set AGENT_JOURNEY_MAP and ensure Viva CS session metrics are imported —")
    else:
        for i, r in enumerate(rows, start=2):
            for col, field in enumerate(_FIELDS, start=1):
                val = r.get(field)
                if field == "persona_type":
                    val = _PERSONA_LABELS.get(val, val)
                ws.cell(row=i, column=col, value=val)
            apply_row_style(ws, i, len(HEADERS))

    autofit_columns(ws, HEADERS)
