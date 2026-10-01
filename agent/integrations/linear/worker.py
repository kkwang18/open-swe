"""What happens to an accepted Linear event once Linear has its 200.

Every session is acknowledged before anything slow runs, because Linear marks a
session unresponsive when no activity arrives within ten seconds.
"""

import logging
import uuid
from collections.abc import Mapping

from langgraph_sdk import get_client
from langgraph_sdk.errors import ConflictError
from langgraph_sdk.schema import Run, Thread

from agent.integrations.linear.client import (
    app_user_id,
    linear_activity_id,
    post_activity,
    set_session_link,
)
from agent.integrations.linear.events import (
    DelegationRemoved,
    LinearEvent,
    LinearIssue,
    LinearUser,
    SessionCreated,
    SessionPrompted,
)
from agent.linear.webhook import process_linear_issue
from agent.source_context import LinearSessionRef, SourceContext
from agent.thread_ids import linear_issue_thread_id
from agent.threads.creation import create_lock_thread
from agent.threads.handlers import interrupt_transcript_turns
from agent.users import User
from agent.utils.dashboard_links import dashboard_thread_url
from agent.webhooks import common

logger = logging.getLogger(__name__)

# Long enough to outlast the dispatch it guards; a Stop only matters before then.
STOP_MARKER_TTL_MINUTES = 60

NO_ACCOUNT = (
    "I couldn't match your Linear account to an Open SWE user. Sign in to Open SWE "
    "with the email you use in Linear, then mention me again."
)
NO_REPO = (
    "I don't know which repository to work in. Add `repo:owner/name` to your message, "
    "or set a default repository in Open SWE."
)


def _stop_marker_id(session_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"open-swe:linear-stop:{session_id}"))


async def process_linear_event(event: LinearEvent) -> None:
    try:
        match event:
            case SessionCreated():
                await _start_session(event)
            case SessionPrompted():
                await _continue_session(event)
            case DelegationRemoved():
                await _stop_after_undelegation(event)
    except Exception:
        logger.exception(
            "Linear event processing failed",
            extra={"linear_event": type(event).__name__, "linear_delivery_id": event.delivery_id},
        )
        if not isinstance(event, DelegationRemoved):
            await _report_failure(event.session_id)


async def _report_failure(session_id: str) -> None:
    try:
        await post_activity(
            session_id,
            {"type": "error", "body": "Something went wrong starting this request."},
        )
    except Exception:
        logger.exception("Posting a Linear failure notice failed")


async def _start_session(event: SessionCreated) -> None:
    thread_id = linear_issue_thread_id(event.issue.id)
    await _acknowledge(event.session_id, event.delivery_id, thread_id)
    if await _is_stopped(event.session_id):
        await post_activity(event.session_id, {"type": "response", "body": "Stopped."})
        return
    request = event.comment_body if event.from_mention else ""
    await _dispatch(
        event.session_id, thread_id, event.issue, event.creator, request, event.comment_id
    )


async def _continue_session(event: SessionPrompted) -> None:
    if event.signal == "stop":
        await _mark_stopped(event.session_id)
        if event.issue is not None:
            await _cancel_session_runs(linear_issue_thread_id(event.issue.id), {event.session_id})
        await post_activity(event.session_id, {"type": "response", "body": "Stopped."})
        return
    if event.issue is None:
        logger.warning("Linear prompt has no issue", extra={"linear_session_id": event.session_id})
        return
    thread_id = linear_issue_thread_id(event.issue.id)
    await _acknowledge(event.session_id, event.delivery_id, thread_id)
    await _dispatch(event.session_id, thread_id, event.issue, event.author, event.body, None)


async def _acknowledge(session_id: str, delivery_id: str, thread_id: str) -> None:
    await post_activity(
        session_id,
        {"type": "thought", "body": "On it."},
        activity_id=linear_activity_id("ack", delivery_id),
        ephemeral=True,
    )
    if url := dashboard_thread_url(thread_id):
        try:
            await set_session_link(session_id, "Open SWE", url)
        except Exception:
            logger.exception("Linking the Open SWE thread to the Linear session failed")


async def _dispatch(
    session_id: str,
    thread_id: str,
    issue: LinearIssue,
    author: LinearUser | None,
    request: str,
    comment_id: str | None,
) -> None:
    login = await User.login_for_email(author.email) if author and author.email else None
    if not login:
        await post_activity(session_id, {"type": "error", "body": NO_ACCOUNT})
        return
    thread = await _thread(thread_id)
    repo = await _resolve_repo(request, login, thread)
    if repo is None:
        await post_activity(session_id, {"type": "error", "body": NO_REPO})
        return
    await _close_superseded_session(thread, session_id)
    issue_data: dict[str, object] = {
        "id": issue.id,
        "title": issue.title,
        "identifier": issue.identifier,
        "url": issue.url,
        "description": issue.description,
        "comment_author": author.model_dump() if author else {},
        "triggering_comment": request,
        "triggering_comment_id": comment_id or "",
    }
    await process_linear_issue(issue_data, repo, linear_session=LinearSessionRef(id=session_id))


async def _thread(thread_id: str) -> Thread | None:
    try:
        return await get_client().threads.get(thread_id)
    except Exception as exc:
        if common.is_not_found_error(exc):
            return None
        raise


def _metadata(thread: Thread | None) -> Mapping[str, object]:
    metadata = thread["metadata"] if thread is not None else None
    return metadata if isinstance(metadata, Mapping) else {}


async def _resolve_repo(request: str, login: str, thread: Thread | None) -> dict[str, str] | None:
    repo = (
        common.extract_repo_from_text(request, default_owner=common.DEFAULT_REPO_OWNER)
        or _thread_repo(_metadata(thread))
        or await common.get_profile_default_repo(login)
        or (await common.get_workspace_settings()).default_repo
    )
    if not repo or not common.is_repo_allowed(repo):
        return None
    return repo


def _thread_repo(metadata: Mapping[str, object]) -> dict[str, str] | None:
    repo = metadata.get("repo")
    if isinstance(repo, dict) and repo.get("owner") and repo.get("name"):
        return {"owner": str(repo["owner"]), "name": str(repo["name"])}
    return None


async def _close_superseded_session(thread: Thread | None, session_id: str) -> None:
    """One run per issue thread: a new session interrupts the running one, so say so there."""
    if thread is None or thread["status"] != "busy":
        return
    previous = SourceContext.from_metadata(_metadata(thread)).linear_session
    if previous is None or not previous.id or previous.id == session_id:
        return
    try:
        await post_activity(
            previous.id,
            {"type": "response", "body": "Continued in a newer request on this issue."},
        )
    except Exception:
        logger.exception("Closing the superseded Linear session failed")


async def _mark_stopped(session_id: str) -> None:
    try:
        await create_lock_thread(
            get_client(), _stop_marker_id(session_id), ttl_minutes=STOP_MARKER_TTL_MINUTES
        )
    except ConflictError:
        return


async def _is_stopped(session_id: str) -> bool:
    try:
        await get_client().threads.get(_stop_marker_id(session_id))
    except Exception as exc:
        if common.is_not_found_error(exc):
            return False
        raise
    return True


async def _stop_after_undelegation(event: DelegationRemoved) -> None:
    """Linear leaves sessions running when the app stops being the delegate; stop them here."""
    if event.previous_delegate_id != await app_user_id():
        return
    stopped = await _cancel_session_runs(linear_issue_thread_id(event.issue.id), None)
    for session_id in stopped:
        await post_activity(
            session_id,
            {"type": "response", "body": "Stopped: this issue is no longer delegated to me."},
        )


async def _cancel_session_runs(thread_id: str, session_ids: set[str] | None) -> set[str]:
    """Interrupt the thread's live runs for these sessions (any Linear session when ``None``).

    Returns the sessions whose runs were interrupted.
    """
    client = get_client()
    cancelled: dict[str, str] = {}
    for status in ("pending", "running"):
        for run in await client.runs.list(thread_id, status=status, limit=100):
            session_id = _run_session_id(run)
            if session_id and (session_ids is None or session_id in session_ids):
                cancelled[run["run_id"]] = session_id
    if cancelled:
        await client.runs.cancel_many(
            thread_id=thread_id, run_ids=sorted(cancelled), action="interrupt"
        )
        await interrupt_transcript_turns(thread_id, sorted(cancelled))
    return set(cancelled.values())


def _run_session_id(run: Run) -> str | None:
    kwargs = run.get("kwargs")
    config = kwargs.get("config") if isinstance(kwargs, Mapping) else None
    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    session = configurable.get("linear_session") if isinstance(configurable, Mapping) else None
    session_id = session.get("id") if isinstance(session, Mapping) else None
    return session_id if isinstance(session_id, str) and session_id else None
