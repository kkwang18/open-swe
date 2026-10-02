"""Closing a Linear agent session with the run's answer."""

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence

from langchain_core.messages import AIMessage, BaseMessage
from langgraph_sdk.schema import Run
from pydantic import JsonValue, ValidationError

from agent.integrations.linear.client import linear_activity_id, post_activity
from agent.source_context import LinearSessionRef
from agent.utils.dashboard_links import dashboard_thread_url

logger = logging.getLogger(__name__)

REPLY_ATTEMPTS = 3


def run_session(run: Run) -> LinearSessionRef | None:
    """The Linear session a run reports to.

    Only the run's own config says which: an issue thread outlives its sessions, and
    runs started elsewhere, such as from the dashboard, report to none.
    """
    kwargs = run.get("kwargs")
    config = kwargs.get("config") if isinstance(kwargs, Mapping) else None
    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    session = configurable.get("linear_session") if isinstance(configurable, Mapping) else None
    if not isinstance(session, Mapping):
        return None
    try:
        ref = LinearSessionRef.model_validate(session)
    except ValidationError:
        logger.warning("Unreadable Linear session on run", exc_info=True)
        return None
    return ref if ref.id else None


def run_session_id(run: Run) -> str | None:
    session = run_session(run)
    return session.id if session is not None else None


def final_answer(messages: Sequence[BaseMessage]) -> str:
    """The text of the turn's last assistant message, which is the run's answer."""
    # Imported here so the web app, which loads this module, stays clear of the agent stack.
    from agent.middleware.message_content import content_to_text
    from agent.middleware.require_user_reply import turn_tail

    for message in reversed(turn_tail(messages)):
        if isinstance(message, AIMessage):
            text = content_to_text(message.content).strip()
            if text:
                return text
    return ""


# Code and links can carry a "?" that asks nothing.
_NOT_PROSE = re.compile(r"```.*?```|`[^`]*`|https?://\S+", re.DOTALL)


def _ends_with_question(answer: str) -> bool:
    """A turn whose last paragraph asks something leaves the session awaiting the person."""
    paragraphs = [part for part in re.split(r"\n\s*\n", answer.strip()) if part.strip()]
    return bool(paragraphs) and "?" in _NOT_PROSE.sub("", paragraphs[-1])


def _fallback(thread_id: str) -> str:
    url = dashboard_thread_url(thread_id)
    return f"Done. The details are in the [Open SWE thread]({url})." if url else "Done."


async def post_final_response(session_id: str, run_id: str, thread_id: str, answer: str) -> bool:
    """Post the run's answer once; the in-run post and the completion backstop share its id.

    An answer that ends with a question is posted as an elicitation, so Linear shows
    the session as awaiting input; the person's reply arrives as a new prompt.
    """
    kind = "elicitation" if _ends_with_question(answer) else "response"
    content: dict[str, JsonValue] = {"type": kind, "body": answer or _fallback(thread_id)}
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


async def post_session_error(session_id: str, run_id: str | None, text: str) -> bool:
    """End the session with an error; with the run id it shares the response's single slot."""
    content: dict[str, JsonValue] = {"type": "error", "body": text}
    try:
        await post_activity(
            session_id,
            content,
            activity_id=linear_activity_id("reply", run_id) if run_id else None,
        )
    except Exception:
        logger.warning(
            "Posting the Linear session error failed",
            extra={"linear_session_id": session_id},
            exc_info=True,
        )
        return False
    return True
