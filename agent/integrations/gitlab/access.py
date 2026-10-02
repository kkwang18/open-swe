"""Which runs on a GitLab thread may act on GitLab with the bot's token.

GitLab checks nothing when the bot pushes, comments or opens a merge request:
the bot's memberships are all that limit it. So the requester's GitLab role is
the check, made when they ask from GitLab. A run started from GitLab passed it
moments ago. A run someone else starts on the same thread, from the dashboard,
passed it only if that person has asked from GitLab on this thread before; the
others can still read the thread and talk to it, but not act on GitLab.
"""

import logging
from collections.abc import Mapping

from langgraph_sdk import get_client
from langgraph_sdk.errors import NotFoundError

from agent.run_config import RunConfig
from agent.utils.thread_participants import merge_participants, participant_logins

logger = logging.getLogger(__name__)

# Open SWE logins that asked from GitLab on this thread and had Developer access then.
REQUESTERS_KEY = "gitlab_requester_logins"

GITLAB_ACCESS_DENIED = (
    "This run cannot act on GitLab: only people who have asked Open SWE from GitLab on this "
    "issue or merge request, with Developer access, can have it push, open merge requests "
    "or comment there. Ask them to, or mention the bot from GitLab yourself."
)


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


async def gitlab_access_allowed(cfg: RunConfig) -> bool:
    """Whether this run may use the bot's GitLab access; ``False`` for non-GitLab runs."""
    if cfg.gitlab is None or not cfg.thread_id:
        return False
    # Dashboard and API runs always name their sender; GitLab-started runs never do.
    login = (cfg.github_login or "").strip().lower()
    if not login:
        return True
    try:
        thread = await get_client().threads.get(cfg.thread_id)
    except NotFoundError:
        return False
    except Exception:
        logger.warning(
            "Reading GitLab requesters failed; denying GitLab access",
            extra={"thread_id": cfg.thread_id},
            exc_info=True,
        )
        return False
    return login in participant_logins(_metadata(thread).get(REQUESTERS_KEY))
