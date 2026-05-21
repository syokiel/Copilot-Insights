"""
Azure Monitor fetcher — dependency failures, exceptions, and alerts.

Uses the same LogsQueryClient as OtelFetcher but targets different tables
(AppDependencies failures, AppExceptions) plus Azure Resource Graph for alerts.
"""

import hashlib
from datetime import timedelta

from azure.monitor.query import LogsQueryClient, LogsQueryStatus

_DEPENDENCY_FAILURES_KQL = """
AppDependencies
| where Success == false
| extend props = parse_json(Properties)
| project
    OperationId,
    AgentId       = tostring(props["gen_ai.agent.id"]),
    AgentName     = tostring(props["gen_ai.agent.name"]),
    EnvId         = tostring(props["gen_ai.environment.id"]),
    ConversationId = tostring(props["conversationId"]),
    DependencyName = Name,
    ResultCode,
    Success,
    DurationMs,
    Timestamp     = TimeGenerated
| order by Timestamp desc
"""

_EXCEPTIONS_KQL = """
AppExceptions
| extend props = parse_json(Properties)
| project
    OperationId,
    AgentId        = tostring(props["gen_ai.agent.id"]),
    ConversationId = tostring(props["conversationId"]),
    ExceptionType,
    ExceptionMessage = OuterMessage,
    Timestamp      = TimeGenerated
| order by Timestamp desc
"""


class AzureMonitorFetcher:
    def __init__(
        self,
        client: LogsQueryClient,
        workspace_id: str,
    ) -> None:
        self._client = client
        self._workspace_id = workspace_id

    def _run_query(self, kql: str, lookback: timedelta) -> list[dict]:
        response = self._client.query_workspace(
            workspace_id=self._workspace_id,
            query=kql,
            timespan=lookback,
        )
        if response.status == LogsQueryStatus.SUCCESS:
            table = response.tables[0]
        elif response.status == LogsQueryStatus.PARTIAL:
            table = response.partial_data[0]
        else:
            raise RuntimeError(f"Azure Monitor query failed: {response.status}")
        columns = table.columns
        return [dict(zip(columns, row)) for row in table.rows]

    def fetch_dependency_failures(self, hours_back: int = 24) -> list[dict]:
        """Failed AppDependencies entries — connector/API calls that errored."""
        return self._run_query(_DEPENDENCY_FAILURES_KQL, timedelta(hours=hours_back))

    def fetch_exceptions(self, hours_back: int = 24) -> list[dict]:
        """AppExceptions entries logged by the agent runtime."""
        return self._run_query(_EXCEPTIONS_KQL, timedelta(hours=hours_back))

    def fetch_alerts(self, hours_back: int = 24, subscription_id: str = "") -> list[dict]:
        """
        Azure Resource Graph query for fired alerts.
        Requires azure-mgmt-resourcegraph and the SP to have Reader on the subscription.
        Returns empty list if the package is not installed or subscription is not set.
        """
        if not subscription_id:
            return []
        try:
            from azure.mgmt.resourcegraph import ResourceGraphClient
            from azure.mgmt.resourcegraph.models import QueryRequest
        except ImportError:
            return []

        try:
            from src.auth import get_credential
            rg_client = ResourceGraphClient(get_credential())
        except Exception:
            return []

        cutoff = f"ago({hours_back}h)"
        query = f"""
        AlertsManagementResources
        | where type == 'microsoft.alertsmanagement/alerts'
        | where properties.essentials.firedDateTime > {cutoff}
        | project
            alert_id    = id,
            agent_id    = tostring(tags['gen_ai.agent.id']),
            alert_name  = name,
            severity    = tostring(properties.essentials.severity),
            fired_time  = tostring(properties.essentials.firedDateTime),
            resource_id = tostring(properties.essentials.targetResourceIds[0])
        | order by fired_time desc
        """
        try:
            result = rg_client.resources(QueryRequest(
                subscriptions=[subscription_id],
                query=query,
            ))
            cols = [c.name for c in result.columns]
            return [dict(zip(cols, row)) for row in result.rows]
        except Exception:
            return []


def dep_row_id(row: dict) -> str:
    key = f"{row.get('OperationId')}|{row.get('DependencyName')}|{row.get('Timestamp')}"
    return hashlib.sha1(key.encode()).hexdigest()


def exc_row_id(row: dict) -> str:
    key = f"{row.get('OperationId')}|{row.get('ExceptionType')}|{row.get('Timestamp')}"
    return hashlib.sha1(key.encode()).hexdigest()
