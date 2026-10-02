"""GitLab webhook HTTP route."""

import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Request

from agent.integrations.gitlab.client import (
    GitLabAPIError,
    bot_user,
    cached_bot_user,
    gitlab_configured,
)
from agent.integrations.gitlab.integration import gitlab_integration
from agent.integrations.gitlab.worker import process_gitlab_event
from agent.integrations.intake import accept_webhook

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhooks/gitlab")
async def gitlab_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    if not gitlab_configured():
        return {"status": "ignored", "reason": "GitLab is not configured"}
    if cached_bot_user() is None:
        try:
            # Startup resolves it; this covers a GitLab outage at boot, once.
            await bot_user()
        except GitLabAPIError, httpx.HTTPError:
            logger.warning("Resolving the GitLab bot user failed", exc_info=True)
    body = await request.body()
    return await accept_webhook(
        gitlab_integration, request, body, background_tasks, process_gitlab_event
    )
