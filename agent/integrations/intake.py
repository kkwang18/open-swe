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

from agent.integrations.base import Ignored, IntegrationName, SignedWebhook, WebhookIngress
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


async def verify_and_record(webhook: SignedWebhook, request: Request, body: bytes) -> None:
    """Reject an unsigned delivery with 401, then record it in the event log."""
    if not webhook.verify(request.headers, body):
        logger.warning("Rejected integration webhook", extra={"integration": webhook.name})
        raise HTTPException(status_code=401, detail="Invalid signature")
    await EventLog.record(request, body, webhook.name, **webhook.log_fields(request.headers, body))


async def accept_webhook[EventT](
    ingress: WebhookIngress[EventT],
    request: Request,
    body: bytes,
    background_tasks: BackgroundTasks,
    handle: Callable[[EventT], Awaitable[None]],
) -> dict[str, str]:
    await verify_and_record(ingress, request, body)
    event = ingress.parse(request.headers, body)
    if isinstance(event, Ignored):
        return {"status": "ignored", "reason": event.reason}
    if not await claim_event(ingress.name, ingress.event_key(event)):
        return {"status": "ignored", "reason": "duplicate delivery"}
    background_tasks.add_task(handle, event)
    return {"status": "accepted"}
