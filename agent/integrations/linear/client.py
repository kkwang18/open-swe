"""Linear GraphQL calls made as the agent app."""

from collections.abc import Mapping

import httpx
from pydantic import BaseModel, Field, JsonValue, ValidationError

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
) -> None:
    """Post to a session. With ``activity_id``, a repeat is a no-op: Linear rejects the duplicate."""
    activity_input: dict[str, JsonValue] = {
        "agentSessionId": session_id,
        "content": content,
        "ephemeral": ephemeral,
    }
    if activity_id:
        activity_input["id"] = activity_id
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
