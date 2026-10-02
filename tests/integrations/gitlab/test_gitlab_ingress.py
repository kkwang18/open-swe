import base64
import hashlib
import hmac
import json

import pytest

from agent.integrations.base import Ignored
from agent.integrations.gitlab.events import GitLabUser, IssueAssigned, Mentioned
from agent.integrations.gitlab.integration import GitLabIntegration, verify_signature

BOT = GitLabUser(id=99, username="open-swe-bot", name="Open SWE")
PERSON: dict[str, object] = {"id": 7, "username": "ada", "name": "Ada"}
PROJECT: dict[str, object] = {
    "id": 42,
    "path_with_namespace": "acme/tools/widgets",
    "web_url": "https://gitlab.example.com/acme/tools/widgets",
    "default_branch": "main",
}
SIGNING_KEY = b"0123456789abcdef0123456789abcdef"
SIGNING_TOKEN = "whsec_" + base64.b64encode(SIGNING_KEY).decode()


def _note(text: str, *, author: dict[str, object] = PERSON, system: bool = False) -> bytes:
    return json.dumps(
        {
            "object_kind": "note",
            "user": author,
            "project": PROJECT,
            "object_attributes": {
                "id": 1243,
                "note": text,
                "noteable_type": "Issue",
                "system": system,
                "discussion_id": "abc123",
                "url": "https://gitlab.example.com/acme/tools/widgets/-/issues/17#note_1243",
            },
            "issue": {"iid": 17, "title": "Flaky test", "description": None},
        }
    ).encode()


def _assignment(previous: list[dict[str, object]], current: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "object_kind": "issue",
            "user": PERSON,
            "project": PROJECT,
            "object_attributes": {"iid": 17, "title": "Flaky test", "action": "update"},
            "changes": {"assignees": {"previous": previous, "current": current}},
        }
    ).encode()


def _parse(body: bytes) -> object:
    return GitLabIntegration(bot=lambda: BOT).parse({"idempotency-key": "delivery-1"}, body)


def _signed(body: bytes, *, timestamp: int, key: bytes = SIGNING_KEY) -> dict[str, str]:
    signed = b"msg_1." + str(timestamp).encode() + b"." + body
    signature = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {
        "webhook-id": "msg_1",
        "webhook-timestamp": str(timestamp),
        "webhook-signature": f"v1,{signature}",
    }


def test_signing_token_accepts_a_fresh_signature_and_rejects_forged_or_replayed_ones() -> None:
    body = _note("@open-swe-bot fix it")

    assert verify_signature(_signed(body, timestamp=1000), body, SIGNING_TOKEN, now=1000)
    forged = _signed(body, timestamp=1000, key=b"x" * 32)
    assert not verify_signature(forged, body, SIGNING_TOKEN, now=1000)
    assert not verify_signature(_signed(body, timestamp=1000), body + b" ", SIGNING_TOKEN, now=1000)
    assert not verify_signature(_signed(body, timestamp=1000), body, SIGNING_TOKEN, now=1000 + 600)


def test_legacy_secret_token_must_match_exactly() -> None:
    body = _note("@open-swe-bot fix it")

    assert verify_signature({"x-gitlab-token": "s3cret"}, body, "s3cret")
    assert not verify_signature({"x-gitlab-token": "wrong"}, body, "s3cret")
    assert not verify_signature({}, body, "")


def test_a_mention_on_an_issue_becomes_a_request() -> None:
    event = _parse(_note("Can you look at this, @open-swe-bot?"))

    assert isinstance(event, Mentioned)
    assert (event.kind, event.iid, event.discussion_id) == ("issue", 17, "abc123")
    assert event.description == ""
    assert event.project.namespace == "acme/tools"


@pytest.mark.parametrize(
    "body",
    [
        _note("thanks @open-swe-bot-2"),
        _note("mail open-swe-bot@example.com"),
        _note("@open-swe-bot done", author=BOT.model_dump()),
        _note("@open-swe-bot assigned", system=True),
    ],
    ids=["other user", "email", "bot's own note", "system note"],
)
def test_notes_that_are_not_a_request_are_ignored(body: bytes) -> None:
    assert isinstance(_parse(body), Ignored)


def test_only_a_new_assignment_of_the_bot_is_a_request() -> None:
    bot = BOT.model_dump()

    assert isinstance(_parse(_assignment([], [bot])), IssueAssigned)
    assert isinstance(_parse(_assignment([bot], [bot, PERSON])), Ignored)
    assert isinstance(_parse(_assignment([bot], [])), Ignored)


def test_deliveries_wait_until_the_bot_is_known() -> None:
    integration = GitLabIntegration(bot=lambda: None)

    assert isinstance(integration.parse({}, _note("@open-swe-bot fix it")), Ignored)
