"""Linear as an Open SWE integration: agent sessions in, session activities out."""

import logging
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from pydantic import ValidationError

from agent.config import ENV
from agent.integrations.base import EventLogFields, Ignored, IntegrationName
from agent.integrations.linear.events import (
    AgentSessionPayload,
    DelegationRemoved,
    Envelope,
    GuidanceRule,
    IssueUpdatePayload,
    LinearEvent,
    LinearIssue,
    SessionCreated,
    SessionPrompted,
)
from agent.webhooks.common import verify_linear_signature
from agent.webhooks.event_log import EventRefs

logger = logging.getLogger(__name__)

# Linear signs `webhookTimestamp` at send time, retries included, so a replayed
# body falls outside this window.
SIGNATURE_WINDOW = timedelta(seconds=60)
# Retries keep `createdAt`; only the 1-minute retry lands inside this.
STALE_AFTER = timedelta(minutes=5)


# Linear sends an @mention inside a session message as `<user id="…" notify>name</user>`.
_MENTION_MARKUP = re.compile(r"<user\b[^>]*>([^<]*)</user>")


def _plain_mentions(body: str) -> str:
    return _MENTION_MARKUP.sub(r"@\1", body)


# The run gets guidance in its system prompt, so it is left out of the request's context.
_GUIDANCE_BLOCK = re.compile(r"\s*<guidance>.*?</guidance>", re.DOTALL)


def _guidance_text(rules: list[GuidanceRule] | None) -> str:
    """Linear lists workspace guidance first and the session's own team last."""
    sections: list[str] = []
    for rule in rules or []:
        if not rule.body.strip():
            continue
        team = rule.origin.team.name if rule.origin.team else ""
        source = f"team {team}" if team else "the workspace"
        sections.append(f"From {source}:\n{rule.body.strip()}")
    return "\n\n".join(sections)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LinearIntegration:
    name: ClassVar[IntegrationName] = "linear"

    def __init__(self, now: Callable[[], datetime] = _utcnow) -> None:
        self._now = now

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        signature = headers.get("linear-signature", "")
        if not verify_linear_signature(body, signature, ENV.LINEAR_WEBHOOK_SECRET.get()):
            return False
        try:
            envelope = Envelope.model_validate_json(body)
        except ValidationError:
            return False
        sent_at = datetime.fromtimestamp(envelope.webhook_timestamp / 1000, UTC)
        return abs(self._now() - sent_at) <= SIGNATURE_WINDOW

    def log_fields(self, headers: Mapping[str, str], body: bytes) -> EventLogFields:
        return {
            "event_type": headers.get("linear-event", ""),
            "delivery_id": headers.get("linear-delivery", ""),
            "refs": EventRefs.linear(body),
        }

    def parse(self, headers: Mapping[str, str], body: bytes) -> LinearEvent | Ignored:
        delivery_id = headers.get("linear-delivery", "")
        if not delivery_id:
            return Ignored("no delivery id")
        try:
            event = self._event(delivery_id, body)
        except ValidationError:
            logger.warning("Unparseable Linear delivery", exc_info=True)
            return Ignored("unparseable payload")
        if isinstance(event, Ignored) or not self._is_stale(event):
            return event
        return Ignored("stale")

    def event_key(self, event: LinearEvent) -> str:
        return event.delivery_id

    def _event(self, delivery_id: str, body: bytes) -> LinearEvent | Ignored:
        envelope = Envelope.model_validate_json(body)
        created_at = envelope.created_at
        if envelope.type == "AgentSessionEvent" and created_at is not None:
            return self._session_event(delivery_id, envelope.action, created_at, body)
        if envelope.type == "Issue" and envelope.action == "update" and created_at is not None:
            return self._delegation_removed(delivery_id, created_at, body)
        return Ignored(f"{envelope.type} {envelope.action} is not handled")

    def _session_event(
        self, delivery_id: str, action: str, created_at: datetime, body: bytes
    ) -> LinearEvent | Ignored:
        payload = AgentSessionPayload.model_validate_json(body)
        session = payload.agent_session
        guidance = _guidance_text(payload.guidance)
        if action == "created":
            if session.issue is None:
                return Ignored("session has no issue")
            return SessionCreated(
                delivery_id=delivery_id,
                created_at=created_at,
                session_id=session.id,
                issue=session.issue,
                creator=session.creator,
                from_mention=session.source_metadata is not None
                and session.source_metadata.type == "comment",
                comment_id=session.comment_id,
                comment_body=session.comment.body if session.comment else "",
                prompt_context=(
                    _GUIDANCE_BLOCK.sub("", payload.prompt_context)
                    if guidance
                    else payload.prompt_context
                ),
                guidance=guidance,
            )
        if action == "prompted" and payload.agent_activity is not None:
            activity = payload.agent_activity
            return SessionPrompted(
                delivery_id=delivery_id,
                created_at=created_at,
                session_id=session.id,
                issue=session.issue,
                activity_id=activity.id,
                body=_plain_mentions(activity.content.body),
                author=activity.user,
                signal=activity.signal,
                guidance=guidance,
            )
        return Ignored(f"agent session {action} is not handled")

    def _delegation_removed(
        self, delivery_id: str, created_at: datetime, body: bytes
    ) -> DelegationRemoved | Ignored:
        payload = IssueUpdatePayload.model_validate_json(body)
        previous = payload.updated_from.get("delegateId")
        if not isinstance(previous, str) or payload.data.delegate_id is not None:
            return Ignored("not an undelegation")
        return DelegationRemoved(
            delivery_id=delivery_id,
            created_at=created_at,
            issue=LinearIssue.model_validate(payload.data.model_dump(by_alias=True)),
            previous_delegate_id=previous,
        )

    def _is_stale(self, event: LinearEvent) -> bool:
        # Stopping is always wanted, however late it arrives.
        if isinstance(event, DelegationRemoved):
            return False
        if isinstance(event, SessionPrompted) and event.signal == "stop":
            return False
        return self._now() - event.created_at > STALE_AFTER


linear_integration = LinearIntegration()
