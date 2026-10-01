"""Closing a Linear agent session with the run's answer."""

import asyncio
import logging
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import JsonValue

from agent.integrations.linear.client import linear_activity_id, post_activity
from agent.middleware.message_content import content_to_text
from agent.middleware.require_user_reply import turn_tail
from agent.utils.dashboard_links import dashboard_thread_url

logger = logging.getLogger(__name__)

REPLY_ATTEMPTS = 3


def final_answer(messages: Sequence[BaseMessage]) -> str:
    """The text of the turn's last assistant message, which is the run's answer."""
    for message in reversed(turn_tail(messages)):
        if isinstance(message, AIMessage):
            text = content_to_text(message.content).strip()
            if text:
                return text
    return ""


def _fallback(thread_id: str) -> str:
    url = dashboard_thread_url(thread_id)
    return f"Done. The details are in the [Open SWE thread]({url})." if url else "Done."


async def post_final_response(session_id: str, run_id: str, thread_id: str, answer: str) -> bool:
    """Post the run's answer once; the in-run post and the completion backstop share its id."""
    content: dict[str, JsonValue] = {"type": "response", "body": answer or _fallback(thread_id)}
    activity_id = linear_activity_id("reply", run_id)
    for attempt in range(REPLY_ATTEMPTS):
        try:
            await post_activity(session_id, content, activity_id=activity_id)
        except Exception:
            logger.warning(
                "Posting the Linear session response failed",
                extra={"linear_session_id": session_id, "attempt": attempt + 1},
                exc_info=True,
            )
            if attempt + 1 < REPLY_ATTEMPTS:
                await asyncio.sleep(2**attempt)
        else:
            return True
    return False
