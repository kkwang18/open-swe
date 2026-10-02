"""GitHub's webhook signature check and event-log fields."""

from collections.abc import Mapping
from typing import ClassVar

from agent.integrations.base import EventLogFields, IntegrationName
from agent.webhooks import common
from agent.webhooks.event_log import EventRefs


class GitHubWebhook:
    name: ClassVar[IntegrationName] = "github"

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        signature = headers.get("x-hub-signature-256", "")
        return common.verify_github_signature(body, signature, secret=common.GITHUB_WEBHOOK_SECRET)

    def log_fields(self, headers: Mapping[str, str], body: bytes) -> EventLogFields:
        return {
            "event_type": headers.get("x-github-event", ""),
            "delivery_id": headers.get("x-github-delivery", ""),
            "refs": EventRefs.github(body),
        }


github_ingress = GitHubWebhook()
