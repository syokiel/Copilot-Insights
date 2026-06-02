from azure.core.credentials import TokenCredential
from azure.identity import (
    AzureCliCredential,
    ChainedTokenCredential,
    ClientSecretCredential,
    InteractiveBrowserCredential,
)
from azure.monitor.query import LogsQueryClient
from msgraph import GraphServiceClient

from config.settings import settings

_credential: TokenCredential | None = None


def get_credential() -> TokenCredential:
    global _credential
    if _credential is None:
        if settings.azure_client_id and settings.azure_client_secret:
            _credential = ClientSecretCredential(
                tenant_id=settings.azure_tenant_id,
                client_id=settings.azure_client_id,
                client_secret=settings.azure_client_secret,
            )
        else:
            # No service principal — try Azure CLI first, then browser
            tenant_id = settings.azure_tenant_id or None
            _credential = ChainedTokenCredential(
                AzureCliCredential(tenant_id=tenant_id),
                InteractiveBrowserCredential(tenant_id=tenant_id),
            )
    return _credential


def get_logs_client() -> LogsQueryClient:
    return LogsQueryClient(get_credential())


def get_graph_client() -> GraphServiceClient:
    return GraphServiceClient(
        credentials=get_credential(),
        scopes=["https://graph.microsoft.com/.default"],
    )


def get_power_platform_token() -> str:
    token = get_credential().get_token("https://api.powerplatform.com/.default")
    return token.token
