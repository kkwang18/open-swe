"""The GitLab project a run works on, whichever surface started it."""

from agent.integrations.gitlab.client import gitlab_configured, gitlab_host, gitlab_url
from agent.run_config import RunConfig
from agent.source_context import GitLabRef


def run_gitlab_project(cfg: RunConfig) -> GitLabRef | None:
    """The run's repository when it is a GitLab project, else ``None``.

    Started from GitLab, it also names the issue or merge request the run
    answers on; started anywhere else, only the project.
    """
    repo = cfg.repo
    extras = (repo.model_extra or {}) if repo is not None else {}
    if repo is None or extras.get("host") != "gitlab" or not gitlab_configured():
        return None
    path = f"{repo.owner}/{repo.name}"
    raw_id = extras.get("project_id")
    project_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
    item = cfg.gitlab
    if item is not None and item.project_path.lower() == path.lower():
        return (
            item
            if item.project_id is not None
            else item.model_copy(update={"project_id": project_id})
        )
    return GitLabRef(
        host=gitlab_host(),
        project_id=project_id,
        project_path=path,
        project_url=f"{gitlab_url()}/{path}",
    )


def names_project(project: GitLabRef, owner: str, repo: str) -> bool:
    """Whether ``owner/repo`` names the project; ``owner`` may carry the GitLab host."""
    host_prefix = f"{project.host or gitlab_host()}/".lower()
    namespace = owner.strip().strip("/")
    if namespace.lower().startswith(host_prefix):
        namespace = namespace[len(host_prefix) :]
    return f"{namespace}/{repo.strip()}".lower() == project.project_path.lower()
