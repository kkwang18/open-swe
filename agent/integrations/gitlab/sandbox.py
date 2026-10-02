"""Git credentials for the sandbox of a run on a GitLab project."""

import base64
import logging

from langgraph.config import get_config

from agent.config import ENV
from agent.integrations.gitlab.access import gitlab_access_allowed
from agent.integrations.gitlab.client import gitlab_host
from agent.integrations.gitlab.project import run_gitlab_project
from agent.run_config import RunConfig

logger = logging.getLogger(__name__)

GITLAB_RULE = "open-swe-gitlab"


def _current_run() -> RunConfig | None:
    try:
        return RunConfig.from_config(get_config())
    except RuntimeError:
        return None


async def gitlab_proxy_rule(thread_id: str | None) -> dict[str, object] | None:
    """The proxy rule adding the bot's token to git traffic, for runs allowed GitLab access.

    Each run re-applies the proxy when it attaches to its sandbox, so a sandbox
    shared by a thread's runs holds the token only during the runs allowed it.
    Outside a run, or for any other thread, it gets none: the bot may reach
    projects the people asking cannot.
    """
    token = ENV.GITLAB_TOKEN.optional()
    host = gitlab_host()
    if not thread_id or not token or not host:
        return None
    run = _current_run()
    if run is None or run.thread_id != thread_id or run_gitlab_project(run) is None:
        return None
    allowed = await gitlab_access_allowed(run)
    logger.info(
        "GitLab sandbox access decided",
        extra={"thread_id": thread_id, "run_id": run.run_id, "gitlab_access": allowed},
    )
    if not allowed:
        return None
    # Git over HTTPS takes a token as the password of any user; GitLab documents `oauth2`.
    basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
    return {
        "name": GITLAB_RULE,
        "match_hosts": [host],
        "headers": [{"name": "Authorization", "type": "opaque", "value": f"Basic {basic}"}],
    }
