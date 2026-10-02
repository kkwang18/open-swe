"""GitLab projects a person can pick in Open SWE."""

import asyncio
import logging

import httpx

from agent.integrations.gitlab.access import person_access
from agent.integrations.gitlab.client import GitLabAPIError, bot_projects, gitlab_host
from agent.integrations.gitlab.refs import gitlab_full_name
from agent.source_context import GitLabRef
from agent.users import User

logger = logging.getLogger(__name__)


async def pickable_projects(login: str) -> list[dict[str, str | bool]]:
    """The bot's projects where ``login``'s linked GitLab account has Developer access or more.

    Shaped like the GitHub repository list (``full_name``, ``private``,
    ``archived``) so the dashboard's picker shows both. Empty when the person
    has not linked GitLab or GitLab cannot be reached: the GitHub list still works.
    """
    user = await User.for_login("github", login)
    if user is None or not user.gitlab_user_id:
        return []
    try:
        projects = await bot_projects()
    except GitLabAPIError, httpx.HTTPError:
        logger.warning("Listing the GitLab bot's projects failed", exc_info=True)
        return []
    host = gitlab_host()
    verdicts = await asyncio.gather(
        *(
            person_access(
                login,
                GitLabRef(
                    host=host, project_id=project.id, project_path=project.path_with_namespace
                ),
            )
            for project in projects
        )
    )
    return [
        {
            "full_name": gitlab_full_name(project.path_with_namespace),
            "private": project.visibility != "public",
            "archived": project.archived,
        }
        for project, verdict in zip(projects, verdicts, strict=True)
        if verdict == "allowed"
    ]
