"""Linear sessions for work on a Linear issue's thread that someone starts outside Linear.

A comment on the issue's pull request, or a message in the Open SWE dashboard,
continues the issue's thread; the run then reports to a session of its own on
the issue, so the issue shows that work too.
"""

import logging
import uuid
from collections.abc import Mapping

from langgraph_sdk.client import LangGraphClient
from langgraph_sdk.errors import ConflictError, NotFoundError
from pydantic import JsonValue

from agent.integrations.linear.client import create_session_on_issue, post_activity
from agent.integrations.linear.token import linear_app_configured
from agent.source_context import LinearSessionRef, SourceContext
from agent.threads.creation import create_lock_thread
from agent.utils.dashboard_links import dashboard_thread_url

logger = logging.getLogger(__name__)

# Where a person can continue a Linear issue's thread outside Linear; internal runs
# such as wakeups and background tasks get no session.
_ORIGINS: Mapping[str, str] = {"github": "GitHub", "web": "Open SWE", "dashboard": "Open SWE"}
# Long enough for Linear to announce the session the app opened.
OWN_SESSION_MARKER_TTL_MINUTES = 5


def _own_session_marker_id(issue_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"open-swe:linear-own-session:{issue_id}"))


async def open_session_for_outside_run(
    client: LangGraphClient, thread_id: str, configurable: dict[str, object], *, source: str
) -> None:
    """Point the run at a new session on the thread's Linear issue; never blocks the run."""
    origin = _ORIGINS.get(source)
    if origin is None or configurable.get("linear_session") or not linear_app_configured():
        return
    try:
        try:
            thread = await client.threads.get(thread_id)
        except NotFoundError:
            return
        issue = SourceContext.from_metadata(thread["metadata"]).linear_issue
        if issue is None or not issue.id:
            return
        await _mark_own_session(client, issue.id)
        session_id = await create_session_on_issue(
            issue.id, "Open SWE", dashboard_thread_url(thread_id)
        )
        content: dict[str, JsonValue] = {"type": "thought", "body": f"Continuing from {origin}."}
        await post_activity(session_id, content)
    except Exception:
        logger.exception(
            "Opening a Linear session for a run started outside Linear failed",
            extra={"thread_id": thread_id, "run_source": source},
        )
        return
    configurable["linear_session"] = LinearSessionRef(id=session_id).model_dump(mode="json")


async def _mark_own_session(client: LangGraphClient, issue_id: str) -> None:
    try:
        await create_lock_thread(
            client, _own_session_marker_id(issue_id), ttl_minutes=OWN_SESSION_MARKER_TTL_MINUTES
        )
    except ConflictError:
        return


async def is_own_session(client: LangGraphClient, issue_id: str) -> bool:
    """Whether the app itself just opened a session on this issue.

    Linear announces such a session like one an automation started, with no creator.
    """
    try:
        await client.threads.get(_own_session_marker_id(issue_id))
    except NotFoundError:
        return False
    return True
