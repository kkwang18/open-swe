"""Replying on GitLab from a run that GitLab started."""

import logging

import httpx
from langgraph.config import get_config
from pydantic import JsonValue

from agent.integrations.gitlab.access import GITLAB_ACCESS_DENIED, gitlab_access_allowed
from agent.integrations.gitlab.client import GitLabAPIError, noteable_kind, post_note
from agent.run_config import RunConfig

logger = logging.getLogger(__name__)


async def gitlab_reply(body: str) -> dict[str, JsonValue]:
    """Implement the `gitlab_reply` tool."""
    text = body.strip()
    if not text:
        return {"success": False, "error": "The reply is empty."}
    cfg = RunConfig.from_config(get_config())
    ref = cfg.gitlab
    if ref is None or ref.project_id is None or ref.iid is None:
        return {"success": False, "error": "This run did not come from GitLab."}
    if not await gitlab_access_allowed(cfg):
        return {"success": False, "error": GITLAB_ACCESS_DENIED}
    kind = noteable_kind(ref.kind)
    if kind is None:
        return {"success": False, "error": f"Cannot reply on a GitLab {ref.kind or 'item'}."}
    try:
        note_id = await post_note(
            ref.project_id, kind, ref.iid, text, discussion_id=ref.discussion_id
        )
    except (GitLabAPIError, httpx.HTTPError) as exc:
        logger.warning("Replying on GitLab failed", exc_info=True)
        return {"success": False, "error": f"GitLab refused the reply: {exc}"}
    return {"success": True, "note_id": note_id}
