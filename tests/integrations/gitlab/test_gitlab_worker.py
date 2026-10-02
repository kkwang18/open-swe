import asyncio
from typing import Any

import pytest

from agent.integrations.gitlab import worker
from agent.integrations.gitlab.client import GitLabAPIError
from agent.integrations.gitlab.events import (
    GitLabMergeRequest,
    GitLabProject,
    GitLabUser,
    Mentioned,
)
from agent.integrations.gitlab.merge_requests import thread_marker
from agent.thread_ids import gitlab_merge_request_thread_id
from agent.webhooks import common

HOST = "gitlab.example.com"
PROJECT = GitLabProject(
    id=42,
    path_with_namespace="acme/tools/widgets",
    web_url=f"https://{HOST}/acme/tools/widgets",
    default_branch="main",
)
OPENING_THREAD = "11111111-2222-3333-4444-555555555555"


def _mention(description: str = "") -> Mentioned:
    return Mentioned(
        delivery_id="delivery-1",
        project=PROJECT,
        author=GitLabUser(id=7, username="ada", name="Ada"),
        kind="merge_request",
        iid=4,
        title="Fix flaky test",
        description=description,
        note_id=1243,
        note_body="@open-swe-bot please also update the docs",
        discussion_id="abc123",
        url=f"https://{HOST}/acme/tools/widgets/-/merge_requests/4#note_1243",
        merge_request=GitLabMergeRequest(
            iid=4, source_branch="open-swe/fix", target_branch="main", source_project_id=42
        ),
    )


class _GitLab:
    def __init__(self, access_level: int) -> None:
        self.access = access_level
        self.notes: list[tuple[str, str]] = []
        self.reactions: list[int | None] = []
        self.email = ""

    async def public_email(self, user_id: int) -> str:
        if self.email == "unavailable":
            raise GitLabAPIError(500, "down")
        return self.email

    async def access_level(self, project_id: int, user_id: int) -> int:
        return self.access

    async def post_note(
        self, project_id: int, kind: str, iid: int, body: str, *, discussion_id: str = ""
    ) -> int:
        self.notes.append((discussion_id, body))
        return 1

    async def award_emoji(
        self, project_id: int, kind: str, iid: int, *, note_id: int | None, name: str = "eyes"
    ) -> None:
        self.reactions.append(note_id)


@pytest.fixture
def gitlab(monkeypatch: pytest.MonkeyPatch) -> _GitLab:
    fake = _GitLab(access_level=30)
    monkeypatch.setattr(worker, "access_level", fake.access_level)
    monkeypatch.setattr(worker, "post_note", fake.post_note)
    monkeypatch.setattr(worker, "award_emoji", fake.award_emoji)
    monkeypatch.setattr(worker, "public_email", fake.public_email)
    monkeypatch.setattr(worker, "gitlab_host", lambda: HOST)
    return fake


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    runs: list[tuple[str, dict[str, Any]]] = []
    thread_metadata = {
        OPENING_THREAD: {"source_context": {"gitlab": {"host": HOST, "project_id": 42}}},
    }

    async def dispatch_agent_run(
        thread_id: str, content: object, configurable: dict[str, Any], **_: object
    ) -> dict[str, str]:
        runs.append((thread_id, configurable))
        return {"run_id": "run-1"}

    async def nothing(*_: object, **__: object) -> None:
        return None

    async def metadata(thread_id: str) -> dict[str, object] | None:
        return thread_metadata.get(thread_id)

    async def exists(thread_id: str) -> bool:
        return True

    async def default_workspace(_repo: object) -> str:
        return "default"

    monkeypatch.setattr(common, "dispatch_agent_run", dispatch_agent_run)
    monkeypatch.setattr(common, "upsert_agent_thread_metadata", nothing)
    monkeypatch.setattr(common, "get_thread_workspace", nothing)
    monkeypatch.setattr(common, "workspace_for_repo_config", default_workspace)
    monkeypatch.setattr(common, "get_thread_metadata_safe", metadata)
    monkeypatch.setattr(common, "thread_exists", exists)
    return runs


def test_someone_without_developer_access_gets_a_reply_and_no_run(
    gitlab: _GitLab, dispatched: list[tuple[str, dict[str, Any]]]
) -> None:
    gitlab.access = 20  # Reporter

    asyncio.run(worker.process_gitlab_event(_mention()))

    assert dispatched == []
    assert gitlab.reactions == []
    assert gitlab.notes == [("abc123", worker.DENIED_TEXT)]


def test_a_mention_on_an_open_swe_merge_request_continues_the_thread_that_opened_it(
    gitlab: _GitLab, dispatched: list[tuple[str, dict[str, Any]]]
) -> None:
    asyncio.run(
        worker.process_gitlab_event(_mention(f"Fixes it.\n\n{thread_marker(OPENING_THREAD)}"))
    )

    [(thread_id, configurable)] = dispatched
    assert thread_id == OPENING_THREAD
    assert gitlab.reactions == [1243]
    assert configurable["source"] == "gitlab"
    assert configurable["repo"] == {
        "owner": "acme/tools",
        "name": "widgets",
        "host": "gitlab",
        "project_id": 42,
    }
    assert configurable["gitlab"]["discussion_id"] == "abc123"


def test_a_marker_naming_a_thread_on_another_project_is_not_followed(
    gitlab: _GitLab, dispatched: list[tuple[str, dict[str, Any]]]
) -> None:
    # The description is editable text: someone could paste another project's thread id.
    other = "99999999-2222-3333-4444-555555555555"

    asyncio.run(worker.process_gitlab_event(_mention(thread_marker(other))))

    [(thread_id, _)] = dispatched
    assert thread_id == gitlab_merge_request_thread_id(HOST, 42, 4)


class _LinkedPerson:
    github_login = "grace"


@pytest.mark.parametrize(
    ("linked", "email", "listed_for", "requester"),
    [
        (True, "", "grace", ["grace"]),
        (False, "ada@acme.com", "ada", ["ada"]),
        (False, "", "", []),
        (False, "unavailable", "", []),
    ],
    ids=["linked account", "matching public email", "no email", "email lookup fails"],
)
def test_the_requesters_open_swe_account_is_listed_and_may_follow_up(
    gitlab: _GitLab,
    dispatched: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    linked: bool,
    email: str,
    listed_for: str,
    requester: list[str],
) -> None:
    gitlab.email = email
    listed: list[str] = []
    recorded: list[str] = []

    async def upsert(
        *_: object, github_login: str = "", user_email: str = "", **__: object
    ) -> None:
        listed.append(github_login or user_email)

    async def for_identity(provider: str, external_id: str) -> _LinkedPerson | None:
        return _LinkedPerson() if linked and (provider, external_id) == ("gitlab", "7") else None

    async def login_for_email(address: str) -> str | None:
        return "ada" if address == "ada@acme.com" else None

    async def record_requester(thread_id: str, login: str) -> None:
        recorded.append(login)

    monkeypatch.setattr(common, "upsert_agent_thread_metadata", upsert)
    monkeypatch.setattr(worker.User, "for_identity", for_identity)
    monkeypatch.setattr(worker.User, "login_for_email", login_for_email)
    monkeypatch.setattr(worker, "record_requester", record_requester)

    asyncio.run(worker.process_gitlab_event(_mention()))

    assert listed == [listed_for]
    assert recorded == requester
    [(_, configurable)] = dispatched
    # The run itself still acts as the bot: no sender identity is added to it.
    assert "user_email" not in configurable
    assert "github_login" not in configurable
