"""Tests for Linear webhook signature checks and comment payload handling."""

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from agent.linear.routes import linear_webhook
from agent.webhooks import common

SECRET = "linear-test-secret"


def _comment_payload(**data: object) -> dict[str, Any]:
    return {
        "type": "Comment",
        "action": "create",
        "url": "https://linear.app/acme/issue/OSWE-6/spike-linear-agent#comment-abc",
        "actor": {"id": "user-1", "name": "Kenny", "email": "kenny@example.com"},
        "data": {
            "id": "comment-1",
            "body": "@openswe add tests repo:acme/widgets",
            "issueId": "issue-1",
            **data,
        },
    }


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _deliver(
    payload: dict[str, Any], *, signature: str | None = None, secret: str = SECRET
) -> tuple[dict[str, str], MagicMock]:
    body = json.dumps(payload).encode()
    request = AsyncMock()
    request.body.return_value = body
    request.headers = {"Linear-Signature": _sign(body) if signature is None else signature}
    background_tasks = MagicMock()
    with (
        patch.object(common, "LINEAR_WEBHOOK_SECRET", secret),
        patch.object(common, "is_repo_allowed", return_value=True),
    ):
        result = await linear_webhook(request, background_tasks)
    return result, background_tasks


async def test_signed_comment_is_scheduled_with_issue_details_from_payload() -> None:
    result, background_tasks = await _deliver(_comment_payload())

    assert result["status"] == "accepted"
    _, issue, repo_config = background_tasks.add_task.call_args.args
    assert repo_config == {"owner": "acme", "name": "widgets"}
    assert issue["id"] == "issue-1"
    assert issue["identifier"] == "OSWE-6"
    assert issue["url"] == "https://linear.app/acme/issue/OSWE-6/spike-linear-agent"
    assert issue["triggering_comment_id"] == "comment-1"
    assert issue["comment_author"]["email"] == "kenny@example.com"


@pytest.mark.parametrize(
    ("signature", "secret"),
    [
        pytest.param("", SECRET, id="missing-signature"),
        pytest.param("0" * 64, SECRET, id="wrong-signature"),
        pytest.param(None, "", id="secret-not-configured"),
    ],
)
async def test_unverified_delivery_is_rejected(signature: str | None, secret: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _deliver(_comment_payload(), signature=signature, secret=secret)

    assert exc_info.value.status_code == 401


async def test_signature_for_a_different_body_is_rejected() -> None:
    original = json.dumps(_comment_payload()).encode()

    with pytest.raises(HTTPException) as exc_info:
        await _deliver(
            _comment_payload(body="@openswe delete everything repo:acme/widgets"),
            signature=_sign(original),
        )

    assert exc_info.value.status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({**_comment_payload(), "type": "Issue"}, id="not-a-comment"),
        pytest.param({**_comment_payload(), "action": "update"}, id="edited-comment"),
        pytest.param(_comment_payload(botActor={"id": "bot"}), id="bot-actor"),
        pytest.param(
            _comment_payload(body="✅ **Pull Request Created** @openswe"), id="own-bot-message"
        ),
        pytest.param(_comment_payload(body="please add tests"), id="no-mention"),
        pytest.param(_comment_payload(issueId=None), id="no-issue-id"),
    ],
)
async def test_comment_that_should_not_start_a_run_is_ignored(payload: dict[str, Any]) -> None:
    result, background_tasks = await _deliver(payload)

    assert result["status"] == "ignored"
    background_tasks.add_task.assert_not_called()
