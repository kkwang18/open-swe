from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent.integrations.base import Actor, Denied, PendingQuestion, SelectOption
from agent.integrations.linear import access
from agent.integrations.linear.events import LinearIssue

ACTOR = Actor(provider_user_id="linear-user", github_login="octocat")
ISSUE = LinearIssue(id="issue-1", identifier="ENG-1")
OPTIONS = (
    SelectOption(value="acme/web", label="acme/web"),
    SelectOption(value="acme/api", label="acme/api"),
    SelectOption(value="acme/api-docs", label="acme/api-docs"),
)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("acme/api", "acme/api"),
        ("the second one", "acme/api"),
        ("3", "acme/api-docs"),
        ("web please", "acme/web"),
        ("api", None),
        ("something else", None),
    ],
)
def test_free_text_answers_resolve_only_when_unambiguous(answer, expected):
    choice = access.interpret_answer(OPTIONS, answer)
    assert (choice.value if choice else None) == expected


@pytest.fixture
def no_defaults(monkeypatch):
    async def none(*_args, **_kwargs):
        return None

    async def settings():
        return SimpleNamespace(default_repo=None)

    async def workspace(_slug):
        return SimpleNamespace(repos=["acme/web", "acme/api"])

    monkeypatch.setattr(access.common, "get_profile_default_repo", none)
    monkeypatch.setattr(access.common, "get_workspace_settings", settings)
    monkeypatch.setattr(access.common, "is_repo_allowed", lambda _repo: True)
    monkeypatch.setattr(access, "load_workspace", workspace)


async def test_ambiguous_repo_asks_the_requester(monkeypatch, no_defaults):
    async def unsure(*_args):
        return [("acme/api", 0.4), ("acme/web", 0.3)]

    monkeypatch.setattr(access, "suggest_repositories", unsure)

    question = await access.choose_repo("fix the login bug", ACTOR, {}, ISSUE, "session-1")

    assert isinstance(question, PendingQuestion)
    assert question.requester_id == ACTOR.provider_user_id
    assert [option.value for option in question.options] == ["acme/web", "acme/api"]


async def test_repo_the_requester_cannot_access_is_refused(monkeypatch, no_defaults):
    async def routable(_owner, _name):
        return True

    async def no_access(_login, _full_name):
        raise HTTPException(404, "repository not found")

    monkeypatch.setattr(access, "repo_is_routable", routable)
    monkeypatch.setattr(access, "require_repo_access_for_user", no_access)

    refused = await access.choose_repo("repo:acme/secret fix it", ACTOR, {}, ISSUE, "session-1")

    assert isinstance(refused, Denied)
    assert "acme/secret" in refused.message
