"""Linear GraphQL calls made as the agent app."""

import hashlib
import uuid
from collections.abc import Mapping

import httpx
from pydantic import BaseModel, Field, JsonValue, ValidationError

from agent.integrations.linear.events import LinearUser
from agent.integrations.linear.token import linear_app_auth

GRAPHQL_URL = "https://api.linear.app/graphql"


class GraphQLErrorItem(BaseModel):
    message: str
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class _GraphQLResponse(BaseModel):
    data: dict[str, JsonValue] | None = None
    errors: list[GraphQLErrorItem] = Field(default_factory=list)


class LinearGraphQLError(RuntimeError):
    """Linear rejected the operation; it reports this with HTTP 200 and an ``errors`` list."""

    def __init__(self, errors: list[GraphQLErrorItem]) -> None:
        self.errors = errors
        super().__init__("; ".join(error.message for error in errors))


async def linear_graphql(
    query: str, variables: Mapping[str, JsonValue] | None = None
) -> dict[str, JsonValue]:
    async with httpx.AsyncClient(auth=linear_app_auth, timeout=httpx.Timeout(15)) as client:
        response = await client.post(
            GRAPHQL_URL, json={"query": query, "variables": dict(variables or {})}
        )
    try:
        result = _GraphQLResponse.model_validate_json(response.content)
    except ValidationError:
        response.raise_for_status()
        raise
    if result.errors:
        raise LinearGraphQLError(result.errors)
    response.raise_for_status()
    if result.data is None:
        raise LinearGraphQLError([GraphQLErrorItem(message="Linear returned no data")])
    return result.data


class _Viewer(BaseModel):
    id: str


class _ViewerData(BaseModel):
    viewer: _Viewer


_app_user_id: str | None = None


async def app_user_id() -> str:
    """The app user's id in this workspace; how its own activity and delegation are recognised."""
    global _app_user_id
    if _app_user_id is None:
        data = _ViewerData.model_validate(await linear_graphql("query { viewer { id } }"))
        _app_user_id = data.viewer.id
    return _app_user_id


_ACTIVITY_CREATE = """
mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) { success }
}
"""

_SESSION_UPDATE = """
mutation AgentSessionUpdate($id: String!, $input: AgentSessionUpdateInput!) {
  agentSessionUpdate(id: $id, input: $input) { success }
}
"""


async def post_activity(
    session_id: str,
    content: dict[str, JsonValue],
    *,
    activity_id: str | None = None,
    ephemeral: bool = False,
    signal: str | None = None,
    signal_metadata: dict[str, JsonValue] | None = None,
) -> None:
    """Post to a session. With ``activity_id``, a repeat is a no-op: Linear rejects the duplicate."""
    activity_input: dict[str, JsonValue] = {
        "agentSessionId": session_id,
        "content": content,
        "ephemeral": ephemeral,
    }
    if activity_id:
        activity_input["id"] = activity_id
    if signal:
        activity_input["signal"] = signal
    if signal_metadata is not None:
        activity_input["signalMetadata"] = signal_metadata
    try:
        await linear_graphql(_ACTIVITY_CREATE, {"input": activity_input})
    except LinearGraphQLError as exc:
        if activity_id and any("conflict on insert" in error.message for error in exc.errors):
            return
        raise


async def set_session_link(session_id: str, label: str, url: str) -> None:
    await linear_graphql(
        _SESSION_UPDATE,
        {"id": session_id, "input": {"externalUrls": [{"label": label, "url": url}]}},
    )


def linear_activity_id(kind: str, key: str) -> str:
    """A stable activity id, so a retried post is rejected as a duplicate instead of repeated.

    Shaped as UUID v4 because Linear rejects activity ids of any other version.
    """
    digest = hashlib.sha256(f"open-swe:linear-{kind}:{key}".encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


async def set_session_plan(session_id: str, plan: list[JsonValue]) -> None:
    """Replace the session's plan; Linear has no partial update for it."""
    await linear_graphql(_SESSION_UPDATE, {"id": session_id, "input": {"plan": plan}})


class _LinearUserData(BaseModel):
    id: str
    name: str = ""
    email: str = ""
    guest: bool = False


class _IssueCreator(BaseModel):
    creator: _LinearUserData | None = None


class _IssueCreatorData(BaseModel):
    issue: _IssueCreator


async def issue_creator(issue_id: str) -> LinearUser | None:
    """The issue's creator, who stands behind sessions an automation started."""
    data = _IssueCreatorData.model_validate(
        await linear_graphql(
            "query($id: String!) { issue(id: $id) { creator { id name email } } }",
            {"id": issue_id},
        )
    )
    creator = data.issue.creator
    return LinearUser(id=creator.id, name=creator.name, email=creator.email) if creator else None


class _UserData(BaseModel):
    user: _LinearUserData


async def is_guest(user_id: str) -> bool:
    data = _UserData.model_validate(
        await linear_graphql(
            "query($id: String!) { user(id: $id) { id guest } }",
            {"id": user_id},
        )
    )
    return data.user.guest


class _Suggestion(BaseModel):
    repository_full_name: str = Field(alias="repositoryFullName")
    confidence: float


class _Suggestions(BaseModel):
    suggestions: list[_Suggestion]


class _SuggestionsData(BaseModel):
    issue_repository_suggestions: _Suggestions = Field(alias="issueRepositorySuggestions")


_SUGGESTIONS = """
query Suggest($issueId: String!, $sessionId: String, $candidates: [CandidateRepository!]!) {
  issueRepositorySuggestions(
    issueId: $issueId, agentSessionId: $sessionId, candidateRepositories: $candidates
  ) { suggestions { repositoryFullName confidence } }
}
"""


async def suggest_repositories(
    issue_id: str, session_id: str, candidates: list[str]
) -> list[tuple[str, float]]:
    """Linear's ranking of ``owner/name`` candidates for the issue, best first."""
    candidate_input: list[JsonValue] = [
        {"hostname": "github.com", "repositoryFullName": full_name} for full_name in candidates
    ]
    data = _SuggestionsData.model_validate(
        await linear_graphql(
            _SUGGESTIONS,
            {"issueId": issue_id, "sessionId": session_id, "candidates": candidate_input},
        )
    )
    ranked = sorted(
        data.issue_repository_suggestions.suggestions, key=lambda s: s.confidence, reverse=True
    )
    return [(s.repository_full_name, s.confidence) for s in ranked]


async def add_session_link(session_id: str, label: str, url: str) -> None:
    """Add a link to the session, keeping the ones already there."""
    await linear_graphql(
        _SESSION_UPDATE,
        {"id": session_id, "input": {"addedExternalUrls": [{"label": label, "url": url}]}},
    )


class _StateType(BaseModel):
    type: str


class _UserRef(BaseModel):
    id: str


class _StateNode(BaseModel):
    id: str
    position: float


class _StateNodes(BaseModel):
    nodes: list[_StateNode]


class _TeamStates(BaseModel):
    states: _StateNodes


class _IssueProgress(BaseModel):
    state: _StateType
    delegate: _UserRef | None = None
    team: _TeamStates


class _IssueProgressData(BaseModel):
    issue: _IssueProgress


_ISSUE_PROGRESS = """
query IssueProgress($id: String!) {
  issue(id: $id) {
    state { type }
    delegate { id }
    team { states(filter: { type: { eq: "started" } }) { nodes { id position } } }
  }
}
"""

_ISSUE_UPDATE = """
mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success }
}
"""

# Linear asks agents to leave issues that are already underway or closed where they are.
_NOT_STARTED = frozenset({"triage", "backlog", "unstarted"})


async def start_delegated_issue(issue_id: str) -> bool:
    """Move an issue delegated to the app to its team's first started status.

    Returns whether it moved.
    """
    data = _IssueProgressData.model_validate(
        await linear_graphql(_ISSUE_PROGRESS, {"id": issue_id})
    )
    issue = data.issue
    if issue.state.type not in _NOT_STARTED or not issue.team.states.nodes:
        return False
    if issue.delegate is None or issue.delegate.id != await app_user_id():
        return False
    first_started = min(issue.team.states.nodes, key=lambda state: state.position)
    await linear_graphql(_ISSUE_UPDATE, {"id": issue_id, "input": {"stateId": first_started.id}})
    return True


class _CreatedSession(BaseModel):
    id: str


class _SessionCreatePayload(BaseModel):
    agent_session: _CreatedSession = Field(alias="agentSession")


class _SessionCreateData(BaseModel):
    agent_session_create_on_issue: _SessionCreatePayload = Field(alias="agentSessionCreateOnIssue")


_SESSION_CREATE_ON_ISSUE = """
mutation SessionCreateOnIssue($input: AgentSessionCreateOnIssue!) {
  agentSessionCreateOnIssue(input: $input) { agentSession { id } }
}
"""


async def create_session_on_issue(issue_id: str, label: str, url: str | None) -> str:
    """Open a session on the issue as the app; Linear then announces it as a `created` event."""
    session_input: dict[str, JsonValue] = {"issueId": issue_id}
    if url:
        session_input["externalUrls"] = [{"label": label, "url": url}]
    data = _SessionCreateData.model_validate(
        await linear_graphql(_SESSION_CREATE_ON_ISSUE, {"input": session_input})
    )
    return data.agent_session_create_on_issue.agent_session.id
