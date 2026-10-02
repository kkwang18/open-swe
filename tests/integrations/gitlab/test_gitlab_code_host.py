import asyncio
import sys
from typing import Any

import httpx
import pytest
from langgraph_sdk.errors import NotFoundError

import agent.tools.open_pull_request  # noqa: F401
from agent.integrations.gitlab import access, sandbox
from agent.middleware import sandbox_circuit_breaker
from agent.run_config import RunConfig
from agent.source_context import GitLabRef

# `agent.tools` re-exports the tool under the module's name.
tool = sys.modules["agent.tools.open_pull_request"]
HOST = "gitlab.example.com"
THREAD = "gitlab-thread"
REF = GitLabRef(host=HOST, project_id=42, project_path="acme/tools/widgets", kind="issue", iid=7)


class _Threads:
    async def get(self, thread_id: str) -> dict[str, object]:
        if thread_id != THREAD:
            response = httpx.Response(404, request=httpx.Request("GET", "http://langgraph"))
            raise NotFoundError("missing", response=response, body=None)
        # Ada asked from GitLab on this thread; the dashboard made Mallory a participant only.
        return {
            "thread_id": thread_id,
            "metadata": {
                access.REQUESTERS_KEY: {"ada": True},
                "participant_logins": {"ada": True, "mallory": True},
            },
        }


class _Client:
    threads = _Threads()


GITLAB_REPO: dict[str, Any] = {
    "owner": "acme/tools",
    "name": "widgets",
    "host": "gitlab",
    "project_id": 42,
}


def _run(
    login: str | None,
    thread_id: str = THREAD,
    ref: GitLabRef | None = REF,
    repo: dict[str, Any] | None = GITLAB_REPO,
    source: str = "gitlab",
) -> RunConfig:
    return RunConfig.model_validate(
        {
            "thread_id": thread_id,
            "github_login": login,
            "gitlab": ref,
            "source": source,
            "repo": repo,
        }
    )


class _Person:
    def __init__(self, gitlab_user_id: str) -> None:
        self.gitlab_user_id = gitlab_user_id


# Open SWE login -> linked GitLab user id; GitLab user id -> access level on project 42.
LINKED = {"grace": "101", "reporter": "102", "flaky": "103"}
ROLES = {101: 30, 102: 20}


@pytest.fixture(autouse=True)
def gitlab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("GITLAB_URL", f"https://{HOST}")
    monkeypatch.setattr(access, "get_client", lambda: _Client())
    monkeypatch.setattr(access, "_roles", {})

    async def for_login(provider: str, login: str) -> _Person | None:
        linked = LINKED.get(login)
        return _Person(linked) if provider == "github" and linked else None

    async def access_level(project_id: int, user_id: int) -> int:
        if user_id not in ROLES:
            raise access.GitLabAPIError(503, "unavailable")
        return ROLES[user_id] if project_id == 42 else 0

    monkeypatch.setattr(access.User, "for_login", for_login)
    monkeypatch.setattr(access, "access_level", access_level)


@pytest.mark.parametrize(
    ("run", "allowed"),
    [
        (_run(None), True),
        (_run("Ada"), True),
        (_run("mallory"), False),
        (_run("ada", thread_id="missing"), False),
        (_run(None, repo={"owner": "acme", "name": "widgets"}), False),
        (_run(None, ref=None, source="slack"), False),
        (_run("ada", ref=None, source="slack"), True),
        (_run("grace", ref=None, source="slack"), True),
        (_run("reporter", ref=None, source="linear"), False),
        (_run("flaky", ref=None, source="dashboard"), False),
    ],
    ids=[
        "started from GitLab",
        "asked from GitLab before",
        "dashboard only",
        "no thread",
        "GitHub repository",
        "no sender, not from GitLab",
        "from Slack, asked from GitLab before",
        "linked Developer",
        "linked Reporter",
        "role check fails",
    ],
)
def test_only_senders_with_developer_access_on_gitlab_may_act_there(
    run: RunConfig, allowed: bool
) -> None:
    assert asyncio.run(access.gitlab_access_allowed(run)) is allowed


@pytest.mark.parametrize(
    ("login", "reason"),
    [
        ("mallory", "not linked"),
        ("reporter", "does not have Developer access"),
        ("flaky", "could not check"),
    ],
)
def test_a_refused_run_tells_the_person_why(login: str, reason: str) -> None:
    refusal = asyncio.run(access.gitlab_access_refusal(_run(login, ref=None, source="slack")))

    assert refusal is not None and reason in refusal and "acme/tools/widgets" in refusal


def test_the_sandbox_gets_gitlab_credentials_only_during_runs_allowed_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
    monkeypatch.setenv("GITLAB_URL", f"https://{HOST}")

    def rule_during(run: RunConfig | None) -> dict[str, object] | None:
        monkeypatch.setattr(sandbox, "_current_run", lambda: run)
        return asyncio.run(sandbox.gitlab_proxy_rule(THREAD))

    rule = rule_during(_run(None))
    assert rule is not None and rule["match_hosts"] == [HOST]
    assert rule_during(_run("mallory")) is None
    assert rule_during(None) is None
    assert rule_during(_run(None, thread_id="another-thread")) is None
    monkeypatch.delenv("GITLAB_TOKEN")
    assert rule_during(_run(None)) is None


@pytest.mark.parametrize(
    ("owner", "repo", "run", "opens"),
    [
        ("acme/tools", "widgets", _run(None), "merge_request"),
        (f"{HOST}/acme/tools", "widgets", _run(None), "merge_request"),
        ("acme/tools", "widgets", _run("ada", ref=None, source="dashboard"), "merge_request"),
        ("acme/tools", "widgets", _run("mallory"), None),
        ("acme", "widgets", _run("mallory"), "pull_request"),
    ],
    ids=[
        "GitLab project",
        "host-qualified owner",
        "picked on the dashboard",
        "GitLab project, no access",
        "GitHub repo",
    ],
)
def test_only_the_runs_gitlab_project_opens_a_merge_request(
    monkeypatch: pytest.MonkeyPatch, owner: str, repo: str, run: RunConfig, opens: str | None
) -> None:
    opened: list[str] = []

    async def merge_request(*_: object, **__: object) -> dict[str, Any]:
        opened.append("merge_request")
        return {"success": True}

    async def pull_request(**_: object) -> dict[str, Any]:
        opened.append("pull_request")
        return {"success": True}

    monkeypatch.setattr(tool, "_configurable", lambda: run)
    monkeypatch.setattr(tool, "open_merge_request", merge_request)
    monkeypatch.setattr(tool, "_open_pull_request", pull_request)

    result = asyncio.run(
        tool.open_pull_request(
            owner=owner, repo=repo, head="open-swe/x", base="main", title="T", body="B"
        )
    )

    assert opened == ([opens] if opens else [])
    assert result["success"] is (opens is not None)


@pytest.mark.parametrize(("login", "posted"), [(None, True), ("mallory", False)])
def test_a_sandbox_outage_is_reported_on_gitlab_when_the_run_may_post_there(
    monkeypatch: pytest.MonkeyPatch, login: str | None, posted: bool
) -> None:
    notes: list[str] = []

    async def post_gitlab_failure(ref: GitLabRef, text: str) -> bool:
        notes.append(text)
        return True

    monkeypatch.setattr(sandbox_circuit_breaker, "post_gitlab_failure", post_gitlab_failure)
    config = {"configurable": _run(login).model_dump(mode="json", exclude_none=True)}

    asyncio.run(sandbox_circuit_breaker.post_sandbox_unreachable_notification(config))

    assert bool(notes) is posted


def test_the_dashboard_offers_only_projects_the_person_may_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.integrations.gitlab import projects
    from agent.integrations.gitlab.client import BotProject

    async def bot_projects() -> list[BotProject]:
        return [
            BotProject(id=42, path_with_namespace="acme/tools/widgets", visibility="private"),
            BotProject(id=7, path_with_namespace="acme/secret"),
        ]

    monkeypatch.setattr(projects, "bot_projects", bot_projects)

    offered = asyncio.run(projects.pickable_projects("grace"))

    # Grace is a Developer on project 42 only; an unlinked person is offered nothing.
    assert offered == [
        {"full_name": f"{HOST}/acme/tools/widgets", "private": True, "archived": False}
    ]
    assert asyncio.run(projects.pickable_projects("mallory")) == []


@pytest.mark.parametrize(
    ("login", "status"), [("grace", None), ("mallory", 403), ("reporter", 403), ("flaky", 503)]
)
def test_dashboard_access_to_a_gitlab_project_follows_the_linked_account(
    monkeypatch: pytest.MonkeyPatch, login: str, status: int | None
) -> None:
    from fastapi import HTTPException

    from agent.dashboard.repo_access import require_repo_access_for_user

    async def project(path: str) -> Any:
        return type("Project", (), {"id": 42})()

    monkeypatch.setattr(access, "get_project", project)
    check = require_repo_access_for_user(login, f"{HOST}/acme/tools/widgets")

    if status is None:
        assert asyncio.run(check) == ""
    else:
        with pytest.raises(HTTPException) as refused:
            asyncio.run(check)
        assert refused.value.status_code == status
