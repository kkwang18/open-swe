"""GitLab webhook deliveries the integration acts on, parsed from GitLab's payloads."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _empty_if_none(value: object) -> object:
    return "" if value is None else value


# GitLab sends null for text left empty, such as an issue with no description.
OptionalText = Annotated[str, BeforeValidator(_empty_if_none)]

NoteableKind = Literal["issue", "merge_request"]


class GitLabUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    username: str
    name: OptionalText = ""


class GitLabProject(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    path_with_namespace: str
    web_url: str
    default_branch: OptionalText = ""

    @property
    def namespace(self) -> str:
        return self.path_with_namespace.rpartition("/")[0]

    @property
    def path(self) -> str:
        return self.path_with_namespace.rpartition("/")[2]


class GitLabIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    iid: int
    title: OptionalText = ""
    description: OptionalText = ""


class GitLabMergeRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    iid: int
    title: OptionalText = ""
    description: OptionalText = ""
    source_branch: OptionalText = ""
    target_branch: OptionalText = ""
    source_project_id: int | None = None


class _NoteAttributes(BaseModel):
    id: int
    note: OptionalText = ""
    noteable_type: str
    system: bool = False
    discussion_id: OptionalText = ""
    url: OptionalText = ""
    # Older GitLab versions omit it; a note hook without one is a new note.
    action: OptionalText = "create"


class NotePayload(BaseModel):
    user: GitLabUser
    project: GitLabProject
    object_attributes: _NoteAttributes
    issue: GitLabIssue | None = None
    merge_request: GitLabMergeRequest | None = None


class _IssueAttributes(GitLabIssue):
    url: OptionalText = ""
    action: OptionalText = ""


class _AssigneeChange(BaseModel):
    previous: list[GitLabUser] = Field(default_factory=list)
    current: list[GitLabUser] = Field(default_factory=list)


class _IssueChanges(BaseModel):
    assignees: _AssigneeChange | None = None


class IssuePayload(BaseModel):
    user: GitLabUser
    project: GitLabProject
    object_attributes: _IssueAttributes
    changes: _IssueChanges = Field(default_factory=_IssueChanges)


class Envelope(BaseModel):
    object_kind: str


@dataclass(frozen=True)
class Mentioned:
    """Someone mentioned the bot in a note on an issue or merge request."""

    delivery_id: str
    project: GitLabProject
    author: GitLabUser
    kind: NoteableKind
    iid: int
    title: str
    description: str
    note_id: int
    note_body: str
    discussion_id: str
    url: str
    merge_request: GitLabMergeRequest | None = None


@dataclass(frozen=True)
class IssueAssigned:
    """Someone assigned the bot to an issue; the issue itself is the request."""

    delivery_id: str
    project: GitLabProject
    author: GitLabUser
    iid: int
    title: str
    description: str
    url: str


GitLabEvent = Mentioned | IssueAssigned
