"""Notes the backend posts on GitLab on a run's behalf."""

import logging

import httpx

from agent.integrations.gitlab.client import (
    GitLabAPIError,
    gitlab_configured,
    noteable_kind,
    post_note,
)
from agent.source_context import GitLabRef

logger = logging.getLogger(__name__)


async def post_gitlab_failure(ref: GitLabRef, text: str) -> bool:
    """Tell the issue or merge request a run failed; best-effort, as for the other sources."""
    kind = noteable_kind(ref.kind)
    if not gitlab_configured() or kind is None or ref.project_id is None or ref.iid is None:
        return False
    try:
        # In the discussion that asked when the run knows it; the thread's own record does not.
        await post_note(ref.project_id, kind, ref.iid, text, discussion_id=ref.discussion_id)
    except GitLabAPIError, httpx.HTTPError:
        logger.warning(
            "Posting a GitLab failure note failed",
            extra={"gitlab_project_id": ref.project_id, "gitlab_iid": ref.iid},
            exc_info=True,
        )
        return False
    return True
