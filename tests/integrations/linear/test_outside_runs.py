import httpx
import pytest
from langgraph_sdk.errors import NotFoundError

from agent.integrations.linear import outside, worker
from agent.integrations.linear.events import SessionCreated
from tests.integrations.linear.test_worker import _event, _FakeClient


@pytest.mark.parametrize(
    ("source", "opens_session"),
    [("github", True), ("dashboard", True), ("scheduler", False), ("linear", False)],
)
async def test_runs_people_start_outside_linear_report_to_a_session_of_their_own(
    monkeypatch, source, opens_session
):
    class Threads:
        async def get(self, thread_id):
            if thread_id != "issue-thread":
                response = httpx.Response(404, request=httpx.Request("GET", "http://langgraph"))
                raise NotFoundError("missing", response=response, body=None)
            return {"metadata": {"source_context": {"linear_issue": {"id": "issue-1"}}}}

        async def create(self, **_):
            return None

    class Client:
        threads = Threads()

    async def create_session_on_issue(issue_id, label, url):
        return f"session-on-{issue_id}"

    async def post_activity(session_id, content, **_):
        return None

    monkeypatch.setattr(outside, "linear_app_configured", lambda: True)
    monkeypatch.setattr(outside, "create_session_on_issue", create_session_on_issue)
    monkeypatch.setattr(outside, "post_activity", post_activity)
    configurable: dict[str, object] = {}

    await outside.open_session_for_outside_run(
        Client(), "issue-thread", configurable, source=source
    )

    assert configurable.get("linear_session") == (
        {"id": "session-on-issue-1", "guidance": ""} if opens_session else None
    )


async def test_the_session_the_app_opened_starts_no_second_run(monkeypatch):
    # Linear announces it like an automation's session: a `created` event with no creator.
    client = _FakeClient()
    created = _event("agent_session_created_delegation")
    assert isinstance(created, SessionCreated)
    created = SessionCreated(**{**created.__dict__, "creator": None})
    posted: list[str] = []

    async def post_activity(session_id, content, **_):
        posted.append(content["type"])

    monkeypatch.setattr(worker, "get_client", lambda: client)
    monkeypatch.setattr(worker, "post_activity", post_activity)
    await outside._mark_own_session(client, created.issue.id)

    await worker.process_linear_event(created)

    assert posted == []
