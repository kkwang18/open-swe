"""Slack's webhook signature check and event-log fields, one per Slack endpoint."""

from collections.abc import Mapping
from typing import ClassVar
from urllib.parse import parse_qs

from agent.integrations.base import EventLogFields, IntegrationName
from agent.slack.payloads import SlackEventEnvelope, SlackInteraction, parse_json_object
from agent.webhooks import common
from agent.webhooks.event_log import EventRefs


def _form_value(body: bytes, key: str) -> str:
    form = parse_qs(body.decode("utf-8"))
    return str((form.get(key) or [""])[0]).strip()


class _SlackWebhook:
    name: ClassVar[IntegrationName] = "slack"

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        return common.verify_slack_signature(
            body=body,
            timestamp=headers.get("x-slack-request-timestamp", ""),
            signature=headers.get("x-slack-signature", ""),
            secret=common.SLACK_SIGNING_SECRET,
        )


class SlackEventsWebhook(_SlackWebhook):
    """The Events API endpoint."""

    def log_fields(self, headers: Mapping[str, str], body: bytes) -> EventLogFields:
        payload = parse_json_object(body)
        envelope = SlackEventEnvelope.parse(payload) if payload is not None else None
        if envelope is None:
            return {"event_type": "", "delivery_id": "", "refs": EventRefs()}
        return {
            "event_type": envelope.kind,
            "delivery_id": envelope.event_id,
            "refs": EventRefs.slack(envelope),
        }


class SlackCommandWebhook(_SlackWebhook):
    """Slash-command endpoints, which post a form."""

    def log_fields(self, headers: Mapping[str, str], body: bytes) -> EventLogFields:
        return {
            "event_type": _form_value(body, "command"),
            "delivery_id": _form_value(body, "trigger_id"),
            "refs": EventRefs(
                slack_user_id=_form_value(body, "user_id"),
                slack_channel_id=_form_value(body, "channel_id"),
            ),
        }


class SlackInteractivityWebhook(_SlackWebhook):
    """The Block Kit interactivity endpoint, which posts the payload as a form field."""

    def log_fields(self, headers: Mapping[str, str], body: bytes) -> EventLogFields:
        payload = parse_json_object(_form_value(body, "payload").encode("utf-8"))
        interaction = SlackInteraction.parse(payload) if payload is not None else None
        if interaction is None:
            return {"event_type": "", "delivery_id": "", "refs": EventRefs()}
        return {
            "event_type": interaction.type,
            "delivery_id": interaction.trigger_id,
            "refs": EventRefs(
                slack_user_id=interaction.user.id,
                slack_channel_id=interaction.origin_channel_id,
            ),
        }


slack_events_ingress = SlackEventsWebhook()
slack_command_ingress = SlackCommandWebhook()
slack_interactivity_ingress = SlackInteractivityWebhook()
