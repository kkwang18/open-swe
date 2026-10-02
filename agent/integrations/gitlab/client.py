"""GitLab REST calls made as the bot user."""

from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, TypeAdapter

from agent.config import ENV
from agent.integrations.gitlab.events import GitLabUser, NoteableKind
from agent.utils.dashboard_links import dashboard_api_base_url

DEFAULT_URL = "https://gitlab.com"
# GitLab's access levels; Developer may push to unprotected branches and open MRs.
DEVELOPER_ACCESS = 30

_NOTEABLE_PATHS: dict[NoteableKind, str] = {
    "issue": "issues",
    "merge_request": "merge_requests",
}


class GitLabAPIError(RuntimeError):
    """GitLab refused a call; the message never contains the token."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"GitLab returned {status_code}: {message}")


def gitlab_configured() -> bool:
    return ENV.GITLAB_TOKEN.is_set() and ENV.GITLAB_WEBHOOK_SECRET.is_set()


def gitlab_linking_configured() -> bool:
    """Whether people can link GitLab accounts, through the GitLab OAuth application."""
    return (
        gitlab_configured()
        and ENV.GITLAB_OAUTH_CLIENT_ID.is_set()
        and ENV.GITLAB_OAUTH_CLIENT_SECRET.is_set()
    )


LINK_PATH = "/dashboard/api/integrations/gitlab/link"


def link_url() -> str:
    """Where a person goes to link their GitLab account."""
    return f"{dashboard_api_base_url().rstrip('/')}{LINK_PATH}"


def gitlab_url() -> str:
    return (ENV.GITLAB_URL.optional() or DEFAULT_URL).rstrip("/")


def gitlab_host() -> str:
    return urlsplit(gitlab_url()).hostname or ""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{gitlab_url()}/api/v4",
        headers={"PRIVATE-TOKEN": ENV.GITLAB_TOKEN.get()},
        timeout=httpx.Timeout(15),
    )


def _raise_for(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        message = str(response.json().get("message") or response.json().get("error") or "")
    except ValueError:
        message = response.text[:200]
    raise GitLabAPIError(response.status_code, message or response.reason_phrase)


_bot_user: GitLabUser | None = None


async def bot_user() -> GitLabUser:
    """The user the token acts as; how mentions of and notes by the bot are recognised."""
    global _bot_user
    if _bot_user is None:
        async with _client() as client:
            response = await client.get("/user")
        _raise_for(response)
        _bot_user = GitLabUser.model_validate_json(response.content)
    return _bot_user


def cached_bot_user() -> GitLabUser | None:
    """The bot user once known; the webhook handler may not call GitLab."""
    return _bot_user


class _PublicProfile(BaseModel):
    public_email: str | None = None


async def public_email(user_id: int) -> str:
    """The email the user chose to show on their profile; empty when they show none."""
    async with _client() as client:
        response = await client.get(f"/users/{user_id}")
    _raise_for(response)
    return (_PublicProfile.model_validate_json(response.content).public_email or "").strip()


class _Member(BaseModel):
    access_level: int = 0


async def access_level(project_id: int, user_id: int) -> int:
    """The user's effective access level on the project, inherited ones included; 0 if none."""
    async with _client() as client:
        response = await client.get(f"/projects/{project_id}/members/all/{user_id}")
    if response.status_code == httpx.codes.NOT_FOUND:
        return 0
    _raise_for(response)
    return _Member.model_validate_json(response.content).access_level


def noteable_kind(kind: str) -> NoteableKind | None:
    if kind == "issue":
        return "issue"
    if kind == "merge_request":
        return "merge_request"
    return None


def _noteable(project_id: int, kind: NoteableKind, iid: int) -> str:
    return f"/projects/{project_id}/{_NOTEABLE_PATHS[kind]}/{iid}"


async def award_emoji(
    project_id: int, kind: NoteableKind, iid: int, *, note_id: int | None, name: str = "eyes"
) -> None:
    """React to a note, or to the issue or merge request itself without ``note_id``."""
    path = _noteable(project_id, kind, iid)
    if note_id is not None:
        path = f"{path}/notes/{note_id}"
    async with _client() as client:
        # GitLab.com answers 404 when the name comes as a query parameter.
        response = await client.post(f"{path}/award_emoji", json={"name": name})
    _raise_for(response)


class _Note(BaseModel):
    id: int


async def post_note(
    project_id: int, kind: NoteableKind, iid: int, body: str, *, discussion_id: str = ""
) -> int:
    """Post a note, in ``discussion_id`` when given; returns the note's id."""
    path = _noteable(project_id, kind, iid)
    async with _client() as client:
        if discussion_id:
            response = await client.post(
                f"{path}/discussions/{discussion_id}/notes", json={"body": body}
            )
            if response.is_success:
                return _Note.model_validate_json(response.content).id
            # A discussion that is gone or not replyable still has a place for the answer.
            if response.status_code not in {httpx.codes.NOT_FOUND, httpx.codes.BAD_REQUEST}:
                _raise_for(response)
        response = await client.post(f"{path}/notes", json={"body": body})
    _raise_for(response)
    return _Note.model_validate_json(response.content).id


class NoteSummary(BaseModel):
    id: int
    body: str = ""
    system: bool = False
    author: GitLabUser


_NOTES = TypeAdapter(list[NoteSummary])


async def recent_notes(
    project_id: int, kind: NoteableKind, iid: int, *, limit: int = 20
) -> list[NoteSummary]:
    """The latest people's notes, oldest first."""
    async with _client() as client:
        response = await client.get(
            f"{_noteable(project_id, kind, iid)}/notes",
            params={"sort": "desc", "order_by": "created_at", "per_page": limit},
        )
    _raise_for(response)
    notes = [note for note in _NOTES.validate_json(response.content) if not note.system]
    return list(reversed(notes))


class MergeRequest(BaseModel):
    iid: int
    web_url: str
    title: str = ""
    draft: bool = False


_MERGE_REQUESTS = TypeAdapter(list[MergeRequest])


async def find_open_merge_request(
    project_id: int, source_branch: str, target_branch: str
) -> MergeRequest | None:
    async with _client() as client:
        response = await client.get(
            f"/projects/{project_id}/merge_requests",
            params={
                "state": "opened",
                "source_branch": source_branch,
                "target_branch": target_branch,
            },
        )
    _raise_for(response)
    found = _MERGE_REQUESTS.validate_json(response.content)
    return found[0] if found else None


async def create_merge_request(
    project_id: int,
    *,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    draft: bool,
) -> MergeRequest:
    # GitLab marks a merge request as a draft by its title.
    full_title = title if not draft or title.lower().startswith("draft:") else f"Draft: {title}"
    async with _client() as client:
        response = await client.post(
            f"/projects/{project_id}/merge_requests",
            json={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": full_title,
                "description": description,
                "remove_source_branch": True,
            },
        )
    _raise_for(response)
    return MergeRequest.model_validate_json(response.content)


class Project(BaseModel):
    id: int
    path_with_namespace: str
    web_url: str
    default_branch: str = ""


async def get_project(project: int | str) -> Project:
    """The project by numeric id or by its full path."""
    ref = str(project).replace("/", "%2F")
    async with _client() as client:
        response = await client.get(f"/projects/{ref}")
    _raise_for(response)
    return Project.model_validate_json(response.content)


class BotProject(BaseModel):
    id: int
    path_with_namespace: str
    visibility: str = "private"
    archived: bool = False


_BOT_PROJECTS = TypeAdapter(list[BotProject])
# A picker lists a page of the bot's projects; more than that wants a search box.
MAX_BOT_PROJECTS = 100


async def bot_projects() -> list[BotProject]:
    """The projects the bot is a member of, most recently active first."""
    async with _client() as client:
        response = await client.get(
            "/projects",
            params={
                "membership": "true",
                "archived": "false",
                "order_by": "last_activity_at",
                "per_page": MAX_BOT_PROJECTS,
                "simple": "true",
            },
        )
    _raise_for(response)
    return _BOT_PROJECTS.validate_json(response.content)
