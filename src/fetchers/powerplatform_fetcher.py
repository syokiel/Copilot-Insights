import json

import requests
from azure.core.credentials import TokenCredential


def _parse_ai_model(configuration: str) -> str:
    """Extract the AI model name from a Copilot Studio bot configuration JSON blob.

    Copilot Studio serialises AI model settings differently across versions, so
    we walk several known key paths and return the first non-empty string found.
    If nothing matches the raw JSON is returned truncated so it's inspectable.
    """
    if not configuration:
        return ""
    try:
        cfg = json.loads(configuration)
    except (json.JSONDecodeError, TypeError):
        return ""

    # Candidate paths — checked in order of specificity
    candidates = [
        # V3 / Studio 2024+
        cfg.get("DefaultAIModel"),
        # Nested under generative AI section
        (cfg.get("generativeAI") or {}).get("modelId"),
        (cfg.get("AICopilotConfig") or {}).get("ModelId"),
        (cfg.get("AICopilotConfig") or {}).get("modelId"),
        # Flat keys
        cfg.get("aiModel"),
        cfg.get("ModelId"),
        cfg.get("modelId"),
        cfg.get("model"),
        cfg.get("AIType"),
    ]
    for val in candidates:
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


_PP_API_BASE = "https://api.powerplatform.com"
_PP_SCOPE = "https://api.powerplatform.com/.default"
_INVENTORY_API = f"{_PP_API_BASE}/resourcequery/resources/query?api-version=2024-10-01"
_INVENTORY_PAGE_SIZE = 1000

# BAP endpoint — used by configure_agent_telemetry.py; returns all tenant environments
_BAP_BASE = "https://api.bap.microsoft.com"
_BAP_SCOPE = "https://api.bap.microsoft.com/.default"

# Dynamics 365 Global Discovery — returns Dataverse orgs the SP has been granted access to
_DISCO_BASE = "https://globaldisco.crm.dynamics.com"
_DISCO_SCOPE = "https://globaldisco.crm.dynamics.com/.default"


class PowerPlatformFetcher:
    def __init__(
        self,
        credential: TokenCredential,
        dataverse_url: str = "",
        environment_id: str = "",
    ) -> None:
        self._dataverse_url = dataverse_url.rstrip("/")
        self._environment_id = environment_id
        self._credential = credential
        self._dv_scope = f"{self._dataverse_url}/.default" if dataverse_url else ""

    def _headers(self) -> dict:
        """Dataverse Web API headers."""
        token = self._credential.get_token(self._dv_scope).token
        return {
            "Authorization": f"Bearer {token}",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Accept": "application/json",
        }

    def _pp_headers(self) -> dict:
        """Power Platform Admin API headers."""
        token = self._credential.get_token(_PP_SCOPE).token
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _bap_headers(self) -> dict:
        token = self._credential.get_token(_BAP_SCOPE).token
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _disco_headers(self) -> dict:
        token = self._credential.get_token(_DISCO_SCOPE).token
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # ------------------------------------------------------------------
    # Agents (Dataverse) — single env or all environments
    # ------------------------------------------------------------------

    def fetch_agents_from(self, dataverse_url: str, environment_id: str) -> list[dict]:
        """Fetch agents from a specific Dataverse org URL."""
        dv_url = dataverse_url.rstrip("/")
        scope = f"{dv_url}/.default"
        token = self._credential.get_token(scope).token
        headers = {
            "Authorization": f"Bearer {token}",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Accept": "application/json",
        }
        resp = requests.get(
            f"{dv_url}/api/data/v9.2/bots",
            headers=headers,
            params={"$select": "botid,name,schemaname,createdon,modifiedon,publishedon,configuration"},
            timeout=30,
        )
        resp.raise_for_status()
        return [
            {
                "id": b.get("botid", ""),
                "name": b.get("name", ""),
                "schemaName": b.get("schemaname", ""),
                "environmentId": environment_id,
                "createdDateTime": b.get("createdon", ""),
                "modifiedDateTime": b.get("modifiedon", ""),
                "publishedDateTime": b.get("publishedon", ""),
                "aiModel": _parse_ai_model(b.get("configuration") or ""),
            }
            for b in resp.json().get("value", [])
        ]

    def fetch_agents(self) -> list[dict]:
        """Fetch agents from the configured Dataverse URL (single-environment convenience wrapper)."""
        return self.fetch_agents_from(self._dataverse_url, self._environment_id)

    def fetch_inventory_agents(self) -> list[dict]:
        """
        Fetch ALL Copilot Studio V2 agents across the entire tenant via the
        Power Platform Inventory API (bypasses the 1,000-item PPAC display cap).

        Returns richer metadata than the per-environment Dataverse agent API:
        createdBy, ownerId, createdIn (Copilot Studio / Agent Builder).
        Classic PVA V1 agents are NOT included.
        """
        headers = self._pp_headers()
        field_list = [
            "agentId = name",
            "displayName = tostring(properties.displayName)",
            "environmentId = tostring(properties.environmentId)",
            "createdAt = tostring(properties.createdAt)",
            "createdBy = tostring(properties.createdBy)",
            "ownerId = tostring(properties.ownerId)",
            "lastPublishedAt = tostring(properties.lastPublishedAt)",
            "createdIn = tostring(properties.createdIn)",
            "schemaName = tostring(properties.schemaName)",
        ]
        rows, seen, skip_token, prev_token = [], set(), "", None
        while True:
            options: dict = {"Top": _INVENTORY_PAGE_SIZE}
            if skip_token:
                options["SkipToken"] = skip_token
            body = {
                "TableName": "PowerPlatformResources",
                "Clauses": [
                    {"$type": "where", "FieldName": "type",
                     "Operator": "==", "Values": ["'microsoft.copilotstudio/agents'"]},
                    {"$type": "project", "FieldList": field_list},
                ],
                "Options": options,
            }
            resp = requests.post(_INVENTORY_API, headers=headers, json=body, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            page = data.get("data", [])
            new = 0
            for r in page:
                key = r.get("agentId")
                if key not in seen:
                    seen.add(key)
                    rows.append({
                        "id": r.get("agentId", ""),
                        "name": r.get("displayName", ""),
                        "schemaName": r.get("schemaName", ""),
                        "environmentId": r.get("environmentId", ""),
                        "createdDateTime": r.get("createdAt", ""),
                        "modifiedDateTime": "",
                        "publishedDateTime": r.get("lastPublishedAt", ""),
                        "createdBy": r.get("createdBy", ""),
                        "ownerId": r.get("ownerId", ""),
                        "createdIn": r.get("createdIn", ""),
                    })
                    new += 1
            total = data.get("totalRecords")
            skip_token = data.get("skipToken") or ""
            if not skip_token:
                break
            if total is not None and len(rows) >= total:
                break
            if new == 0:
                break
            if skip_token == prev_token:
                break
            prev_token = skip_token
        return rows

    # ------------------------------------------------------------------
    # Environments (Power Platform Admin API)
    # ------------------------------------------------------------------

    def fetch_environments(self) -> list[dict]:
        """
        Return ALL Power Platform environments in the tenant.

        Discovery order (first success wins):
          1. BAP Admin API  — full tenant view; requires Power Platform Administrator role
          2. Global Discovery — Dataverse orgs the SP has been explicitly granted access to
          3. Config fallback  — the single env in DATAVERSE_URL / POWERPLATFORM_ENVIRONMENT_ID
        """
        # ── 1. BAP Admin API (full tenant) ──────────────────────────────────────
        try:
            resp = requests.get(
                f"{_BAP_BASE}/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments",
                headers=self._bap_headers(),
                params={"api-version": "2021-04-01"},
                timeout=30,
            )
            resp.raise_for_status()
            out = []
            for e in resp.json().get("value", []):
                props = e.get("properties", {})
                linked = props.get("linkedEnvironmentMetadata") or {}
                out.append({
                    "environment_id": e.get("name", ""),
                    "display_name": props.get("displayName", ""),
                    "type": props.get("environmentSku", ""),
                    "region": e.get("location", ""),
                    "state": (props.get("states", {}).get("runtime", {}) or {}).get("id", ""),
                    "created_at": props.get("createdTime", ""),
                    "modified_at": props.get("modifiedTime", ""),
                    "sku": props.get("environmentSku", ""),
                    "dataverse_url": linked.get("instanceUrl", "").rstrip("/"),
                })
            if out:
                return out
        except Exception as e:
            print(f"  WARNING: BAP Admin API unavailable ({e}), trying Global Discovery...")

        # ── 2. Global Discovery (orgs the SP has Dataverse access to) ───────────
        try:
            resp = requests.get(
                f"{_DISCO_BASE}/api/discovery/v2.0/Instances",
                headers=self._disco_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            out = []
            for inst in resp.json().get("value", []):
                out.append({
                    "environment_id": inst.get("EnvironmentId", ""),
                    "display_name": inst.get("FriendlyName", ""),
                    "type": inst.get("EnvironmentSku", ""),
                    "region": inst.get("DatacenterRegion", ""),
                    "state": "Ready",
                    "created_at": "",
                    "modified_at": "",
                    "sku": inst.get("EnvironmentSku", ""),
                    "dataverse_url": inst.get("Url", "").rstrip("/"),
                })
            if out:
                return out
        except Exception as e:
            print(f"  WARNING: Global Discovery unavailable ({e}), using config fallback")

        # ── 3. Config fallback (single env) ─────────────────────────────────────
        if self._environment_id:
            return [{
                "environment_id": self._environment_id,
                "display_name": "",
                "type": "",
                "region": "",
                "state": "Ready",
                "created_at": "",
                "modified_at": "",
                "sku": "",
                "dataverse_url": self._dataverse_url,
            }]
        return []

    # ------------------------------------------------------------------
    # Publishers (Dataverse)
    # ------------------------------------------------------------------

    def fetch_publishers(self) -> list[dict]:
        """Return solution publishers from Dataverse (excluding Microsoft built-ins)."""
        url = f"{self._dataverse_url}/api/data/v9.2/publishers"
        resp = requests.get(
            url,
            headers=self._headers(),
            params={
                "$select": "publisherid,friendlyname,uniquename,emailaddress,customizationprefix",
                "$filter": "isreadonly eq false",
                "$orderby": "friendlyname asc",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return [
            {
                "publisher_id": p.get("publisherid", ""),
                "display_name": p.get("friendlyname", ""),
                "unique_name": p.get("uniquename", ""),
                "email": p.get("emailaddress", "") or "",
                "phone": "",
                "custom_prefix": p.get("customizationprefix", ""),
                "solution_count": None,
            }
            for p in resp.json().get("value", [])
        ]

    # ------------------------------------------------------------------
    # Solutions (Dataverse)
    # ------------------------------------------------------------------

    def fetch_agent_solutions_from(self, dataverse_url: str) -> list[dict]:
        """
        Return solution membership for agents in a specific Dataverse org.
        componenttype 380 = Chatbot in Power Platform.
        Returns empty list if the component type doesn't exist or access is denied.
        """
        dv_url = dataverse_url.rstrip("/")
        scope = f"{dv_url}/.default"
        token = self._credential.get_token(scope).token
        headers = {
            "Authorization": f"Bearer {token}",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Accept": "application/json",
        }
        try:
            resp = requests.get(
                f"{dv_url}/api/data/v9.2/solutioncomponents",
                headers=headers,
                params={
                    "$filter": "componenttype eq 380",
                    "$select": "objectid,_solutionid_value",
                    "$expand": "solutionid($select=uniquename,friendlyname,version,ismanaged)",
                },
                timeout=30,
            )
            resp.raise_for_status()
            out = []
            for sc in resp.json().get("value", []):
                sol = sc.get("solutionid") or {}
                out.append({
                    "agent_id": sc.get("objectid", ""),
                    "solution_id": sc.get("_solutionid_value", ""),
                    "solution_name": sol.get("friendlyname", ""),
                    "solution_unique": sol.get("uniquename", ""),
                    "version": sol.get("version", ""),
                    "is_managed": sol.get("ismanaged", False),
                })
            return out
        except Exception:
            return []

    def fetch_agent_solutions(self) -> list[dict]:
        """Fetch agent solutions from the configured Dataverse URL (single-environment wrapper)."""
        return self.fetch_agent_solutions_from(self._dataverse_url)

    # ------------------------------------------------------------------
    # DLP Policies (Power Platform Admin API)
    # ------------------------------------------------------------------

    def fetch_dlp_policies(self) -> list[dict]:
        """Return tenant-level DLP policies."""
        # Try the newer Power Platform API first — better app-only SP support
        try:
            resp = requests.get(
                f"{_PP_API_BASE}/governance/connectorPolicies",
                headers=self._pp_headers(),
                params={"api-version": "2022-03-01-preview"},
                timeout=30,
            )
            resp.raise_for_status()
            out = []
            for p in resp.json().get("value", []):
                props = p.get("properties", {})
                groups = props.get("connectorGroups", [])
                # Newer API uses Confidential/General; older used Business/NonBusiness
                out.append({
                    "policy_id": p.get("name", ""),
                    "display_name": props.get("displayName", ""),
                    "environment_type": props.get("environmentType", ""),
                    "created_by": (props.get("createdBy", {}) or {}).get("displayName", ""),
                    "created_at": props.get("createdTime", ""),
                    "modified_at": props.get("lastModifiedTime", ""),
                    "enforcement_mode": props.get("etag", ""),
                    "blocked_connectors": _connector_names(groups, "Blocked"),
                    "business_connectors": _connector_names(groups, "Confidential") or _connector_names(groups, "Business"),
                    "non_business_connectors": _connector_names(groups, "General") or _connector_names(groups, "NonBusiness"),
                })
            return out
        except Exception:
            pass

        # Fall back to BAP Admin API
        try:
            resp = requests.get(
                f"{_BAP_BASE}/providers/Microsoft.BusinessAppPlatform/scopes/admin/apiPolicies",
                headers=self._bap_headers(),
                params={"api-version": "2021-04-01"},
                timeout=30,
            )
            resp.raise_for_status()
            out = []
            for p in resp.json().get("value", []):
                props = p.get("properties", {})
                groups = props.get("connectorGroups", [])
                out.append({
                    "policy_id": p.get("name", ""),
                    "display_name": props.get("displayName", ""),
                    "environment_type": props.get("environmentType", ""),
                    "created_by": (props.get("createdBy", {}) or {}).get("displayName", ""),
                    "created_at": props.get("createdTime", ""),
                    "modified_at": props.get("lastModifiedTime", ""),
                    "enforcement_mode": props.get("etag", ""),
                    "blocked_connectors": _connector_names(groups, "Blocked"),
                    "business_connectors": _connector_names(groups, "Business"),
                    "non_business_connectors": _connector_names(groups, "NonBusiness"),
                })
            return out
        except Exception:
            return []


def _connector_names(groups: list, classification: str) -> str:
    for g in groups:
        if g.get("classification") == classification:
            return ", ".join(
                c.get("name", c.get("id", "")) for c in g.get("connectors", [])
            )
    return ""
