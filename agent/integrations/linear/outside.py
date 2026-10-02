"""Linear sessions for work on a Linear issue's thread that someone starts outside Linear.

A comment on the issue's pull request, or a message in the Open SWE dashboard,
continues the issue's thread; the run then reports to a session of its own on
the issue, so the issue shows that work too.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Mapping

from langgraph_sdk.client import LangGraphClient
from langgraph_sdk.errors import ConflictError, NotFoundError
from pydantic import JsonValue

from agent.integrations.linear.client import create_session_on_issue, post_activity
from agent.integrations.linear.session import running_sessions
from agent.integrations.linear.token import linear_app_configured
from agent.source_context import LinearSessionRef, SourceContext
from agent.threads.creation import create_lock_thread
from agent.utils.dashboard_links import dashboard_thread_url

logger = logging.getLogger(__name__)

# Where a person can continue a Linear issue's thread outside Linear; internal runs
# such as wakeups and background tasks get no session.
_ORIGINS: Mapping[str, str] = {"github": "GitHub", "web": "Open SWE", "dashboard": "Open SWE"}
# Linear announces a session the app opened like one an automation started, with no
# creator. These markers tell the worker which session is the app's own.
OWN_SESSION_MARKER_TTL_MINUTES = 10
CREATING_MARKER_TTL_MINUTES = 2
# How long the worker waits for an in-flight creation to name its session.
OWN_SESSION_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.25


def _own_session_marker_id(session_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"open-swe:linear-own-session:{session_id}"))


def _creating_marker_id(issue_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"open-swe:linear-creating-session:{issue_id}"))


async def open_session_for_outside_run(
    client: LangGraphClient,
    thread_id: str,
    configurable: dict[str, object],
    *,
    source: str,
    interrupts: bool,
) -> tuple[str, ...]:
    """Point the run at a new session on the thread's Linear issue; never blocks the run.

    Returns the sessions of the runs it will interrupt, for the caller to close once
    the run is dispatched: a run that fails to start interrupts nothing.
    """
    origin = _ORIGINS.get(source)
    if origin is None or configurable.get("linear_session") or not linear_app_configured():
        return ()
    try:
        try:
            thread = await client.threads.get(thread_id)
        except NotFoundError:
            return ()
        issue = SourceContext.from_metadata(thread["metadata"]).linear_issue
        if issue is None or not issue.id:
            return ()
        superseded = await running_sessions(client, thread_id) if interrupts else ()
        session_id = await _create_own_session(client, issue.id, thread_id)
        content: dict[str, JsonValue] = {"type": "thought", "body": f"Continuing from {origin}."}
        await post_activity(session_id, content)
    except Exception:
        logger.exception(
            "Opening a Linear session for a run started outside Linear failed",
            extra={"thread_id": thread_id, "run_source": source},
        )
        return ()
    configurable["linear_session"] = LinearSessionRef(id=session_id).model_dump(mode="json")
    return superseded


async def _create_own_session(client: LangGraphClient, issue_id: str, thread_id: str) -> str:
    creating = _creating_marker_id(issue_id)
    try:
        await create_lock_thread(client, creating, ttl_minutes=CREATING_MARKER_TTL_MINUTES)
    except ConflictError:
        pass  # Another outside run is opening a session on this issue too.
    try:
        session_id = await create_session_on_issue(
            issue_id, "Open SWE", dashboard_thread_url(thread_id)
        )
        try:
            await create_lock_thread(
                client,
                _own_session_marker_id(session_id),
                ttl_minutes=OWN_SESSION_MARKER_TTL_MINUTES,
            )
        except ConflictError:
            pass  # Already marked: a repeat of this session's creation.
    finally:
        try:
            await client.threads.delete(creating)
        except NotFoundError:
            pass  # Expired, or another outside run's creation cleared it first.
    return session_id


async def is_own_session(client: LangGraphClient, issue_id: str, session_id: str) -> bool:
    """Whether the app itself opened this session, for a run started outside Linear.

    Linear can announce the session before the app hears back that it exists; while
    a creation on the issue is in flight, wait briefly for it to name its session.
    """
    deadline = time.monotonic() + OWN_SESSION_WAIT_SECONDS
    while True:
        if await _exists(client, _own_session_marker_id(session_id)):
            return True
        if not await _exists(client, _creating_marker_id(issue_id)):
            return False
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_POLL_SECONDS)


async def _exists(client: LangGraphClient, thread_id: str) -> bool:
    try:
        await client.threads.get(thread_id)
    except NotFoundError:
        return False
    return True
