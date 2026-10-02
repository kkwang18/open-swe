import hashlib
import hmac
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException, Request
from langgraph_sdk.errors import ConflictError

from agent.integrations import intake
from agent.integrations.base import Ignored
from agent.integrations.linear.events import (
    DelegationRemoved,
    IssueCompleted,
    LinearEvent,
    SessionCreated,
)
from agent.integrations.linear.integration import LinearIntegration
from agent.prompt import construct_system_prompt

FIXTURES = Path(__file__).with_name("fixtures")
SECRET = "test-webhook-secret"


def _payload(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _created_at(name: str) -> datetime:
    return datetime.fromisoformat(str(_payload(name)["createdAt"]))


def _delivery(
    name: str, *, sent_at: datetime, secret: str = SECRET
) -> tuple[bytes, dict[str, str]]:
    payload = _payload(name)
    payload["webhookTimestamp"] = int(sent_at.timestamp() * 1000)
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "linear-signature": signature,
        "linear-delivery": f"delivery-{name}",
        "linear-event": str(payload["type"]),
    }
    return body, headers


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/webhooks/linear",
            "query_string": b"",
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
        }
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(intake.EventLog, "record", AsyncMock())


async def test_retried_delivery_is_handed_off_once(monkeypatch):
    held: set[str] = set()

    async def lock(_client, thread_id: str, *, ttl_minutes: int) -> None:
        if thread_id in held:
            conflict = httpx.Response(409, request=httpx.Request("POST", "http://langgraph"))
            raise ConflictError("held", response=conflict, body=None)
        held.add(thread_id)

    monkeypatch.setattr(intake, "create_lock_thread", lock)
    monkeypatch.setattr(intake, "get_client", lambda: None)
    now = _created_at("agent_session_created_delegation") + timedelta(seconds=2)
    linear = LinearIntegration(now=lambda: now)
    handled: list[LinearEvent] = []

    async def handle(event: LinearEvent) -> None:
        handled.append(event)

    body, headers = _delivery("agent_session_created_delegation", sent_at=now)
    for _ in range(2):
        tasks = BackgroundTasks()
        await intake.accept_webhook(linear, _request(headers), body, tasks, handle)
        await tasks()

    assert len(handled) == 1
    event = handled[0]
    assert isinstance(event, SessionCreated)
    assert event.creator is not None and event.creator.id


async def test_bad_signature_and_replayed_body_are_rejected():
    now = _created_at("agent_session_created_delegation")
    linear = LinearIntegration(now=lambda: now)

    forged, headers = _delivery("agent_session_created_delegation", sent_at=now, secret="wrong")
    with pytest.raises(HTTPException) as rejected:
        await intake.accept_webhook(
            linear, _request(headers), forged, BackgroundTasks(), AsyncMock()
        )
    assert rejected.value.status_code == 401

    replayed, headers = _delivery(
        "agent_session_created_delegation", sent_at=now - timedelta(minutes=2)
    )
    assert not linear.verify(headers, replayed)


def test_late_prompt_is_ignored_but_late_stop_and_undelegation_are_not():
    def parse(name: str):
        late = _created_at(name) + timedelta(hours=1)
        body, headers = _delivery(name, sent_at=late)
        return LinearIntegration(now=lambda: late).parse(headers, body)

    assert parse("agent_session_prompted") == Ignored("stale")
    assert parse("agent_session_prompted_stop").signal == "stop"
    assert isinstance(parse("issue_undelegated"), DelegationRemoved)
    assert isinstance(parse("issue_delegated"), Ignored)


def test_session_guidance_reaches_the_system_prompt_once():
    payload = _payload("agent_session_created_delegation")
    payload["guidance"] = [
        {"body": "Open PRs as drafts.", "origin": {"type": "Organization"}},
        {
            "body": "Work in acme/api.",
            "origin": {"type": "Team", "team": {"id": "t1", "name": "Backend"}},
        },
    ]
    payload["promptContext"] = (
        f"{payload['promptContext']}\n<guidance>\n"
        '<guidance-rule origin="team" team-name="Backend">Work in acme/api.</guidance-rule>\n'
        "</guidance>"
    )
    now = _created_at("agent_session_created_delegation")
    event = LinearIntegration(now=lambda: now)._event("delivery-1", json.dumps(payload).encode())
    assert isinstance(event, SessionCreated)

    system_prompt = construct_system_prompt(
        working_dir="/workspace",
        source="linear",
        linear_session=True,
        linear_guidance=event.guidance,
    )
    assert "From the workspace:\nOpen PRs as drafts." in system_prompt
    assert "From team Backend:\nWork in acme/api." in system_prompt
    assert "<guidance>" not in event.prompt_context
    assert '<issue identifier="OSWE-6">' in event.prompt_context


@pytest.mark.parametrize(("state_type", "completed"), [("completed", True), ("started", False)])
def test_only_a_move_to_a_completed_status_is_an_issue_completion(state_type, completed):
    payload = _payload("issue_delegated")
    payload["updatedFrom"] = {"stateId": "previous-state"}
    payload["data"]["state"] = {"id": "s1", "name": "Done", "type": state_type}
    now = _created_at("issue_delegated")
    event = LinearIntegration(now=lambda: now)._event("delivery-1", json.dumps(payload).encode())
    assert isinstance(event, IssueCompleted) is completed
