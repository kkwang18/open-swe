"""Utilities for extracting repository configuration from text."""

import re

from agent.config import ENV
from agent.integrations.gitlab.client import gitlab_configured, gitlab_host
from agent.integrations.gitlab.refs import gitlab_repo_config

_DEFAULT_REPO_OWNER = ENV.DEFAULT_REPO_OWNER.get()


def extract_repo_from_text(text: str, default_owner: str | None = None) -> dict[str, str] | None:
    """Extract owner/name repo config from text containing repo: syntax or GitHub URLs.

    Checks for explicit ``repo:owner/name`` or ``repo owner/name`` first, then
    falls back to GitHub URL extraction.

    Returns:
        A dict with ``owner`` and ``name`` keys, or ``None`` if no repo found.
    """
    if default_owner is None:
        default_owner = _DEFAULT_REPO_OWNER
    owner: str | None = None
    name: str | None = None

    if "repo:" in text or "repo " in text:
        match = re.search(r"repo[: ]([a-zA-Z0-9_.\-/]+)", text)
        if match:
            value = match.group(1).rstrip("/")
            if (gitlab := gitlab_repo_config(value)) is not None:
                return gitlab
            if "/" in value:
                owner, name = value.split("/", 1)
            else:
                owner = default_owner
                name = value

    if (not owner or not name) and (gitlab := _gitlab_project_in(text)) is not None:
        return gitlab

    if not owner or not name:
        github_match = re.search(r"github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", text)
        if github_match:
            owner, name = github_match.group(1).split("/", 1)

    if owner and name:
        return {"owner": owner, "name": name}
    return None


def _gitlab_project_in(text: str) -> dict[str, str] | None:
    """The GitLab project a link in ``text`` points into, such as an issue or merge request."""
    if not gitlab_configured():
        return None
    # A project's own pages sit under `/-/`, so the path before it is the project.
    match = re.search(
        rf"{re.escape(gitlab_host())}/([a-zA-Z0-9_.\-/]+?)(?:/-/|\.git\b|/?(?=[\s)>\]]|$))", text
    )
    return gitlab_repo_config(f"{gitlab_host()}/{match.group(1)}") if match else None


def gitlab_repo_from_text(text: str) -> dict[str, str] | None:
    """A GitLab project the text names, by ``repo:`` token or link; GitHub names are ignored."""
    match = re.search(r"repo[: ]([a-zA-Z0-9_.\-/]+)", text)
    if match and (gitlab := gitlab_repo_config(match.group(1).rstrip("/"))) is not None:
        return gitlab
    return _gitlab_project_in(text)
