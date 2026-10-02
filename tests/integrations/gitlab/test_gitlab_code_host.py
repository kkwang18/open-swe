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


def _run(login: str | None, thread_id: str = THREAD, ref: GitLabRef | None = REF) -> RunConfig:
    return RunConfig(thread_id=thread_id, github_login=login, gitlab=ref, source="gitlab")


@pytest.fixture(autouse=True)
def threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access, "get_client", lambda: _Client())


@pytest.mark.parametrize(
    ("run", "allowed"),
    [
        (_run(None), True),
        (_run("Ada"), True),
        (_run("mallory"), False),
        (_run("ada", thread_id="missing"), False),
        (_run(None, ref=None), False),
    ],
    ids=[
        "started from GitLab",
        "asked from GitLab before",
        "dashboard only",
        "no thread",
        "not GitLab",
    ],
)
def test_only_runs_whose_sender_asked_from_gitlab_may_act_on_gitlab(
    run: RunConfig, allowed: bool
) -> None:
    assert asyncio.run(access.gitlab_access_allowed(run)) is allowed


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
    ("owner", "repo", "login", "opens"),
    [
        ("acme/tools", "widgets", None, "merge_request"),
        ("acme/tools", "widgets", "mallory", None),
        ("acme", "widgets", "mallory", "pull_request"),
    ],
    ids=["GitLab project", "GitLab project, no access", "GitHub repo"],
)
def test_only_the_runs_gitlab_project_opens_a_merge_request(
    monkeypatch: pytest.MonkeyPatch, owner: str, repo: str, login: str | None, opens: str | None
) -> None:
    opened: list[str] = []

    async def merge_request(*_: object, **__: object) -> dict[str, Any]:
        opened.append("merge_request")
        return {"success": True}

    async def pull_request(**_: object) -> dict[str, Any]:
        opened.append("pull_request")
        return {"success": True}

    monkeypatch.setattr(tool, "_configurable", lambda: _run(login))
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
