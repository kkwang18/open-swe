"""GitLab as an Open SWE integration: mentions and assignments in, notes out."""

import base64
import binascii
import hashlib
import hmac
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import ClassVar

from pydantic import ValidationError

from agent.config import ENV
from agent.integrations.base import EventLogFields, Ignored, IntegrationName
from agent.integrations.gitlab.client import cached_bot_user
from agent.integrations.gitlab.events import (
    Envelope,
    GitLabEvent,
    GitLabUser,
    IssueAssigned,
    IssuePayload,
    Mentioned,
    NotePayload,
)
from agent.webhooks.event_log import EventRefs

logger = logging.getLogger(__name__)

SIGNING_TOKEN_PREFIX = "whsec_"
# GitLab signs `webhook-timestamp` at send time; a replayed body falls outside this.
SIGNATURE_WINDOW_SECONDS = 300


def _signing_key(secret: str) -> bytes | None:
    try:
        return base64.b64decode(secret.removeprefix(SIGNING_TOKEN_PREFIX), validate=True)
    except binascii.Error:
        return None


def verify_signature(
    headers: Mapping[str, str], body: bytes, secret: str, *, now: float | None = None
) -> bool:
    """Check a delivery against the webhook's signing token or legacy secret token."""
    if not secret:
        return False
    if not secret.startswith(SIGNING_TOKEN_PREFIX):
        return hmac.compare_digest(headers.get("x-gitlab-token", "").encode(), secret.encode())
    key = _signing_key(secret)
    message_id = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    if key is None or not message_id or not timestamp.isdigit():
        return False
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > SIGNATURE_WINDOW_SECONDS:
        return False
    signed = message_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    # The header lists space-separated `v1,<signature>` entries, one per active key.
    for entry in headers.get("webhook-signature", "").split():
        version, _, signature = entry.partition(",")
        if version == "v1" and hmac.compare_digest(signature.encode(), expected.encode()):
            return True
    return False


def mentions(text: str, username: str) -> bool:
    # GitLab usernames may contain `.`, `-` and `_`, so only those end a mention early.
    pattern = rf"(?<![\w.@-])@{re.escape(username)}(?![\w-]|\.\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


class GitLabIntegration:
    name: ClassVar[IntegrationName] = "gitlab"

    def __init__(self, bot: Callable[[], GitLabUser | None] = cached_bot_user) -> None:
        self._bot = bot

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        return verify_signature(headers, body, ENV.GITLAB_WEBHOOK_SECRET.optional() or "")

    def log_fields(self, headers: Mapping[str, str], body: bytes) -> EventLogFields:
        del body
        return {
            "event_type": headers.get("x-gitlab-event", ""),
            "delivery_id": self._delivery_id(headers),
            "refs": EventRefs(),
        }

    def parse(self, headers: Mapping[str, str], body: bytes) -> GitLabEvent | Ignored:
        bot = self._bot()
        if bot is None:
            # Startup resolves it; until then no delivery can be told apart from the bot's own.
            return Ignored("bot user not resolved yet")
        delivery_id = self._delivery_id(headers)
        try:
            kind = Envelope.model_validate_json(body).object_kind
            if kind == "note":
                return self._note(delivery_id, NotePayload.model_validate_json(body), bot)
            if kind == "issue":
                return self._issue(delivery_id, IssuePayload.model_validate_json(body), bot)
        except ValidationError:
            logger.warning("Unparseable GitLab delivery", exc_info=True)
            return Ignored("unparseable payload")
        return Ignored(f"{kind} events are not handled")

    def event_key(self, event: GitLabEvent) -> str:
        if event.delivery_id:
            return event.delivery_id
        if isinstance(event, Mentioned):
            return f"note:{event.project.id}:{event.note_id}"
        return f"assigned:{event.project.id}:{event.iid}:{event.author.id}"

    @staticmethod
    def _delivery_id(headers: Mapping[str, str]) -> str:
        # Unchanged across GitLab's retries of the same delivery.
        return headers.get("idempotency-key", "") or headers.get("webhook-id", "")

    def _note(self, delivery_id: str, payload: NotePayload, bot: GitLabUser) -> Mentioned | Ignored:
        note = payload.object_attributes
        if note.system or payload.user.id == bot.id:
            return Ignored("system note or the bot's own")
        if note.action != "create":
            return Ignored(f"note {note.action} is not handled")
        if not mentions(note.note, bot.username):
            return Ignored("note does not mention the bot")
        if note.noteable_type == "Issue" and payload.issue is not None:
            noteable = payload.issue
            merge_request = None
        elif note.noteable_type == "MergeRequest" and payload.merge_request is not None:
            noteable = merge_request = payload.merge_request
        else:
            return Ignored(f"notes on {note.noteable_type} are not handled")
        return Mentioned(
            delivery_id=delivery_id,
            project=payload.project,
            author=payload.user,
            kind="issue" if merge_request is None else "merge_request",
            iid=noteable.iid,
            title=noteable.title,
            description=noteable.description,
            note_id=note.id,
            note_body=note.note,
            discussion_id=note.discussion_id,
            url=note.url,
            merge_request=merge_request,
        )

    def _issue(
        self, delivery_id: str, payload: IssuePayload, bot: GitLabUser
    ) -> IssueAssigned | Ignored:
        change = payload.changes.assignees
        if change is None or payload.user.id == bot.id:
            return Ignored("not an assignment by a person")
        was_assigned = any(user.id == bot.id for user in change.previous)
        is_assigned = any(user.id == bot.id for user in change.current)
        if was_assigned or not is_assigned:
            return Ignored("the bot was not newly assigned")
        issue = payload.object_attributes
        return IssueAssigned(
            delivery_id=delivery_id,
            project=payload.project,
            author=payload.user,
            iid=issue.iid,
            title=issue.title,
            description=issue.description,
            url=issue.url,
        )


gitlab_integration = GitLabIntegration()
