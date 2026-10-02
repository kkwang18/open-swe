"""What happens to an accepted Linear event once Linear has its 200.

Every session is acknowledged before anything slow runs, because Linear marks a
session unresponsive when no activity arrives within ten seconds.
"""

import logging
import uuid
from collections.abc import Mapping
from typing import Literal

from langgraph_sdk import get_client
from langgraph_sdk.errors import ConflictError
from langgraph_sdk.schema import Thread
from pydantic import BaseModel, Field, JsonValue, ValidationError

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
    start_delegated_issue,
)
from agent.integrations.linear.events import (
    DelegationRemoved,
    LinearEvent,
    LinearIssue,
    LinearUser,
    SessionCreated,
    SessionPrompted,
)
from agent.integrations.linear.session import run_session, run_session_id
from agent.linear.webhook import process_linear_issue
from agent.source_context import LinearSessionRef
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
    """A question the requester must answer, saved with everything needed to resume."""

    kind: Literal["link_account", "select_repo"] = "select_repo"
    session_id: str
    requester_id: str
    prompt: str
    request: str
    comment_id: str | None = None
    issue: LinearIssue
    author: LinearUser | None = None
    options: list[_PendingOption] = Field(default_factory=list)
    link_url: str | None = None
    prompt_context: str = ""
    guidance: str = ""

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
    await _run(
        event.session_id,
        thread_id,
        event.issue,
        event.creator,
        request,
        event.comment_id,
        prompt_context=event.prompt_context,
        guidance=event.guidance,
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
    metadata = _metadata(await _thread(thread_id))
    pending = _pending_question(metadata, event.session_id)
    if pending is None:
        await _run(
            event.session_id,
            thread_id,
            event.issue,
            event.author,
            event.body,
            None,
            guidance=event.guidance or await _session_guidance(thread_id, event.session_id),
        )
        return
    if event.author.id != pending.requester_id:
        await post_activity(
            event.session_id,
            {"type": "thought", "body": "Waiting for the person who asked."},
            ephemeral=True,
        )
        return
    if pending.kind == "link_account":
        # They may have linked already; if not, this asks again.
        await _set_pending_question(thread_id, None)
        await _run(
            event.session_id,
            thread_id,
            pending.issue,
            pending.author,
            pending.request,
            pending.comment_id,
            prompt_context=pending.prompt_context,
            guidance=pending.guidance,
        )
        return
    choice = interpret_answer(pending.options_as_choices(), event.body)
    repo = repo_from_choice(choice) if choice is not None else None
    if repo is None:
        await _ask(pending)
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
        prompt_context=pending.prompt_context,
        guidance=pending.guidance,
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
    prompt_context: str = "",
    guidance: str = "",
) -> None:
    """Authorize the requester, settle the repository, then dispatch on the issue thread."""
    user = await requester(author, issue)
    actor = await resolve_actor(user, session_id, issue)
    if isinstance(actor, Denied):
        await post_activity(session_id, {"type": "error", "body": actor.message})
        return
    if isinstance(actor, PendingQuestion):
        await _save_and_ask(
            actor, session_id, thread_id, issue, user, request, comment_id, prompt_context, guidance
        )
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
        await _save_and_ask(
            repo, session_id, thread_id, issue, user, request, comment_id, prompt_context, guidance
        )
        return
    await _close_superseded_sessions(thread_id, session_id)
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
    await process_linear_issue(
        issue_data,
        repo,
        linear_session=LinearSessionRef(id=session_id, guidance=guidance),
        github_login=actor.github_login,
        prompt_context=prompt_context,
    )
    # Linear leaves issues an automation delegated in triage for a person to pick up.
    if author is not None:
        await _start_issue(issue.id)


async def _start_issue(issue_id: str) -> None:
    """Show the issue as in progress once work begins, as Linear asks of agents."""
    try:
        await start_delegated_issue(issue_id)
    except Exception:
        logger.exception(
            "Moving the Linear issue to started failed", extra={"linear_issue_id": issue_id}
        )


async def _save_and_ask(
    question: PendingQuestion,
    session_id: str,
    thread_id: str,
    issue: LinearIssue,
    author: LinearUser | None,
    request: str,
    comment_id: str | None,
    prompt_context: str,
    guidance: str,
) -> None:
    pending = _PendingQuestion(
        kind=question.kind,
        session_id=session_id,
        requester_id=question.requester_id,
        prompt=question.prompt,
        request=request,
        comment_id=comment_id,
        issue=issue,
        author=author,
        options=[_PendingOption(value=o.value, label=o.label) for o in question.options],
        link_url=question.link_url,
        prompt_context=prompt_context,
        guidance=guidance,
    )
    await _set_pending_question(thread_id, pending)
    await _ask(pending)


async def resume_after_link(session_id: str, issue_id: str, linear_user_id: str) -> None:
    """Pick the request back up once its requester has linked their Linear account."""
    thread_id = linear_issue_thread_id(issue_id)
    try:
        pending = _pending_question(_metadata(await _thread(thread_id)), session_id)
        if pending is None or pending.kind != "link_account":
            return
        if pending.requester_id != linear_user_id:
            logger.info(
                "Linear account linked by someone other than the requester",
                extra={"linear_session_id": session_id},
            )
            return
        await _set_pending_question(thread_id, None)
        await post_activity(
            session_id, {"type": "thought", "body": "Linked. Picking up your request."}
        )
        await _run(
            session_id,
            thread_id,
            pending.issue,
            pending.author,
            pending.request,
            pending.comment_id,
            prompt_context=pending.prompt_context,
            guidance=pending.guidance,
        )
    except Exception:
        logger.exception("Resuming a Linear request after linking failed")
        await _report_failure(session_id)


async def _ask(pending: _PendingQuestion) -> None:
    """Linear renders a select question as options and a link question as a button."""
    if pending.kind == "link_account" and pending.link_url:
        await post_activity(
            pending.session_id,
            {"type": "elicitation", "body": pending.prompt},
            signal="auth",
            signal_metadata={
                "url": pending.link_url,
                "userId": pending.requester_id,
                "providerName": "Open SWE",
            },
        )
        return
    choices: list[JsonValue] = [{"label": o.label, "value": o.value} for o in pending.options]
    await post_activity(
        pending.session_id,
        {"type": "elicitation", "body": pending.prompt},
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


async def _session_guidance(thread_id: str, session_id: str) -> str:
    """The guidance Linear sent when this session started, kept on the session's latest run."""
    for run in await get_client().runs.list(thread_id, limit=50):
        session = run_session(run)
        if session is not None and session.id == session_id:
            return session.guidance
    return ""


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


async def _close_superseded_sessions(thread_id: str, session_id: str) -> None:
    """One run per issue thread: a new session interrupts the running ones, so say so there."""
    client = get_client()
    previous: set[str] = set()
    for status in ("pending", "running"):
        for run in await client.runs.list(thread_id, status=status, limit=100):
            other = run_session_id(run)
            if other and other != session_id:
                previous.add(other)
    try:
        for other in sorted(previous):
            await post_activity(
                other,
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
            session_id = run_session_id(run)
            if session_id and (session_ids is None or session_id in session_ids):
                cancelled[run["run_id"]] = session_id
    if cancelled:
        await client.runs.cancel_many(
            thread_id=thread_id, run_ids=sorted(cancelled), action="interrupt"
        )
        await interrupt_transcript_turns(thread_id, sorted(cancelled))
    return set(cancelled.values())
