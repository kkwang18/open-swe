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
