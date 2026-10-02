"""Which runs work on a GitLab project, and how their sandbox reaches it."""

import logging

import httpx

from agent.integrations.gitlab.client import GitLabAPIError, bot_user, gitlab_host
from agent.integrations.gitlab.merge_requests import gitlab_project_for
from agent.run_config import RunConfig
from agent.utils.authorship import CollaboratorIdentity

logger = logging.getLogger(__name__)


def gitlab_clone_url(cfg: RunConfig) -> str:
    """The HTTPS clone URL when the run's repository is its GitLab project, else empty."""
    if cfg.repo is None:
        return ""
    project = gitlab_project_for(cfg.gitlab, cfg.repo.owner, cfg.repo.name)
    if project is None or not project.project_url:
        return ""
    return f"{project.project_url.rstrip('/')}.git"


async def gitlab_commit_identity(cfg: RunConfig) -> CollaboratorIdentity | None:
    """The GitLab bot's identity for a GitLab run that no linked person started.

    Its GitLab noreply address credits the commits to the bot's account, where
    the GitHub bot's address would match no GitLab user.
    """
    if cfg.source != "gitlab" or not gitlab_clone_url(cfg):
        return None
    try:
        bot = await bot_user()
    except GitLabAPIError, httpx.HTTPError:
        # The run still commits, as the Open SWE bot it falls back to.
        logger.warning("Resolving the GitLab bot's commit identity failed", exc_info=True)
        return None
    name = bot.name or bot.username
    return CollaboratorIdentity(
        display_name=name,
        commit_name=name,
        commit_email=f"{bot.id}-{bot.username}@users.noreply.{gitlab_host()}",
    )
