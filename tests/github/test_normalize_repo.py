import pytest

from agent.review.styles import normalize_repo_full_name


def test_normalize_repo_full_name_accepts_urls() -> None:
    assert normalize_repo_full_name("https://github.com/langchain-ai/langgraph") == (
        "langchain-ai/langgraph"
    )


def test_normalize_repo_full_name_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        normalize_repo_full_name("not-a-repo")


def test_normalize_repo_full_name_names_gitlab_projects_by_host_only_when_gitlab_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = "https://gitlab.example.com/acme/tools/widgets.git"
    with pytest.raises(ValueError):
        normalize_repo_full_name(project)

    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.com")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "secret")

    # Groups nest, and the host keeps the name apart from any GitHub repository.
    assert normalize_repo_full_name(project) == "gitlab.example.com/acme/tools/widgets"
    assert normalize_repo_full_name("acme/widgets") == "acme/widgets"
    with pytest.raises(ValueError):
        normalize_repo_full_name("gitlab.example.com/acme/tools/widgets/extra/../x y")
