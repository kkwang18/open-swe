"""Opening merge requests for runs on GitLab projects, and finding their threads again."""

import logging
import re

import httpx
from pydantic import JsonValue

from agent.integrations.gitlab.client import (
    GitLabAPIError,
    create_merge_request,
    find_open_merge_request,
)
from agent.source_context import GitLabRef

logger = logging.getLogger(__name__)

_THREAD_MARKER = re.compile(r"<!--\s*open-swe-thread:\s*([0-9a-fA-F-]{36})\s*-->")


def _closes(body: str, iid: int) -> bool:
    """Whether the description already uses one of GitLab's closing patterns for the issue."""
    pattern = rf"\b(?:close[sd]?|closing|fix(?:e[sd]|ing)?|resolv(?:e[sd]?|ing)|implement(?:s|ed|ing)?)\s*:?\s+#{iid}\b"
    return re.search(pattern, body, re.IGNORECASE) is not None


def thread_marker(thread_id: str) -> str:
    """Hidden in the description, so a later mention on the MR continues the thread that opened it."""
    return f"<!-- open-swe-thread: {thread_id} -->"


def marked_thread(description: str) -> str | None:
    match = _THREAD_MARKER.search(description)
    return match.group(1).lower() if match else None


def gitlab_project_for(ref: GitLabRef | None, owner: str, repo: str) -> GitLabRef | None:
    """The run's GitLab project when ``owner/repo`` names it; GitHub repos get ``None``."""
    if ref is None or ref.project_id is None or not ref.project_path:
        return None
    return ref if ref.project_path.lower() == f"{owner}/{repo}".lower() else None


def _description(body: str, ref: GitLabRef, thread_id: str, *, resolves_thread: bool) -> str:
    lines = [body.strip()]
    if (
        resolves_thread
        and ref.kind == "issue"
        and ref.iid is not None
        and not _closes(body, ref.iid)
    ):
        lines.append(f"Closes #{ref.iid}")
    if thread_id:
        lines.append(thread_marker(thread_id))
    return "\n\n".join(line for line in lines if line)


async def open_merge_request(
    ref: GitLabRef,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
    draft: bool,
    resolves_thread: bool,
    thread_id: str,
) -> dict[str, JsonValue]:
    """Implement `open_pull_request` for a GitLab project, with the same result shape."""
    if ref.project_id is None:
        return {"success": False, "error": "The GitLab project is unknown."}
    try:
        existing = await find_open_merge_request(ref.project_id, head, base)
        if existing is not None:
            return {
                "success": True,
                "created": False,
                "url": existing.web_url,
                "number": existing.iid,
                "token_kind": "bot",
            }
        merge_request = await create_merge_request(
            ref.project_id,
            source_branch=head,
            target_branch=base,
            title=title,
            description=_description(body, ref, thread_id, resolves_thread=resolves_thread),
            draft=draft,
        )
    except (GitLabAPIError, httpx.HTTPError) as exc:
        logger.warning(
            "Opening a GitLab merge request failed",
            extra={"gitlab_project_id": ref.project_id, "head": head, "base": base},
            exc_info=True,
        )
        return {
            "success": False,
            "error": f"GitLab refused the merge request: {exc}",
            "likely_cause": "the branch was not pushed, or the bot lacks Developer access",
        }
    return {
        "success": True,
        "created": True,
        "url": merge_request.web_url,
        "number": merge_request.iid,
        "draft": merge_request.draft,
        "token_kind": "bot",
    }
