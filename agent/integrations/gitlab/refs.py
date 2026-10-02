"""How a GitLab project is named wherever Open SWE names repositories.

GitHub repositories stay ``owner/name``. A GitLab project is its path prefixed
with the GitLab host, ``gitlab.com/group/sub/project``: groups nest, so the path
has two or more parts, and the host keeps it apart from any GitHub name.
"""

import re
from collections.abc import Mapping

from agent.integrations.gitlab.client import gitlab_configured, gitlab_host

_SCHEMES = ("https://", "http://")
# GitLab group and project paths: letters, digits, `_`, `-` and `.`, not starting with
# `-` or `.`; so no `..`, spaces or URL syntax reach a key or a clone URL.
_PATH_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def gitlab_project_path(raw: str) -> str | None:
    """``group/sub/project`` when ``raw`` names a project on the configured GitLab host."""
    if not gitlab_configured():
        return None
    value = raw.strip()
    for scheme in _SCHEMES:
        if value.lower().startswith(scheme):
            value = value[len(scheme) :]
    value = value.strip("/").removesuffix(".git")
    host, _, path = value.partition("/")
    if host.lower() != gitlab_host().lower():
        return None
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or not all(_PATH_PART.fullmatch(part) for part in parts):
        return None
    return "/".join(parts)


def gitlab_full_name(path: str) -> str:
    return f"{gitlab_host()}/{path}"


def is_gitlab_repo(repo: Mapping[str, object] | None) -> bool:
    """Whether a run's ``repo`` config is a GitLab project."""
    return repo is not None and repo.get("host") == "gitlab"


def repo_full_name(repo: Mapping[str, object]) -> str:
    """The repository's name as workspaces and the dashboard list it."""
    path = f"{repo.get('owner', '')}/{repo.get('name', '')}"
    return gitlab_full_name(path) if is_gitlab_repo(repo) else path


def repo_config_for(full_name: str, project_id: int | None = None) -> dict[str, object] | None:
    """The run's ``repo`` config for a GitLab full name, or ``None`` for anything else."""
    path = gitlab_project_path(full_name)
    if path is None:
        return None
    namespace, _, name = path.rpartition("/")
    config: dict[str, object] = {"owner": namespace, "name": name, "host": "gitlab"}
    if project_id is not None:
        config["project_id"] = project_id
    return config


def gitlab_repo_config(full_name: str) -> dict[str, str] | None:
    """``{owner, name, host}`` for a GitLab full name; the project id is looked up when needed."""
    path = gitlab_project_path(full_name)
    if path is None:
        return None
    namespace, _, name = path.rpartition("/")
    return {"owner": namespace, "name": name, "host": "gitlab"}
