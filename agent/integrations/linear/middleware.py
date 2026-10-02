"""Mirror a run started from a Linear agent session into that session."""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping

from langchain.agents.middleware.types import AgentState
from langchain_core.messages import ToolMessage
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import JsonValue

from agent.integrations.linear.client import add_session_link, post_activity, set_session_plan
from agent.integrations.linear.session import final_answer, post_final_response
from agent.middleware.message_content import content_to_text
from agent.middleware.trace import OpenSWEMiddleware

logger = logging.getLogger(__name__)

# Bookkeeping the person watching the session gains nothing from.
_QUIET_TOOLS = frozenset({"write_todos", "load_integration_tools"})
_PARAMETER_KEYS = ("command", "file_path", "path", "pattern", "query", "url", "description")
_PARAMETER_LIMIT = 200
_PLAN_STATUSES = {"pending": "pending", "in_progress": "inProgress", "completed": "completed"}
# Commands start in the repository with `cd <dir> &&`; Linear truncates the line, so the
# prefix would hide the command itself.
_CD_PREFIX = re.compile(r"^cd\s+\S+\s*&&\s*")


def _parameter(args: Mapping[str, object]) -> str:
    for key in _PARAMETER_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            shown = _CD_PREFIX.sub("", value.strip()) if key == "command" else value.strip()
            return (shown or value.strip())[:_PARAMETER_LIMIT]
    return json.dumps(args, default=str)[:_PARAMETER_LIMIT]


def _plan(args: Mapping[str, object]) -> list[JsonValue]:
    todos = args.get("todos")
    if not isinstance(todos, list):
        return []
    plan: list[JsonValue] = []
    for todo in todos:
        if isinstance(todo, Mapping) and isinstance(todo.get("content"), str):
            status = _PLAN_STATUSES.get(str(todo.get("status")), "pending")
            plan.append({"content": todo["content"], "status": status})
    return plan


def _opened_pull_request(result: ToolMessage | Command) -> tuple[str, str] | None:
    """``(label, url)`` of the PR an ``open_pull_request`` call opened or found."""
    if not isinstance(result, ToolMessage):
        return None
    try:
        payload = json.loads(content_to_text(result.content))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    url, number = payload.get("url"), payload.get("number")
    if not isinstance(url, str) or not url:
        return None
    return (f"Pull request #{number}" if isinstance(number, int) else "Pull request"), url


def _run_ids() -> tuple[str, str] | None:
    config = get_config()
    configurable = config.get("configurable") or {}
    thread_id = configurable.get("thread_id")
    run_id = config.get("run_id") or configurable.get("run_id")
    if isinstance(thread_id, str) and thread_id and run_id:
        return thread_id, str(run_id)
    return None


class LinearSessionMiddleware(OpenSWEMiddleware):
    """Tool calls become actions, todos become the plan, and the answer closes the session.

    Progress posts run in order in the background so the agent never waits on
    Linear; the closing response waits for them so it lands last.
    """

    state_schema = AgentState

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self._session_id = session_id
        self._last_post: asyncio.Task[None] | None = None

    def _enqueue(self, post: Callable[[], Awaitable[None]]) -> None:
        previous = self._last_post

        async def run_after_previous() -> None:
            if previous is not None:
                await previous
            try:
                await post()
            except Exception:
                logger.warning(
                    "Posting Linear session progress failed",
                    extra={"linear_session_id": self._session_id},
                    exc_info=True,
                )

        self._last_post = asyncio.create_task(run_after_previous())

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        name = request.tool_call["name"]
        args = request.tool_call.get("args") or {}
        if name == "write_todos":
            plan = _plan(args)
            self._enqueue(lambda: set_session_plan(self._session_id, plan))
        elif name not in _QUIET_TOOLS:
            content: dict[str, JsonValue] = {
                "type": "action",
                "action": name,
                "parameter": _parameter(args),
            }
            self._enqueue(lambda: post_activity(self._session_id, content))
        result = await handler(request)
        if name == "open_pull_request" and (pr := _opened_pull_request(result)) is not None:
            label, url = pr
            self._enqueue(lambda: add_session_link(self._session_id, label, url))
        return result

    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> None:
        del runtime
        if self._last_post is not None:
            await self._last_post
        ids = _run_ids()
        if ids is None:
            return
        thread_id, run_id = ids
        await post_final_response(
            self._session_id, run_id, thread_id, final_answer(state["messages"])
        )
