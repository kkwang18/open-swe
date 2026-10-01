"""Webhook intake shared by integrations: verify, log, parse, dedupe, hand off.

The provider gets its 200 as soon as the delivery is claimed; everything that
calls the provider back runs afterwards in a background task, as on Slack.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import BackgroundTasks, HTTPException, Request
from langgraph_sdk import get_client
from langgraph_sdk.errors import ConflictError

from agent.integrations.base import Ignored, IntegrationName, WebhookIngress
from agent.threads.creation import create_lock_thread
from agent.webhooks.event_log import EventLog

logger = logging.getLogger(__name__)

# Covers the provider's quick retries; later ones are older than any integration acts on.
CLAIM_TTL_MINUTES = 10


def _claim_thread_id(source: IntegrationName, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"open-swe:{source}-event:{key}"))


async def claim_event(source: IntegrationName, key: str) -> bool:
    """Claim a delivery once across processes; fail open when the platform is unavailable."""
    try:
        await create_lock_thread(
            get_client(), _claim_thread_id(source, key), ttl_minutes=CLAIM_TTL_MINUTES
        )
    except ConflictError:
        return False
    except Exception:  # noqa: BLE001
        # A duplicate run is better than a dropped request, as Slack's claim decides.
        logger.warning(
            "Integration event claim failed; accepting the delivery",
            extra={"integration": source, "event_key": key},
            exc_info=True,
        )
    return True


async def accept_webhook[EventT](
    ingress: WebhookIngress[EventT],
    request: Request,
    body: bytes,
    background_tasks: BackgroundTasks,
    handle: Callable[[EventT], Awaitable[None]],
) -> dict[str, str]:
    if not ingress.verify(request.headers, body):
        logger.warning("Rejected integration webhook", extra={"integration": ingress.name})
        raise HTTPException(status_code=401, detail="Invalid signature")
    await EventLog.record(request, body, ingress.name, **ingress.log_fields(request.headers, body))
    event = ingress.parse(request.headers, body)
    if isinstance(event, Ignored):
        return {"status": "ignored", "reason": event.reason}
    if not await claim_event(ingress.name, ingress.event_key(event)):
        return {"status": "ignored", "reason": "duplicate delivery"}
    background_tasks.add_task(handle, event)
    return {"status": "accepted"}
