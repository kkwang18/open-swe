"""Who a Linear request runs as, and in which repository.

A run acts for the person behind the event, never the issue's assignee, and only
in a repository that person can reach on GitHub themselves.
"""

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence

from fastapi import HTTPException

from agent.dashboard.repo_access import require_repo_access_for_user
from agent.integrations.base import Actor, Denied, PendingQuestion, SelectOption
from agent.integrations.gitlab.client import link_url as gitlab_link_url
from agent.integrations.gitlab.refs import gitlab_repo_config, is_gitlab_repo, repo_full_name
from agent.integrations.linear.client import is_guest, issue_creator, suggest_repositories
from agent.integrations.linear.events import LinearIssue, LinearUser
from agent.integrations.linear.link import link_url
from agent.users import User
from agent.users.authorization import is_authorized_github_login
from agent.webhooks import common
from agent.workspaces.routing import repo_is_routable
from agent.workspaces.store import load_workspace

logger = logging.getLogger(__name__)

RepoConfig = dict[str, str]

NO_REQUESTER = "I couldn't tell who asked for this, so I can't run it on anyone's behalf."
LINK_QUESTION = (
    "Link your Linear account to Open SWE so I can work as you. I'll pick this up as "
    "soon as you're linked."
)
NOT_ALLOWED = "Your account isn't allowed to use Open SWE. Ask an Open SWE admin for access."
GUEST = "Linear guests can't ask me to work. Ask a member of the team to delegate the issue."
NO_REPO = (
    "I don't know which repository to work in. Add `repo:owner/name` to your message, "
    "or set a default repository in Open SWE."
)
REPO_QUESTION = "Which repository should I work in?"

SUGGESTION_TIMEOUT_SECONDS = 2.0
SUGGESTION_CONFIDENCE = 0.75
MAX_OPTIONS = 10


async def requester(author: LinearUser | None, issue: LinearIssue) -> LinearUser | None:
    """The person behind the event; for an automation, whoever created the issue."""
    return author if author is not None else await issue_creator(issue.id)


async def resolve_actor(
    user: LinearUser | None, session_id: str, issue: LinearIssue
) -> Actor | PendingQuestion | Denied:
    """The linked Open SWE person behind a Linear user; never matched by email."""
    if user is None:
        return Denied(NO_REQUESTER)
    if await is_guest(user.id):
        return Denied(GUEST)
    person = await User.for_identity("linear", user.id)
    login = person.github_login if person is not None else ""
    if not login:
        return PendingQuestion(
            kind="link_account",
            prompt=LINK_QUESTION,
            requester_id=user.id,
            link_url=link_url(session_id, issue.id),
        )
    if not await is_authorized_github_login(login):
        return Denied(NOT_ALLOWED)
    return Actor(provider_user_id=user.id, github_login=login, email=user.email or None)


def _full_name(repo: RepoConfig) -> str:
    return repo_full_name(repo)


def _parse_full_name(full_name: str) -> RepoConfig | None:
    if (gitlab := gitlab_repo_config(full_name)) is not None:
        return gitlab
    owner, _, name = full_name.strip().partition("/")
    return {"owner": owner, "name": name} if owner and name and "/" not in name else None


def _thread_repo(metadata: Mapping[str, object]) -> RepoConfig | None:
    repo = metadata.get("repo")
    if isinstance(repo, Mapping) and repo.get("owner") and repo.get("name"):
        config = {"owner": str(repo["owner"]), "name": str(repo["name"])}
        if is_gitlab_repo(repo):
            # The thread works on a GitLab project, not a GitHub namesake.
            config["host"] = "gitlab"
        return config
    return None


async def check_repo(repo: RepoConfig, actor: Actor) -> RepoConfig | Denied:
    """Allowlisted, routed to a workspace, and reachable with the person's own GitHub access."""
    full_name = _full_name(repo)
    gitlab = is_gitlab_repo(repo)
    # The GitHub allowlists name GitHub owners; a GitLab project is bounded by the bot's memberships.
    if not gitlab and not common.is_repo_allowed(repo):
        return Denied(f"`{full_name}` isn't one of the repositories Open SWE may work in.")
    owner, _, name = full_name.rpartition("/")
    if not await repo_is_routable(owner, name):
        return Denied(f"`{full_name}` isn't routed to an Open SWE workspace.")
    try:
        await require_repo_access_for_user(actor.github_login, full_name)
    except HTTPException as exc:
        if gitlab and exc.status_code in (403, 503):
            return Denied(gitlab_denial(exc.status_code, full_name))
        if exc.status_code == 401:
            return Denied(
                f"Sign in to Open SWE again so I can check your GitHub access to `{full_name}`."
            )
        if exc.status_code in (403, 404):
            return Denied(f"You don't have access to `{full_name}` on GitHub.")
        raise
    return repo


async def _candidates(workspace: str | None) -> list[str]:
    record = await load_workspace(workspace)
    repos = record.repos if record is not None else []
    return [
        full_name
        for full_name in dict.fromkeys(repos)
        if (repo := _parse_full_name(full_name)) is not None and common.is_repo_allowed(repo)
    ]


async def _suggested(issue_id: str, session_id: str, candidates: list[str]) -> str | None:
    try:
        ranked = await asyncio.wait_for(
            suggest_repositories(issue_id, session_id, candidates),
            timeout=SUGGESTION_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("Linear repository suggestion failed", exc_info=True)
        return None
    if ranked and ranked[0][1] >= SUGGESTION_CONFIDENCE and ranked[0][0] in candidates:
        return ranked[0][0]
    return None


async def choose_repo(
    request: str,
    actor: Actor,
    thread_metadata: Mapping[str, object],
    issue: LinearIssue,
    session_id: str,
) -> RepoConfig | PendingQuestion | Denied:
    repo = (
        common.extract_repo_from_text(request, default_owner=common.DEFAULT_REPO_OWNER)
        or _thread_repo(thread_metadata)
        or await common.get_profile_default_repo(actor.github_login)
        or (await common.get_workspace_settings()).default_repo
    )
    if repo:
        return await check_repo(repo, actor)
    workspace = thread_metadata.get("workspace")
    candidates = await _candidates(workspace if isinstance(workspace, str) else None)
    if not candidates:
        return Denied(NO_REPO)
    chosen = candidates[0] if len(candidates) == 1 else None
    chosen = chosen or await _suggested(issue.id, session_id, candidates)
    if chosen is not None and (parsed := _parse_full_name(chosen)) is not None:
        return await check_repo(parsed, actor)
    return PendingQuestion(
        kind="select_repo",
        prompt=REPO_QUESTION,
        requester_id=actor.provider_user_id,
        options=tuple(SelectOption(value=name, label=name) for name in candidates[:MAX_OPTIONS]),
    )


def repo_from_choice(choice: SelectOption) -> RepoConfig | None:
    return _parse_full_name(choice.value)


_ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}
_ORDINAL_NUMBER = re.compile(r"^#?(\d+)(?:st|nd|rd|th)?\.?$")


def _ordinal(text: str) -> int | None:
    words = text.split()
    for word in words:
        if word in _ORDINAL_WORDS:
            return _ORDINAL_WORDS[word]
    if len(words) == 1 and (match := _ORDINAL_NUMBER.match(words[0])):
        return int(match.group(1))
    return None


def interpret_answer(options: Sequence[SelectOption], answer: str) -> SelectOption | None:
    """The option the person meant: picked as is, by position, or by a unique part of its name."""
    text = " ".join(answer.strip().lower().split())
    if not text:
        return None
    for option in options:
        if text in {option.value.lower(), option.label.lower()}:
            return option
    position = _ordinal(text)
    if position is not None:
        return options[position - 1] if 1 <= position <= len(options) else None
    matches = [
        option
        for option in options
        if option.label.lower() in text
        or text in option.label.lower()
        or option.label.lower().rsplit("/", 1)[-1] in text.split()
    ]
    return matches[0] if len(matches) == 1 else None


def gitlab_denial(status_code: int, full_name: str) -> str:
    if status_code == 503:
        return f"I couldn't check your GitLab access to `{full_name}`. Try again in a minute."
    # The link URL lets a person fix the commonest cause from the session itself.
    return (
        f"You need Developer access to `{full_name}` on GitLab, through a linked GitLab "
        f"account. Link yours at {gitlab_link_url()} if you haven't, then ask again."
    )
