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


async def test_unlinked_requester_is_asked_to_link_not_matched_by_email(monkeypatch):
    async def not_guest(_user_id):
        return False

    async def no_identity(_provider, _external_id):
        return None

    monkeypatch.setattr(access, "is_guest", not_guest)
    monkeypatch.setattr(access.User, "for_identity", no_identity)
    requester = access.LinearUser(id="linear-user", email="octocat@example.com")

    asked = await access.resolve_actor(requester, "session-1", ISSUE)

    assert isinstance(asked, PendingQuestion)
    assert asked.kind == "link_account"
    assert asked.requester_id == "linear-user"
    assert asked.link_url is not None and "session=session-1" in asked.link_url


async def test_a_gitlab_project_skips_the_github_allowlist_and_explains_a_refusal(
    monkeypatch, no_defaults
):
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "secret")
    checked: list[str] = []

    async def routable(owner, name):
        checked.append(f"{owner}/{name}")
        return True

    async def no_gitlab_access(_login, _full_name):
        raise HTTPException(403, "you need Developer access to this GitLab project")

    monkeypatch.setattr(access.common, "is_repo_allowed", lambda _repo: False)
    monkeypatch.setattr(access, "repo_is_routable", routable)
    monkeypatch.setattr(access, "require_repo_access_for_user", no_gitlab_access)

    refused = await access.choose_repo(
        "repo:gitlab.example.com/acme/tools/widgets fix it", ACTOR, {}, ISSUE, "session-1"
    )

    assert checked == ["gitlab.example.com/acme/tools/widgets"]
    assert isinstance(refused, Denied)
    assert "Developer access" in refused.message and "/integrations/gitlab/link" in refused.message
