"""What happens to an accepted Linear event once Linear has its 200.

Every session is acknowledged before anything slow runs, because Linear marks a
session unresponsive when no activity arrives within ten seconds.
"""

import logging
import uuid
from collections.abc import Mapping, Sequence

from langgraph_sdk import get_client
from langgraph_sdk.errors import ConflictError
from langgraph_sdk.schema import Run, Thread
from pydantic import BaseModel, JsonValue, ValidationError

from agent.integrations.base import Denied, PendingQuestion, SelectOption
from agent.integrations.linear.access import (
    RepoConfig,
    check_repo,
    choose_repo,
    interpret_answer,
    repo_from_choice,
    requester,
    resolve_actor,
)
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
from agent.utils.dashboard_links import dashboard_thread_url
from agent.webhooks import common

logger = logging.getLogger(__name__)

# Long enough to outlast the dispatch it guards; a Stop only matters before then.
STOP_MARKER_TTL_MINUTES = 60
# Thread metadata holding a question the requester has yet to answer.
PENDING_QUESTION_KEY = "linear_pending_question"


class _PendingOption(BaseModel):
    value: str
    label: str


class _PendingQuestion(BaseModel):
    session_id: str
    requester_id: str
    prompt: str
    request: str
    comment_id: str | None = None
    options: list[_PendingOption]

    def options_as_choices(self) -> tuple[SelectOption, ...]:
        return tuple(SelectOption(value=o.value, label=o.label) for o in self.options)


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
    await _run(event.session_id, thread_id, event.issue, event.creator, request, event.comment_id)


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
    pending = _pending_question(_metadata(await _thread(thread_id)), event.session_id)
    if pending is None:
        await _run(event.session_id, thread_id, event.issue, event.author, event.body, None)
        return
    if event.author.id != pending.requester_id:
        await post_activity(
            event.session_id,
            {"type": "thought", "body": "Waiting for the person who asked to choose."},
            ephemeral=True,
        )
        return
    choice = interpret_answer(pending.options_as_choices(), event.body)
    repo = repo_from_choice(choice) if choice is not None else None
    if repo is None:
        await _ask(event.session_id, pending.prompt, pending.options_as_choices())
        return
    await _set_pending_question(thread_id, None)
    await _run(
        event.session_id,
        thread_id,
        event.issue,
        event.author,
        pending.request,
        pending.comment_id,
        chosen_repo=repo,
    )


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


async def _run(
    session_id: str,
    thread_id: str,
    issue: LinearIssue,
    author: LinearUser | None,
    request: str,
    comment_id: str | None,
    *,
    chosen_repo: RepoConfig | None = None,
) -> None:
    """Authorize the requester, settle the repository, then dispatch on the issue thread."""
    user = await requester(author, issue)
    actor = await resolve_actor(user)
    if isinstance(actor, Denied):
        await post_activity(session_id, {"type": "error", "body": actor.message})
        return
    thread = await _thread(thread_id)
    repo = (
        await check_repo(chosen_repo, actor)
        if chosen_repo is not None
        else await choose_repo(request, actor, _metadata(thread), issue, session_id)
    )
    if isinstance(repo, Denied):
        await post_activity(session_id, {"type": "error", "body": repo.message})
        return
    if isinstance(repo, PendingQuestion):
        pending = _PendingQuestion(
            session_id=session_id,
            requester_id=repo.requester_id,
            prompt=repo.prompt,
            request=request,
            comment_id=comment_id,
            options=[_PendingOption(value=o.value, label=o.label) for o in repo.options],
        )
        await _set_pending_question(thread_id, pending)
        await _ask(session_id, repo.prompt, repo.options)
        return
    await _close_superseded_session(thread, session_id)
    issue_data: dict[str, object] = {
        "id": issue.id,
        "title": issue.title,
        "identifier": issue.identifier,
        "url": issue.url,
        "description": issue.description,
        "comment_author": user.model_dump() if user else {},
        "triggering_comment": request,
        "triggering_comment_id": comment_id or "",
    }
    await process_linear_issue(issue_data, repo, linear_session=LinearSessionRef(id=session_id))


async def _ask(session_id: str, prompt: str, options: Sequence[SelectOption]) -> None:
    """A select question; Linear shows the options and sends the pick back as a prompt."""
    choices: list[JsonValue] = [{"label": o.label, "value": o.value} for o in options]
    await post_activity(
        session_id,
        {"type": "elicitation", "body": prompt},
        signal="select",
        signal_metadata={"options": choices},
    )


def _pending_question(metadata: Mapping[str, object], session_id: str) -> _PendingQuestion | None:
    raw = metadata.get(PENDING_QUESTION_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        pending = _PendingQuestion.model_validate(raw)
    except ValidationError:
        logger.warning("Unreadable pending Linear question", exc_info=True)
        return None
    return pending if pending.session_id == session_id else None


async def _set_pending_question(thread_id: str, pending: _PendingQuestion | None) -> None:
    client = get_client()
    value = pending.model_dump(mode="json") if pending is not None else None
    # The issue's first request may ask before any run has created its thread.
    await client.threads.create(thread_id=thread_id, if_exists="do_nothing")
    await client.threads.update(thread_id=thread_id, metadata={PENDING_QUESTION_KEY: value})


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
