"""Linear webhook deliveries the agent app acts on, parsed from Linear's payloads."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

AgentSignal = Literal["stop", "continue", "auth", "select"]


class LinearUser(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str = ""
    email: str = ""


class LinearIssue(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str
    identifier: str
    title: str = ""
    description: str = ""
    url: str = ""
    team_id: str = Field("", alias="teamId")


@dataclass(frozen=True)
class SessionCreated:
    """Someone delegated an issue to the app or mentioned it."""

    delivery_id: str
    created_at: datetime
    session_id: str
    issue: LinearIssue
    # Unset when an automation, such as a triage rule, started the session.
    creator: LinearUser | None
    # A mention carries the request in its comment; a delegation's comment is
    # Linear's own placeholder, so the issue itself is the request.
    from_mention: bool
    comment_id: str | None
    comment_body: str
    prompt_context: str
    guidance: str


@dataclass(frozen=True)
class SessionPrompted:
    """A message in an existing session; Stop arrives this way with ``signal == "stop"``."""

    delivery_id: str
    created_at: datetime
    session_id: str
    issue: LinearIssue | None
    activity_id: str
    body: str
    author: LinearUser
    signal: AgentSignal | None
    # Linear sends guidance when a session starts; empty means use what it sent then.
    guidance: str = ""


@dataclass(frozen=True)
class DelegationRemoved:
    """An issue's delegate was cleared; Linear leaves the app's open sessions running."""

    delivery_id: str
    created_at: datetime
    issue: LinearIssue
    previous_delegate_id: str


@dataclass(frozen=True)
class IssueCompleted:
    """An issue moved to a completed status, such as when Linear closes it on merge."""

    delivery_id: str
    created_at: datetime
    issue: LinearIssue


LinearEvent = SessionCreated | SessionPrompted | DelegationRemoved | IssueCompleted


class Envelope(BaseModel):
    type: str = ""
    action: str = ""
    created_at: datetime | None = Field(None, alias="createdAt")
    webhook_timestamp: int = Field(alias="webhookTimestamp")


class _Comment(BaseModel):
    body: str = ""


class _SourceMetadata(BaseModel):
    type: str = ""


class _AgentSession(BaseModel):
    id: str
    issue: LinearIssue | None = None
    creator: LinearUser | None = None
    comment_id: str | None = Field(None, alias="commentId")
    comment: _Comment | None = None
    source_metadata: _SourceMetadata | None = Field(None, alias="sourceMetadata")


class _ActivityContent(BaseModel):
    body: str = ""


class _AgentActivity(BaseModel):
    id: str
    content: _ActivityContent
    signal: AgentSignal | None = None
    user: LinearUser


class _GuidanceTeam(BaseModel):
    name: str = ""


class _GuidanceOrigin(BaseModel):
    type: str = ""
    team: _GuidanceTeam | None = None


class GuidanceRule(BaseModel):
    """Instructions for agents set on the workspace or a team."""

    body: str
    origin: _GuidanceOrigin


class AgentSessionPayload(BaseModel):
    agent_session: _AgentSession = Field(alias="agentSession")
    agent_activity: _AgentActivity | None = Field(None, alias="agentActivity")
    prompt_context: str = Field("", alias="promptContext")
    guidance: list[GuidanceRule] | None = None


class _IssueState(BaseModel):
    type: str = ""


class _IssueData(LinearIssue):
    delegate_id: str | None = Field(None, alias="delegateId")
    state: _IssueState | None = None


class IssueUpdatePayload(BaseModel):
    data: _IssueData
    updated_from: dict[str, JsonValue] = Field(default_factory=dict, alias="updatedFrom")
