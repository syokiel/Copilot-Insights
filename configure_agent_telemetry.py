"""
configure_agent_telemetry.py
─────────────────────────────────────────────────────────────────────────────
Bulk-configures ALL Copilot Studio agents in every Power Platform environment
in a tenant with:
  • Application Insights connection string
  • All telemetry/logging toggles enabled

API surface used:
  • MSAL (Azure Identity) — auth
  • Power Platform Admin API — enumerate environments
  • Dataverse Web API (per-environment) — read & patch bot entities

Required app registration permissions:
  • Dynamics CRM → user_impersonation  (or use a service principal with
    System Administrator role in each environment)
  • PowerPlatform.Administration.ReadWrite.All (for env enumeration)

Usage:
  python configure_agent_telemetry.py \
      --appid  <Entra App Client ID>  \
      --tenant <Tenant ID>            \
      --secret <Client Secret>        \
      --appinsights-connection-string "InstrumentationKey=...;IngestionEndpoint=..."

  Optional flags:
      --env-filter  "prod,uat"   # only process environments whose name contains these substrings (comma-separated)
      --dry-run                  # print what would change, make no writes
      --log-file  run.log        # mirror console output to a file
─────────────────────────────────────────────────────────────────────────────
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests
from msal import ConfidentialClientApplication

# ── Logging setup ────────────────────────────────────────────────────────────

def setup_logging(log_file: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger("agent_telemetry")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


LOG = setup_logging()

# ── Constants ─────────────────────────────────────────────────────────────────

POWER_PLATFORM_MGMT_URL = "https://api.bap.microsoft.com"
DATAVERSE_API_VERSION   = "9.2"

# The Dataverse resource root used when acquiring tokens per-environment.
# Each env has its own URL: https://<orgname>.crm.dynamics.com
DATAVERSE_SCOPE_SUFFIX  = "/.default"

# Bot entity fields we care about (Dataverse schema names)
# These map to the Copilot Studio "Advanced → Diagnostics" settings.
BOT_TELEMETRY_FIELDS = {
    # Field name in Dataverse          : value to set
    "applicationinsightsconnectionstring": None,   # filled from CLI arg
    "enabletelemetry":                     True,   # master telemetry switch
    "enableauthenticationtelemetry":       True,   # log auth events
    "enableactivitytelemetry":             True,   # log conversation turns
    "enabletooltelemetry":                 True,   # ExecuteTool spans (connectors/actions)
    "enableperformancetelemetry":          True,   # latency & perf spans
    "enablecontentmoderation":             True,   # content-filter signals
}

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Environment:
    id:           str
    name:         str
    org_url:      str   # e.g. https://contoso.crm.dynamics.com
    state:        str


@dataclass
class BotRecord:
    bot_id:          str
    name:            str
    environment_id:  str
    current_fields:  dict = field(default_factory=dict)


@dataclass
class PatchResult:
    bot_id:   str
    bot_name: str
    env_name: str
    success:  bool
    skipped:  bool = False   # already configured — no change needed
    error:    str  = ""

# ── Auth helpers ──────────────────────────────────────────────────────────────

class TokenCache:
    """Minimal MSAL wrapper that caches tokens by resource."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        self._tenant    = tenant_id
        self._client_id = client_id
        self._secret    = client_secret
        self._apps: dict[str, ConfidentialClientApplication] = {}

    def _get_app(self, resource_url: str) -> ConfidentialClientApplication:
        if resource_url not in self._apps:
            authority = f"https://login.microsoftonline.com/{self._tenant}"
            self._apps[resource_url] = ConfidentialClientApplication(
                self._client_id,
                authority=authority,
                client_credential=self._secret,
            )
        return self._apps[resource_url]

    def get_token(self, resource_url: str) -> str:
        scope = resource_url.rstrip("/") + DATAVERSE_SCOPE_SUFFIX
        app   = self._get_app(resource_url)
        result = app.acquire_token_for_client(scopes=[scope])
        if "access_token" not in result:
            raise RuntimeError(
                f"Failed to acquire token for {resource_url}: "
                f"{result.get('error_description', result)}"
            )
        return result["access_token"]


# ── API client ────────────────────────────────────────────────────────────────

class PowerPlatformClient:

    def __init__(self, token_cache: TokenCache):
        self._cache = token_cache

    # ── headers ──

    def _headers(self, resource_url: str) -> dict:
        return {
            "Authorization": f"Bearer {self._cache.get_token(resource_url)}",
            "Content-Type":  "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version":    "4.0",
            "Prefer":           "return=minimal",   # don't return body on PATCH
        }

    # ── environments ──

    def list_environments(self) -> list[Environment]:
        """Returns all Power Platform environments visible to this service principal."""
        url   = f"{POWER_PLATFORM_MGMT_URL}/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments?api-version=2021-04-01"
        token = self._cache.get_token(POWER_PLATFORM_MGMT_URL)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }

        envs = []
        while url:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data  = resp.json()
            for item in data.get("value", []):
                props    = item.get("properties", {})
                org_url  = (
                    props.get("linkedEnvironmentMetadata", {})
                        .get("instanceUrl", "")
                        .rstrip("/")
                )
                if not org_url:
                    continue   # environment has no Dataverse org — skip

                envs.append(Environment(
                    id       = item["name"],
                    name     = props.get("displayName", item["name"]),
                    org_url  = org_url,
                    state    = props.get("states", {}).get("management", {}).get("id", ""),
                ))
            url = data.get("nextLink")   # follow paging

        LOG.info("Found %d environments with Dataverse orgs.", len(envs))
        return envs

    # ── bots ──

    def list_bots(self, env: Environment) -> list[BotRecord]:
        """Returns all bot records in a single Dataverse environment."""
        fields_to_select = "botid,name," + ",".join(BOT_TELEMETRY_FIELDS.keys())
        url = (
            f"{env.org_url}/api/data/v{DATAVERSE_API_VERSION}/bots"
            f"?$select={fields_to_select}&$filter=statecode eq 0"
        )
        headers = self._headers(env.org_url)

        bots = []
        while url:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 404:
                LOG.debug("  No bots table in %s (env may not use Copilot Studio).", env.name)
                return []
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("value", []):
                bots.append(BotRecord(
                    bot_id         = item["botid"],
                    name           = item.get("name", ""),
                    environment_id = env.id,
                    current_fields = {k: item.get(k) for k in BOT_TELEMETRY_FIELDS},
                ))
            url = data.get("@odata.nextLink")

        return bots

    # ── patch ──

    def patch_bot(self, env: Environment, bot: BotRecord, payload: dict) -> bool:
        """PATCH a bot record. Returns True on success."""
        url = (
            f"{env.org_url}/api/data/v{DATAVERSE_API_VERSION}"
            f"/bots({bot.bot_id})"
        )
        headers = self._headers(env.org_url)
        resp = requests.patch(url, headers=headers, json=payload, timeout=30)
        if resp.status_code in (200, 204):
            return True
        LOG.error(
            "    PATCH failed for bot %s (%s): HTTP %s — %s",
            bot.name, bot.bot_id, resp.status_code, resp.text[:300]
        )
        return False


# ── Core logic ────────────────────────────────────────────────────────────────

def build_patch_payload(
    bot: BotRecord,
    connection_string: str,
    dry_run: bool,
) -> Optional[dict]:
    """
    Compares current bot field values against desired values.
    Returns None if already fully configured (skip), otherwise returns the
    minimal PATCH payload containing only changed fields.
    """
    desired = dict(BOT_TELEMETRY_FIELDS)
    desired["applicationinsightsconnectionstring"] = connection_string

    delta = {}
    for field_name, desired_value in desired.items():
        current = bot.current_fields.get(field_name)
        if current != desired_value:
            delta[field_name] = desired_value

    if not delta:
        return None   # already configured

    return delta


def process_tenant(
    client:            PowerPlatformClient,
    connection_string: str,
    env_filter:        list[str],
    dry_run:           bool,
    explicit_envs:     list[Environment] | None = None,
) -> list[PatchResult]:

    results: list[PatchResult] = []

    if explicit_envs:
        LOG.info("Using %d explicitly provided org URL(s) — skipping Power Platform Admin API.", len(explicit_envs))
        environments = explicit_envs
    else:
        environments = client.list_environments()

    # Apply optional name filter
    if env_filter:
        environments = [
            e for e in environments
            if any(f.lower() in e.name.lower() for f in env_filter)
        ]
        LOG.info("After filter: %d environments to process.", len(environments))

    for env in environments:
        LOG.info("── Environment: %s (%s)", env.name, env.id)

        try:
            bots = client.list_bots(env)
        except requests.HTTPError as exc:
            LOG.warning("  Could not list bots: %s", exc)
            continue

        if not bots:
            LOG.info("  No active bots found.")
            continue

        LOG.info("  Found %d bot(s).", len(bots))

        for bot in bots:
            payload = build_patch_payload(bot, connection_string, dry_run)

            if payload is None:
                LOG.info("  ✓  %-40s  already configured — skipped", bot.name)
                results.append(PatchResult(
                    bot_id=bot.bot_id, bot_name=bot.name,
                    env_name=env.name, success=True, skipped=True
                ))
                continue

            LOG.info("  →  %-40s  patching %d field(s): %s",
                     bot.name, len(payload), list(payload.keys()))

            if dry_run:
                LOG.info("     [DRY RUN] Would PATCH: %s", json.dumps(payload, indent=2))
                results.append(PatchResult(
                    bot_id=bot.bot_id, bot_name=bot.name,
                    env_name=env.name, success=True, skipped=True
                ))
                continue

            try:
                ok = client.patch_bot(env, bot, payload)
                results.append(PatchResult(
                    bot_id=bot.bot_id, bot_name=bot.name,
                    env_name=env.name, success=ok,
                    error="" if ok else "PATCH returned non-2xx"
                ))
                if ok:
                    LOG.info("     ✓ Done.")
                # Polite rate-limiting — PP API is ~100 req/min per SP
                time.sleep(0.3)
            except Exception as exc:  # noqa: BLE001
                LOG.error("  ✗  %s — exception: %s", bot.name, exc)
                results.append(PatchResult(
                    bot_id=bot.bot_id, bot_name=bot.name,
                    env_name=env.name, success=False, error=str(exc)
                ))

    return results


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(results: list[PatchResult], dry_run: bool) -> None:
    total    = len(results)
    patched  = sum(1 for r in results if r.success and not r.skipped)
    skipped  = sum(1 for r in results if r.skipped)
    failed   = sum(1 for r in results if not r.success)

    LOG.info("")
    LOG.info("═" * 60)
    LOG.info("  SUMMARY%s", "  [DRY RUN]" if dry_run else "")
    LOG.info("  Total bots examined : %d", total)
    LOG.info("  Patched             : %d", patched)
    LOG.info("  Already configured  : %d", skipped)
    LOG.info("  Failed              : %d", failed)
    LOG.info("═" * 60)

    if failed:
        LOG.warning("Failed bots:")
        for r in results:
            if not r.success:
                LOG.warning("  • %-40s  [%s]  %s", r.bot_name, r.env_name, r.error)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bulk-enable Application Insights telemetry on all Copilot Studio agents in a tenant."
    )
    parser.add_argument("--appid",   required=True,  help="Entra App (client) ID")
    parser.add_argument("--tenant",  required=True,  help="Entra Tenant ID")
    parser.add_argument("--secret",  required=True,  help="Client secret")
    parser.add_argument(
        "--appinsights-connection-string", required=True,
        dest="connection_string",
        help='App Insights connection string — e.g. "InstrumentationKey=xxx;IngestionEndpoint=https://..."'
    )
    parser.add_argument(
        "--env-filter", default="",
        help="Comma-separated substrings; only environments whose name matches are processed"
    )
    parser.add_argument(
        "--org-url", default="", dest="org_url",
        help=(
            "Bypass the Power Platform Admin API and target specific Dataverse orgs directly. "
            "Comma-separated list of org URLs, each optionally prefixed with a name: "
            "'My Env=https://org.crm.dynamics.com' or just 'https://org.crm.dynamics.com'. "
            "Use this when the SP does not have Power Platform Administrator role."
        )
    )
    parser.add_argument("--dry-run",  action="store_true", help="Read-only mode — no writes")
    parser.add_argument("--log-file", default="", help="Optional path to write a log file")
    return parser.parse_args()


def _parse_org_urls(raw: str) -> list[Environment]:
    """
    Parse --org-url value into Environment objects without calling the PP Admin API.
    Accepts comma-separated entries of the form:
      'Display Name=https://org.crm.dynamics.com'  or  'https://org.crm.dynamics.com'
    """
    envs = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            name, url = entry.split("=", 1)
            name = name.strip()
            url  = url.strip()
        else:
            url  = entry
            name = url  # use URL as display name
        envs.append(Environment(id=url, name=name, org_url=url.rstrip("/"), state="Ready"))
    return envs


def main() -> None:
    args = parse_args()

    # Reconfigure log-file now that we have the path
    if args.log_file:
        fh = logging.FileHandler(args.log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"
        ))
        LOG.addHandler(fh)

    LOG.info("Agent Telemetry Configurator — %s",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
    LOG.info("Tenant : %s", args.tenant)
    LOG.info("App ID : %s", args.appid)
    LOG.info("DRY RUN: %s", args.dry_run)
    LOG.info("")

    env_filter = [f.strip() for f in args.env_filter.split(",") if f.strip()]

    cache  = TokenCache(args.tenant, args.appid, args.secret)
    client = PowerPlatformClient(cache)

    explicit_envs = _parse_org_urls(args.org_url) if args.org_url else None

    results = process_tenant(
        client            = client,
        connection_string = args.connection_string,
        env_filter        = env_filter,
        dry_run           = args.dry_run,
        explicit_envs     = explicit_envs,
    )

    print_summary(results, args.dry_run)

    # Exit non-zero if any bots failed
    failed = sum(1 for r in results if not r.success)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
