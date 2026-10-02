"""Turning a GitLab mention or assignment into a run, after GitLab has its 200."""

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from agent.input_messages import (
    PersonIdentity,
    RunInput,
    human_input,
    person_introduction,
    system_input,
    system_introduction,
)
from agent.integrations.gitlab.access import record_requester
from agent.integrations.gitlab.client import (
    DEVELOPER_ACCESS,
    GitLabAPIError,
    NoteSummary,
    access_level,
    award_emoji,
    bot_user,
    gitlab_host,
    post_note,
    public_email,
    recent_notes,
)
from agent.integrations.gitlab.events import GitLabEvent, GitLabUser, IssueAssigned, Mentioned
from agent.integrations.gitlab.merge_requests import marked_thread
from agent.prompts import prompt
from agent.source_context import GitLabRef, SourceContext
from agent.thread_ids import gitlab_issue_thread_id, gitlab_merge_request_thread_id
from agent.users import User
from agent.webhooks import common

logger = logging.getLogger(__name__)

DENIED_TEXT = (
    "Only members with the Developer role or higher on this project can ask Open SWE to work here."
)


def _log_extra(event: GitLabEvent) -> dict[str, object]:
    return {"gitlab_project_id": event.project.id, "gitlab_iid": event.iid}


async def process_gitlab_event(event: GitLabEvent) -> None:
    """Background task for one accepted delivery; never raises into the web server."""
    try:
        await _handle(event)
    except Exception:
        logger.exception("Handling a GitLab delivery failed", extra=_log_extra(event))


def _kind(event: GitLabEvent) -> str:
    return event.kind if isinstance(event, Mentioned) else "issue"


async def _post(event: GitLabEvent, body: str) -> None:
    kind = (
        "merge_request"
        if isinstance(event, Mentioned) and event.kind == "merge_request"
        else "issue"
    )
    discussion_id = event.discussion_id if isinstance(event, Mentioned) else ""
    await post_note(event.project.id, kind, event.iid, body, discussion_id=discussion_id)


async def _handle(event: GitLabEvent) -> None:
    level = await access_level(event.project.id, event.author.id)
    if level < DEVELOPER_ACCESS:
        logger.info(
            "Ignoring a GitLab request from a non-developer",
            extra={**_log_extra(event), "gitlab_access_level": level},
        )
        await _post(event, DENIED_TEXT)
        return
    await _acknowledge(event)
    thread_id = await _thread_for(event)
    await _dispatch(event, thread_id)


async def _acknowledge(event: GitLabEvent) -> None:
    try:
        if isinstance(event, Mentioned):
            await award_emoji(event.project.id, event.kind, event.iid, note_id=event.note_id)
        else:
            await award_emoji(event.project.id, "issue", event.iid, note_id=None)
    except GitLabAPIError, httpx.HTTPError:
        # The run still starts; the reaction only tells the person it was seen.
        logger.warning(
            "Reacting to a GitLab request failed", extra=_log_extra(event), exc_info=True
        )


async def _thread_for(event: GitLabEvent) -> str:
    host = gitlab_host()
    if isinstance(event, IssueAssigned) or event.kind == "issue":
        return gitlab_issue_thread_id(host, event.project.id, event.iid)
    # A merge request Open SWE opened continues the thread that opened it.
    opening = marked_thread(event.description)
    if opening is not None and await _thread_on_project(opening, event.project.id):
        return opening
    return gitlab_merge_request_thread_id(host, event.project.id, event.iid)


async def _thread_on_project(thread_id: str, project_id: int) -> bool:
    """Whether ``thread_id`` is a thread on this project; the marker is editable text."""
    metadata = await common.get_thread_metadata_safe(thread_id)
    if metadata is None:
        return False
    ref = SourceContext.from_metadata(metadata).gitlab
    return ref is not None and ref.project_id == project_id and ref.host == gitlab_host()


def _person(user: GitLabUser) -> PersonIdentity:
    person: PersonIdentity = {"id": f"gitlab:{user.username}"}
    if user.name:
        person["display_name"] = user.name
    return person


def _reference(event: GitLabEvent) -> str:
    return f"{'!' if _kind(event) == 'merge_request' else '#'}{event.iid}"


async def _earlier_notes(event: Mentioned) -> list[NoteSummary]:
    """People's notes before the mention, for a thread that starts now."""
    try:
        notes = await recent_notes(event.project.id, event.kind, event.iid)
    except GitLabAPIError, httpx.HTTPError:
        logger.warning("Reading GitLab notes failed", extra=_log_extra(event), exc_info=True)
        return []
    bot = await bot_user()
    return [note for note in notes if note.id != event.note_id and note.author.id != bot.id]


def _request_prompt(event: GitLabEvent, repository: str, *, new_thread: bool) -> str:
    merge_request = event.merge_request if isinstance(event, Mentioned) else None
    common_fields: dict[str, Any] = {
        "repository": repository,
        "project_url": event.project.web_url,
        "kind": "merge request" if _kind(event) == "merge_request" else "issue",
        "reference": _reference(event),
        "title": event.title,
        "url": event.url,
        "triggered_by": event.author.name or event.author.username,
    }
    if not new_thread:
        return prompt("runs/gitlab-reply", **common_fields)
    return prompt(
        "runs/gitlab-request",
        **common_fields,
        description=event.description or "No description",
        assigned=isinstance(event, IssueAssigned),
        source_branch=merge_request.source_branch if merge_request else "",
        target_branch=merge_request.target_branch if merge_request else "",
        same_project=merge_request is None
        or merge_request.source_project_id in (None, event.project.id),
    )


async def _requester_email(event: GitLabEvent) -> str:
    """The requester's public GitLab email, which matches their Open SWE user when it is theirs."""
    try:
        return await public_email(event.author.id)
    except GitLabAPIError, httpx.HTTPError:
        # The run still starts; only the dashboard listing for its requester is missing.
        logger.warning(
            "Reading a GitLab user's public email failed", extra=_log_extra(event), exc_info=True
        )
        return ""


async def _remember_requester(thread_id: str, email: str, event: GitLabEvent) -> None:
    """Let the requester's Open SWE account act on GitLab from this thread's dashboard view."""
    login = await User.login_for_email(email) if email else None
    if not login:
        return
    try:
        await record_requester(thread_id, login)
    except Exception:
        # The run still starts; only the requester's later dashboard follow-ups lose GitLab access.
        logger.warning(
            "Recording a GitLab requester failed", extra=_log_extra(event), exc_info=True
        )


async def _dispatch(event: GitLabEvent, thread_id: str) -> None:
    project = event.project
    repository = project.path_with_namespace
    new_thread = not await common.thread_exists(thread_id)
    ref = GitLabRef(
        host=gitlab_host(),
        project_id=project.id,
        project_path=repository,
        project_url=project.web_url,
        kind=_kind(event),
        iid=event.iid,
        url=event.url,
        discussion_id=event.discussion_id if isinstance(event, Mentioned) else "",
    )
    repo_config: dict[str, Any] = {
        "owner": project.namespace,
        "name": project.path,
        "host": "gitlab",
        "project_id": project.id,
    }
    workspace = await common.get_thread_workspace(
        thread_id
    ) or await common.workspace_for_repo_config(repo_config)
    configurable: dict[str, Any] = {
        "repo": repo_config,
        "source": "gitlab",
        "gitlab": ref.model_dump(mode="json"),
        "workspace": workspace,
        "environment": workspace,
    }
    email = await _requester_email(event)
    await common.upsert_agent_thread_metadata(
        thread_id,
        source="gitlab",
        repo_config=repo_config,
        title=f"{project.path}{_reference(event)}: {event.title}"[:80],
        # The thread outlives any one mention, so it keeps the item, not the discussion.
        source_context=SourceContext.parse({"gitlab": ref.model_dump(exclude={"discussion_id"})}),
        workspace=workspace,
        # Lists the thread for the requester's Open SWE account; the run still acts as the bot.
        user_email=email,
    )
    await _remember_requester(thread_id, email, event)
    run_input = await _run_input(event, repository, new_thread=new_thread)
    run = await common.dispatch_agent_run(
        thread_id,
        None,
        configurable,
        source="gitlab",
        thread_title=None,
        input=run_input,
        metadata=common.AGENT_VERSION_METADATA,
    )
    logger.info(
        "Dispatched a GitLab run",
        extra={
            **_log_extra(event),
            "thread_id": thread_id,
            "run_id": run.get("run_id") if isinstance(run, Mapping) else None,
        },
    )


async def _run_input(event: GitLabEvent, repository: str, *, new_thread: bool) -> RunInput:
    request = _request_prompt(event, repository, new_thread=new_thread)
    messages = [
        system_introduction(
            {"id": "system:gitlab", "display_name": "GitLab", "platform": "gitlab"}
        ),
        system_input(
            request,
            {
                "sender_id": "system:gitlab",
                "surface": "gitlab",
                "kind": "system",
                "data": {
                    "gitlab": {
                        "project": repository,
                        "reference": _reference(event),
                        "url": event.url,
                        "title": event.title,
                    }
                },
            },
        ),
    ]
    if not isinstance(event, Mentioned):
        return {"messages": messages}
    # The mention triggers the run, and the run describes its author itself.
    introduced: set[str] = {_person(event.author)["id"]}
    earlier = await _earlier_notes(event) if new_thread else []
    for note in [*earlier, None]:
        author = event.author if note is None else note.author
        person = _person(author)
        if person["id"] not in introduced:
            messages.append(person_introduction(person))
            introduced.add(person["id"])
        messages.append(
            human_input(
                event.note_body if note is None else note.body,
                {
                    "sender_id": person["id"],
                    "surface": "gitlab",
                    "kind": "human",
                    "data": {"note_id": str(event.note_id if note is None else note.id)},
                },
            )
        )
    return {"messages": messages}
