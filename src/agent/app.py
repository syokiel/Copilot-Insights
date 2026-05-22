"""
Agent entry point — AIOHTTP server exposing:
  POST /api/messages   Bot Framework Activity endpoint (Copilot Studio calls this)
  GET  /health         Liveness probe

Run locally:
  python -m src.agent.app

The bot listens on BOT_PORT (default 3978).
Use Teams Toolkit Dev Tunnels or ngrok to expose it to Copilot Studio during development.
"""

import logging
import os
import sys
from pathlib import Path

from aiohttp import web
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.schema import Activity
from openai import AsyncAzureOpenAI

# Allow running as  python -m src.agent.app  from repo root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Load .env if present (local dev)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)
except ImportError:
    pass

from src.agent.bot import TelemetryBot

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def _build_app() -> web.Application:
    # Bot Framework adapter — leave app_id/password empty for local dev (no auth)
    adapter = BotFrameworkAdapter(
        BotFrameworkAdapterSettings(
            app_id=os.getenv("BOT_APP_ID", ""),
            app_password=os.getenv("BOT_APP_PASSWORD", ""),
        )
    )

    async def on_error(context, error):
        log.exception("Unhandled error in turn", exc_info=error)

    adapter.on_turn_error = on_error

    # Azure OpenAI client pointing at your Foundry project
    openai_client = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview"),
    )
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

    bot = TelemetryBot(openai_client, deployment)

    async def messages(req: web.Request) -> web.Response:
        if "application/json" not in req.content_type:
            return web.Response(status=415)
        body = await req.json()
        activity = Activity().deserialize(body)
        auth_header = req.headers.get("Authorization", "")
        invoke_response = await adapter.process_activity(activity, auth_header, bot.on_turn)
        if invoke_response:
            return web.json_response(data=invoke_response.body, status=invoke_response.status)
        return web.Response(status=201)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/health", health)
    return app


if __name__ == "__main__":
    port = int(os.getenv("BOT_PORT", "3978"))
    log.info("Starting TelemetryBot on port %d", port)
    web.run_app(_build_app(), host="localhost", port=port)
