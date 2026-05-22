"""
TelemetryBot — custom engine agent for Copilot Studio.

Conversation flow per turn:
  1. Receive the user's message via Bot Framework Activity.
  2. Fetch available MCP tools from the deployed SSE endpoint.
  3. Run an agentic loop: call Azure OpenAI → execute tool calls → repeat
     until the model returns a final text response.
  4. Send the response back as a Bot Framework Activity.

Conversation history is kept in-memory per conversation ID.
For multi-replica deployments, replace _history with a Redis or
Cosmos DB state provider.
"""

import json
import logging

from botbuilder.core import ActivityHandler, MessageFactory, TurnContext
from openai import AsyncAzureOpenAI

from src.agent.instructions import SYSTEM_PROMPT
from src.agent import mcp_tools

log = logging.getLogger(__name__)

# Max history turns kept per conversation (user+assistant pairs)
_MAX_HISTORY = 20


class TelemetryBot(ActivityHandler):
    def __init__(self, client: AsyncAzureOpenAI, deployment: str) -> None:
        self._client = client
        self._deployment = deployment
        self._history: dict[str, list[dict]] = {}

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        user_text = (turn_context.activity.text or "").strip()
        if not user_text:
            return

        conv_id = turn_context.activity.conversation.id
        history = self._history.setdefault(conv_id, [])
        history.append({"role": "user", "content": user_text})

        await turn_context.send_activity(MessageFactory.text("_Thinking…_"))

        try:
            reply = await self._run(history)
        except Exception as exc:
            log.exception("Agent error")
            reply = f"Sorry, I ran into an error: {exc}"

        history.append({"role": "assistant", "content": reply})
        if len(history) > _MAX_HISTORY * 2:
            self._history[conv_id] = history[-(  _MAX_HISTORY * 2):]

        await turn_context.send_activity(MessageFactory.text(reply))

    async def _run(self, history: list[dict]) -> str:
        tools = await mcp_tools.list_tools()
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}] + history

        # Agentic loop — keep going until no more tool calls
        while True:
            response = await self._client.chat.completions.create(
                model=self._deployment,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            msg = response.choices[0].message

            if not msg.tool_calls:
                return msg.content or ""

            # Append assistant turn with tool calls
            messages.append(msg.model_dump(exclude_none=True))

            # Execute each tool call in sequence
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments or "{}")
                log.info("tool call: %s %s", fn_name, fn_args)
                try:
                    result = await mcp_tools.call_tool(fn_name, fn_args)
                except Exception as exc:
                    result = f"Tool error: {exc}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
