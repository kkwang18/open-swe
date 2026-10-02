"""The Slack thread a Linear issue was created from, and the updates it gets.

An issue created from Slack keeps the conversation on its own Open SWE thread;
the Slack thread hears when the issue's pull request opens and when the issue
is done, so the person who asked need not watch Linear.
"""

import logging
from collections.abc import Callable, Mapping

from langgraph_sdk import get_client
from langgraph_sdk.errors import NotFoundError
from pydantic import BaseModel, ValidationError

from agent.slack.client import post_slack_thread_reply, post_slack_top_level_message_with_ts
from agent.slack.dm import CONCIERGE_TS, note_for_concierge
from agent.source_context import SourceContext
from agent.thread_ids import linear_issue_thread_id

logger = logging.getLogger(__name__)

# Thread metadata on the issue's thread naming the Slack thread it came from.
ORIGIN_KEY = "linear_slack_origin"


class SlackOrigin(BaseModel):
    channel_id: str
    thread_ts: str
    agent_thread_id: str = ""
    user_id: str = ""
    is_dm: bool = False


async def record_slack_origin(issue_id: str, origin: SlackOrigin) -> None:
    client = get_client()
    thread_id = linear_issue_thread_id(issue_id)
    # The issue's first run creates its thread later; this only adds to it.
    await client.threads.create(thread_id=thread_id, if_exists="do_nothing")
    await client.threads.update(thread_id=thread_id, metadata={ORIGIN_KEY: origin.model_dump()})


async def announce_pull_request(issue_thread_id: str, label: str, pr_url: str) -> None:
    await _announce(issue_thread_id, lambda issue: f"<{pr_url}|{label}> is open for {issue}.")


async def announce_done(issue_id: str, identifier: str, url: str) -> None:
    fallback = f"<{url}|{identifier}>" if url else identifier
    await _announce(linear_issue_thread_id(issue_id), lambda _: f"{fallback} is done.", fallback)


async def _announce(
    issue_thread_id: str, message_for: Callable[[str], str], fallback_issue: str = ""
) -> None:
    try:
        thread = await get_client().threads.get(issue_thread_id)
    except NotFoundError:
        return
    metadata = thread["metadata"] if isinstance(thread["metadata"], Mapping) else {}
    origin = _origin(metadata)
    if origin is None:
        return
    issue = SourceContext.from_metadata(metadata).linear_issue
    issue_link = (
        f"<{issue.url}|{issue.identifier}>"
        if issue is not None and issue.url and issue.identifier
        else fallback_issue or "the Linear issue"
    )
    text = message_for(issue_link)
    if origin.thread_ts == CONCIERGE_TS:
        # A concierge DM has no threads: the conversation is the DM itself.
        _, error = await post_slack_top_level_message_with_ts(origin.channel_id, text)
        posted = error is None
    else:
        posted = await post_slack_thread_reply(
            origin.channel_id,
            origin.thread_ts,
            text,
            agent_thread_id=origin.agent_thread_id or None,
        )
    if not posted:
        logger.warning(
            "Posting a Linear issue update to Slack failed", extra={"thread_id": issue_thread_id}
        )
        return
    if origin.is_dm and origin.user_id:
        await note_for_concierge(origin.user_id, origin.channel_id, text)


def _origin(metadata: Mapping[str, object]) -> SlackOrigin | None:
    raw = metadata.get(ORIGIN_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        return SlackOrigin.model_validate(raw)
    except ValidationError:
        logger.warning("Unreadable Slack origin on a Linear issue thread", exc_info=True)
        return None
