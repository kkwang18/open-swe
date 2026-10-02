from unittest.mock import AsyncMock

import pytest

from agent import dispatch
from agent.integrations.linear import outside, worker
from agent.integrations.linear.events import SessionCreated
from tests.integrations.linear.test_linear_worker import _event, _FakeClient


class _IssueThreadClient(_FakeClient):
    """An issue's thread with one running run that reports to an older session."""

    def __init__(self) -> None:
        super().__init__()
        self.threads.metadata["issue-thread"] = {
            "source_context": {"linear_issue": {"id": "issue-1"}}
        }

        class Runs:
            async def list(self, thread_id, *, status, limit):
                if status != "running":
                    return []
                configurable = {"linear_session": {"id": "older-session"}}
                return [{"run_id": "r-1", "kwargs": {"config": {"configurable": configurable}}}]

        self.runs = Runs()


@pytest.mark.parametrize(
    ("source", "opens_session"),
    [("github", True), ("dashboard", True), ("scheduler", False), ("linear", False)],
)
async def test_runs_people_start_outside_linear_report_to_a_session_of_their_own(
    monkeypatch, source, opens_session
):
    posted: list[str] = []

    async def create_session_on_issue(issue_id, label, url):
        return f"session-on-{issue_id}"

    async def post_activity(session_id, content, **_):
        posted.append(session_id)

    monkeypatch.setattr(outside, "linear_app_configured", lambda: True)
    monkeypatch.setattr(outside, "create_session_on_issue", create_session_on_issue)
    monkeypatch.setattr(outside, "post_activity", post_activity)
    configurable: dict[str, object] = {}

    superseded = await outside.open_session_for_outside_run(
        _IssueThreadClient(), "issue-thread", configurable, source=source, interrupts=True
    )

    assert configurable.get("linear_session") == (
        {"id": "session-on-issue-1", "guidance": ""} if opens_session else None
    )
    # The interrupted session is closed by the caller, once the run exists.
    assert superseded == (("older-session",) if opens_session else ())
    assert "older-session" not in posted


@pytest.mark.parametrize("dispatched", [True, False])
async def test_interrupted_sessions_close_only_once_the_new_run_exists(monkeypatch, dispatched):
    closed = AsyncMock()

    async def open_session_for_outside_run(client, thread_id, configurable, **_):
        return ("older-session",)

    async def create_durable_run(*args, **kwargs):
        if not dispatched:
            raise RuntimeError("dispatch failed")
        return {"run_id": "r-2"}

    monkeypatch.setattr(dispatch, "open_session_for_outside_run", open_session_for_outside_run)
    monkeypatch.setattr(dispatch, "create_durable_run", create_durable_run)
    monkeypatch.setattr(dispatch, "close_superseded_sessions", closed)

    try:
        await dispatch.dispatch_agent_run(
            "issue-thread", None, {}, source="github", thread_title=None, input={"messages": []}
        )
    except RuntimeError:
        assert not dispatched

    assert closed.await_count == (1 if dispatched else 0)


async def test_only_the_session_the_app_opened_is_skipped(monkeypatch):
    # Linear announces the app's own session, and an automation's, with no creator.
    client = _FakeClient()
    created = _event("agent_session_created_delegation")
    assert isinstance(created, SessionCreated)

    async def create_session_on_issue(issue_id, label, url):
        return "own-session"

    monkeypatch.setattr(outside, "create_session_on_issue", create_session_on_issue)
    monkeypatch.setattr(outside, "OWN_SESSION_WAIT_SECONDS", 0.2)
    await outside._create_own_session(client, created.issue.id, "issue-thread")
    acknowledged: list[str] = []

    async def post_activity(session_id, content, **_):
        acknowledged.append(session_id)

    monkeypatch.setattr(worker, "get_client", lambda: client)
    monkeypatch.setattr(worker, "post_activity", post_activity)
    monkeypatch.setattr(worker, "dashboard_thread_url", lambda _thread_id: None)
    monkeypatch.setattr(worker, "_run", AsyncMock())

    own = SessionCreated(**{**created.__dict__, "creator": None, "session_id": "own-session"})
    automation = SessionCreated(
        **{**created.__dict__, "creator": None, "session_id": "automation-session"}
    )
    await worker.process_linear_event(own)
    await worker.process_linear_event(automation)

    assert acknowledged == ["automation-session"]


async def test_a_failed_session_creation_leaves_no_marker(monkeypatch):
    client = _FakeClient()

    async def create_session_on_issue(issue_id, label, url):
        raise RuntimeError("Linear refused")

    monkeypatch.setattr(outside, "create_session_on_issue", create_session_on_issue)

    with pytest.raises(RuntimeError):
        await outside._create_own_session(client, "issue-1", "issue-thread")

    assert client.threads.metadata == {}
