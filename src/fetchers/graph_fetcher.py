import csv
import io

import requests
from azure.identity import ClientSecretCredential

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"


def _period(days: int) -> str:
    for threshold, label in ((7, "D7"), (30, "D30"), (90, "D90")):
        if days <= threshold:
            return label
    return "D180"


def _int(val: str) -> int | None:
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


class GraphFetcher:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self._credential = ClientSecretCredential(tenant_id, client_id, client_secret)

    def _headers(self) -> dict:
        token = self._credential.get_token(_GRAPH_SCOPE).token
        return {"Authorization": f"Bearer {token}"}

    def _fetch_csv(self, path: str) -> list[dict]:
        resp = requests.get(
            f"{_GRAPH_BASE}{path}",
            headers=self._headers(),
            timeout=60,
            allow_redirects=True,
        )
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))

    def fetch_copilot_usage(self, lookback_days: int = 30) -> list[dict]:
        rows = self._fetch_csv(
            f"/reports/getMicrosoft365CopilotUsageUserDetail(period='{_period(lookback_days)}')"
        )
        out = []
        for r in rows:
            out.append({
                "report_refresh_date": r.get("Report Refresh Date", ""),
                "user_principal_name": r.get("User Principal Name", ""),
                "display_name": r.get("Display Name", ""),
                "last_activity_date": r.get("Last Activity Date", ""),
                "teams_chats": _int(r.get("Teams Chats", "")),
                "teams_meetings": _int(r.get("Teams Meetings", "")),
                "word": _int(r.get("Word", "")),
                "excel": _int(r.get("Excel", "")),
                "powerpoint": _int(r.get("PowerPoint", "")),
                "outlook": _int(r.get("Outlook", "")),
                "onenote": _int(r.get("OneNote", "")),
                "loop": _int(r.get("Loop", "")),
                "copilot_chat": _int(r.get("Copilot Chat", "")),
                "report_period": r.get("Report Period", ""),
            })
        return out

    def fetch_teams_activity(self, lookback_days: int = 30) -> list[dict]:
        rows = self._fetch_csv(
            f"/reports/getTeamsUserActivityUserDetail(period='{_period(lookback_days)}')"
        )
        out = []
        for r in rows:
            out.append({
                "report_refresh_date": r.get("Report Refresh Date", ""),
                "user_principal_name": r.get("User Principal Name", ""),
                "last_activity_date": r.get("Last Activity Date", ""),
                "team_chat_messages": _int(r.get("Team Chat Message Count", "")),
                "private_chat_messages": _int(r.get("Private Chat Message Count", "")),
                "calls": _int(r.get("Call Count", "")),
                "meetings": _int(r.get("Meeting Count", "")),
                "meetings_organized": _int(r.get("Meetings Organized Count", "")),
                "meetings_attended": _int(r.get("Meetings Attended Count", "")),
                "report_period": r.get("Report Period", ""),
            })
        return out
