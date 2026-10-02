import asyncio
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from langgraph_sdk.errors import ConflictError, NotFoundError

from agent.integrations.base import Actor
from agent.integrations.linear import worker
from agent.integrations.linear.client import linear_activity_id
from agent.integrations.linear.events import SessionCreated, SessionPrompted
from agent.integrations.linear.integration import LinearIntegration

FIXTURES = Path(__file__).with_name("fixtures")


def _event(name: str) -> SessionCreated | SessionPrompted:
    raw = json.loads((FIXTURES / f"{name}.json").read_text())
    now = datetime.fromisoformat(raw["createdAt"])
    event = LinearIntegration(now=lambda: now)._event(f"delivery-{name}", json.dumps(raw).encode())
    assert isinstance(event, SessionCreated | SessionPrompted)
    return event


class _FakeThreads:
    def __init__(self) -> None:
        self.metadata: dict[str, dict[str, object]] = {}

    @property
    def ids(self) -> set[str]:
        return set(self.metadata)

    async def create(
        self,
        *,
        thread_id: str,
        if_exists: str,
        ttl: int | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if thread_id in self.metadata:
            if if_exists == "do_nothing":
                return
            response = httpx.Response(409, request=httpx.Request("POST", "http://langgraph"))
            raise ConflictError("exists", response=response, body=None)
        self.metadata[thread_id] = dict(metadata or {})

    async def update(self, *, thread_id: str, metadata: dict[str, object]) -> None:
        self.metadata[thread_id].update(metadata)

    async def get(self, thread_id: str) -> dict[str, object]:
        if thread_id not in self.metadata:
            response = httpx.Response(404, request=httpx.Request("GET", "http://langgraph"))
            raise NotFoundError("missing", response=response, body=None)
        return {"thread_id": thread_id, "metadata": self.metadata[thread_id], "status": "idle"}


class _FakeRuns:
    def __init__(self, threads: _FakeThreads) -> None:
        self._threads = threads

    async def list(
        self, thread_id: str, *, status: str | None, limit: int
    ) -> list[dict[str, object]]:
        # As on the server: a thread exists only once something has created it.
        if thread_id not in self._threads.ids:
            response = httpx.Response(404, request=httpx.Request("GET", "http://langgraph"))
            raise NotFoundError("missing", response=response, body=None)
        return []


class _FakeClient:
    def __init__(self) -> None:
        self.threads = _FakeThreads()
        self.runs = _FakeRuns(self.threads)


async def test_stop_before_dispatch_prevents_the_run(monkeypatch):
    client = _FakeClient()
    posted: list[tuple[str, dict[str, object]]] = []
    dispatched: list[str] = []

    async def post_activity(session_id, content, **_):
        posted.append((session_id, content))

    async def process_linear_issue(issue_data, repo, *, linear_session):
        dispatched.append(linear_session.id)

    monkeypatch.setattr(worker, "get_client", lambda: client)
    monkeypatch.setattr(worker, "post_activity", post_activity)
    monkeypatch.setattr(worker, "process_linear_issue", process_linear_issue)
    monkeypatch.setattr(worker, "dashboard_thread_url", lambda _thread_id: None)

    created = _event("agent_session_created_delegation")
    stop = _event("agent_session_prompted_stop")
    stop = SessionPrompted(**{**stop.__dict__, "session_id": created.session_id})

    await worker.process_linear_event(stop)
    await worker.process_linear_event(created)

    assert dispatched == []
    assert [content["type"] for session, content in posted if session == created.session_id] == [
        "response",
        "thought",
        "response",
    ]


async def test_first_request_on_a_new_issue_starts_a_run(monkeypatch):
    client = _FakeClient()
    dispatched: list[str] = []

    async def post_activity(session_id, content, **_):
        return None

    async def requester(author, issue):
        return author

    async def resolve_actor(user, session_id, issue):
        return Actor(provider_user_id=user.id, github_login="octocat")

    async def choose_repo(request, actor, metadata, issue, session_id):
        return {"owner": "octocat", "name": "hello-world"}

    async def process_linear_issue(issue_data, repo, *, linear_session, **_):
        dispatched.append(linear_session.id)

    async def start_delegated_issue(issue_id):
        return True

    monkeypatch.setattr(worker, "get_client", lambda: client)
    monkeypatch.setattr(worker, "post_activity", post_activity)
    monkeypatch.setattr(worker, "dashboard_thread_url", lambda _thread_id: None)
    monkeypatch.setattr(worker, "requester", requester)
    monkeypatch.setattr(worker, "resolve_actor", resolve_actor)
    monkeypatch.setattr(worker, "choose_repo", choose_repo)
    monkeypatch.setattr(worker, "process_linear_issue", process_linear_issue)
    monkeypatch.setattr(worker, "start_delegated_issue", start_delegated_issue)

    created = _event("agent_session_created_delegation")
    await worker.process_linear_event(created)

    assert dispatched == [created.session_id]


def _dispatching_worker(monkeypatch, client, on_authorize=None):
    """Patch the worker so a request authorizes and dispatches without Linear or GitHub."""
    dispatched: list[str] = []

    async def post_activity(session_id, content, **_):
        return None

    async def requester(author, issue):
        return author

    async def resolve_actor(user, session_id, issue):
        if on_authorize is not None:
            await on_authorize()
        return Actor(provider_user_id=user.id, github_login="octocat")

    async def choose_repo(request, actor, metadata, issue, session_id):
        return {"owner": "octocat", "name": "hello-world"}

    async def process_linear_issue(issue_data, repo, *, linear_session, **_):
        dispatched.append(linear_session.id)

    async def start_delegated_issue(issue_id):
        return True

    monkeypatch.setattr(worker, "get_client", lambda: client)
    monkeypatch.setattr(worker, "post_activity", post_activity)
    monkeypatch.setattr(worker, "dashboard_thread_url", lambda _thread_id: None)
    monkeypatch.setattr(worker, "requester", requester)
    monkeypatch.setattr(worker, "resolve_actor", resolve_actor)
    monkeypatch.setattr(worker, "choose_repo", choose_repo)
    monkeypatch.setattr(worker, "process_linear_issue", process_linear_issue)
    monkeypatch.setattr(worker, "start_delegated_issue", start_delegated_issue)
    return dispatched


async def test_stop_while_the_request_is_being_authorized_dispatches_nothing(monkeypatch):
    client = _FakeClient()
    created = _event("agent_session_created_delegation")
    stop = _event("agent_session_prompted_stop")
    stop = SessionPrompted(
        **{**stop.__dict__, "session_id": created.session_id, "created_at": created.created_at}
    )

    async def stop_now():
        await worker.process_linear_event(stop)

    dispatched = _dispatching_worker(monkeypatch, client, on_authorize=stop_now)

    await worker.process_linear_event(created)

    assert dispatched == []


async def test_a_message_after_a_stop_still_runs(monkeypatch):
    client = _FakeClient()
    created = _event("agent_session_created_delegation")
    stop = _event("agent_session_prompted_stop")
    stop = SessionPrompted(**{**stop.__dict__, "session_id": created.session_id})
    later = _event("agent_session_prompted")
    later = SessionPrompted(
        **{
            **later.__dict__,
            "session_id": created.session_id,
            "created_at": stop.created_at + timedelta(seconds=30),
        }
    )
    dispatched = _dispatching_worker(monkeypatch, client)

    await worker.process_linear_event(stop)
    await worker.process_linear_event(later)

    assert dispatched == [created.session_id]


def test_acknowledgement_ids_are_stable_uuid4():
    first = linear_activity_id("ack", "delivery-1")
    assert first == linear_activity_id("ack", "delivery-1")
    assert uuid.UUID(first).version == 4


async def test_stop_cancels_only_its_own_sessions_run(monkeypatch):
    stop = _event("agent_session_prompted_stop")
    assert isinstance(stop, SessionPrompted)
    cancelled: list[str] = []

    def run(run_id: str, session_id: str) -> dict[str, object]:
        configurable = {"linear_session": {"id": session_id}}
        return {"run_id": run_id, "kwargs": {"config": {"configurable": configurable}}}

    class Runs:
        async def list(self, thread_id: str, *, status: str, limit: int) -> list[dict[str, object]]:
            if status != "running":
                return []
            return [run("run-mine", stop.session_id), run("run-other", "another-session")]

        async def cancel_many(self, *, thread_id: str, run_ids: list[str], action: str) -> None:
            cancelled.extend(run_ids)

    client = _FakeClient()
    client.runs = Runs()
    responses: list[str] = []

    async def post_activity(session_id, content, **_):
        responses.append(session_id)

    async def interrupt_transcript_turns(thread_id, run_ids):
        return None

    monkeypatch.setattr(worker, "get_client", lambda: client)
    monkeypatch.setattr(worker, "post_activity", post_activity)
    monkeypatch.setattr(worker, "interrupt_transcript_turns", interrupt_transcript_turns)

    await worker.process_linear_event(stop)

    assert cancelled == ["run-mine"]
    assert responses == [stop.session_id]


async def test_new_session_closes_the_session_whose_run_it_interrupts(monkeypatch):
    # The thread outlives its sessions, so the running run's config, not the thread's
    # metadata, says which session to close; a dashboard run has none.
    class Runs:
        async def list(self, thread_id: str, *, status: str, limit: int) -> list[dict[str, object]]:
            if status != "running":
                return []
            configurable = {"linear_session": {"id": "second-session"}}
            return [
                {"run_id": "run-1", "kwargs": {"config": {"configurable": configurable}}},
                {"run_id": "run-2", "kwargs": {"config": {"configurable": {}}}},
            ]

    client = _FakeClient()
    client.runs = Runs()
    closed: list[str] = []

    async def post_activity(session_id, content, **_):
        closed.append(session_id)

    monkeypatch.setattr(worker, "get_client", lambda: client)
    monkeypatch.setattr(worker, "post_activity", post_activity)

    await worker._close_superseded_sessions("thread-1", "third-session")

    assert closed == ["second-session"]


async def test_link_by_someone_else_does_not_resume_the_request(monkeypatch):
    created = _event("agent_session_created_delegation")
    assert isinstance(created, SessionCreated)
    pending = {
        "kind": "link_account",
        "session_id": created.session_id,
        "requester_id": "the-requester",
        "prompt": "Link your account",
        "request": "",
        "issue": created.issue.model_dump(),
    }

    async def thread(_thread_id):
        return {"metadata": {worker.PENDING_QUESTION_KEY: pending}}

    resumed: list[str] = []

    async def run(session_id, *_args, **_kwargs):
        resumed.append(session_id)

    async def set_pending(_thread_id, _pending):
        return None

    monkeypatch.setattr(worker, "_thread", thread)
    monkeypatch.setattr(worker, "_run", run)
    monkeypatch.setattr(worker, "_set_pending_question", set_pending)
    monkeypatch.setattr(worker, "post_activity", lambda *_a, **_k: asyncio.sleep(0))

    await worker.resume_after_link(created.session_id, created.issue.id, "someone-else")
    assert resumed == []

    await worker.resume_after_link(created.session_id, created.issue.id, "the-requester")
    assert resumed == [created.session_id]
