from openpyxl.worksheet.worksheet import Worksheet

from src.writers._style import autofit_columns, write_headers

HEADERS = [
    "Agent ID",
    "Agent Name",
    "Environment ID",
    "Environment Name",
    "Created Date",
    "Modified Date",
    "Published",
    "Solution Name",
    "Solution Version",
    "Managed",
]


def write(ws: Worksheet, agents: list[dict], environments: list[dict] = [], bot_solutions: list[dict] = []) -> None:
    write_headers(ws, HEADERS)

    env_by_id = {e["environment_id"]: e["display_name"] for e in environments if e.get("environment_id")}
    sol_by_bot = {s["bot_id"]: s for s in bot_solutions if s.get("bot_id")}

    if not agents:
        ws.cell(row=2, column=1, value="— No agent data synced yet —")
    else:
        for row_idx, bot in enumerate(agents, start=2):
            bot_id = bot.get("bot_id") or bot.get("id", "")
            env_id = bot.get("environment_id") or bot.get("environmentId", "")
            sol = sol_by_bot.get(bot_id, {})
            published = bot.get("published_at") or bot.get("publishedDateTime", "")
            ws.cell(row=row_idx, column=1, value=bot_id)
            ws.cell(row=row_idx, column=2, value=bot.get("display_name") or bot.get("name", ""))
            ws.cell(row=row_idx, column=3, value=env_id)
            ws.cell(row=row_idx, column=4, value=env_by_id.get(env_id, ""))
            ws.cell(row=row_idx, column=5, value=bot.get("created_at") or bot.get("createdDateTime", ""))
            ws.cell(row=row_idx, column=6, value=bot.get("modified_at") or bot.get("modifiedDateTime", ""))
            ws.cell(row=row_idx, column=7, value="Yes" if published else "No")
            ws.cell(row=row_idx, column=8, value=sol.get("solution_name", ""))
            ws.cell(row=row_idx, column=9, value=sol.get("version", ""))
            ws.cell(row=row_idx, column=10, value="Yes" if sol.get("is_managed") else ("No" if sol else ""))

    autofit_columns(ws, HEADERS)
