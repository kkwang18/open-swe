"""Which runs on a GitLab project may act on GitLab with the bot's token.

GitLab checks nothing when the bot pushes, comments or opens a merge request:
the bot's memberships are all that limit it. So the person asking is checked
instead, against their own GitLab role on the project:

- A run started from GitLab passed that check moments ago, when its comment
  arrived.
- Any other run (dashboard, Slack, Linear) needs a sender whose linked GitLab
  account has Developer access or more on the project now. Someone who asked
  from GitLab on the same issue or merge request before linking still counts,
  as they did in phase 1.

Others can still read the thread and talk to it, but not act on GitLab.
"""

import logging
import time
from collections.abc import Mapping
from typing import Literal

import httpx
from langgraph_sdk import get_client
from langgraph_sdk.errors import NotFoundError

from agent.integrations.gitlab.client import (
    DEVELOPER_ACCESS,
    GitLabAPIError,
    access_level,
    get_project,
    link_url,
)
from agent.integrations.gitlab.project import run_gitlab_project
from agent.prompts import prompt
from agent.run_config import RunConfig
from agent.source_context import GitLabRef
from agent.users import User
from agent.utils.thread_participants import merge_participants, participant_logins

logger = logging.getLogger(__name__)

# Open SWE logins that asked from GitLab on this thread and had Developer access then.
REQUESTERS_KEY = "gitlab_requester_logins"
# One run asks several times (prompt, tools, sandbox); the role changes rarely.
_ROLE_TTL_SECONDS = 60.0

GitLabAccess = Literal["allowed", "no_sender", "unlinked", "no_role", "unavailable"]

_roles: dict[tuple[int, int], tuple[int, float]] = {}


def _metadata(thread: Mapping[str, object]) -> Mapping[str, object]:
    metadata = thread.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


async def record_requester(thread_id: str, login: str) -> None:
    """Remember that ``login`` asked from GitLab here, with the access the request needed."""
    client = get_client()
    thread = await client.threads.get(thread_id)
    existing = _metadata(thread).get(REQUESTERS_KEY)
    await client.threads.update(
        thread_id=thread_id, metadata={REQUESTERS_KEY: merge_participants(existing, login)}
    )


async def _role(project_id: int, gitlab_user_id: int) -> int:
    key = (project_id, gitlab_user_id)
    cached = _roles.get(key)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]
    level = await access_level(project_id, gitlab_user_id)
    _roles[key] = (level, time.monotonic() + _ROLE_TTL_SECONDS)
    return level


async def person_access(login: str, project: GitLabRef) -> GitLabAccess:
    """Whether the Open SWE person ``login`` may have the bot work on ``project``."""
    user = await User.for_login("github", login)
    gitlab_user_id = user.gitlab_user_id if user is not None else ""
    if not gitlab_user_id.isdigit():
        return "unlinked"
    try:
        project_id = project.project_id or (await get_project(project.project_path)).id
        level = await _role(project_id, int(gitlab_user_id))
    except GitLabAPIError, httpx.HTTPError:
        logger.warning(
            "Checking a person's GitLab role failed",
            extra={"gitlab_project": project.project_path},
            exc_info=True,
        )
        return "unavailable"
    return "allowed" if level >= DEVELOPER_ACCESS else "no_role"


async def _asked_from_gitlab(thread_id: str, login: str) -> bool:
    try:
        thread = await get_client().threads.get(thread_id)
    except NotFoundError:
        return False
    except Exception:
        logger.warning(
            "Reading GitLab requesters failed; not counting them",
            extra={"thread_id": thread_id},
            exc_info=True,
        )
        return False
    return login in participant_logins(_metadata(thread).get(REQUESTERS_KEY))


async def run_gitlab_access(cfg: RunConfig) -> GitLabAccess | None:
    """Whether this run may act on its GitLab project, and why not; ``None`` off GitLab."""
    project = run_gitlab_project(cfg)
    if project is None or not cfg.thread_id:
        return None
    login = (cfg.github_login or "").strip().lower()
    if not login:
        # Only GitLab-started runs name no sender; any other run without one is refused.
        return "allowed" if cfg.source == "gitlab" else "no_sender"
    verdict = await person_access(login, project)
    if verdict != "allowed" and await _asked_from_gitlab(cfg.thread_id, login):
        return "allowed"
    return verdict


async def gitlab_access_allowed(cfg: RunConfig) -> bool:
    """Whether this run may use the bot's GitLab access; ``False`` for non-GitLab runs."""
    return await run_gitlab_access(cfg) == "allowed"


def access_denied_text(verdict: GitLabAccess, project: GitLabRef) -> str:
    """What the agent tells the person when the run may not act on GitLab."""
    return prompt(
        "system/gitlab-access-denied",
        reason=verdict,
        project=project.project_path,
        link_url=link_url(),
    ).strip()


async def gitlab_access_refusal(cfg: RunConfig) -> str | None:
    """Why this run may not act on its GitLab project, or ``None`` when it may."""
    verdict = await run_gitlab_access(cfg)
    project = run_gitlab_project(cfg)
    if verdict == "allowed" or project is None:
        return None
    return access_denied_text(verdict or "no_sender", project)


async def gitlab_access_note(cfg: RunConfig) -> str:
    """The run's no-access explanation for its prompt, or ``""`` when it may act."""
    verdict = await run_gitlab_access(cfg)
    project = run_gitlab_project(cfg)
    if verdict is None or verdict == "allowed" or project is None:
        return ""
    return access_denied_text(verdict, project)
