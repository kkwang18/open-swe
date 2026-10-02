import httpx

from agent.integrations.linear import client as linear_client
from agent.integrations.linear.token import LinearAppAuth


async def test_revoked_app_token_is_reminted_once(monkeypatch):
    monkeypatch.setenv("LINEAR_CLIENT_ID", "test-client")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", "test-secret")
    minted: list[str] = []
    revoked: set[str] = set()

    async def linear(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            minted.append(f"token-{len(minted) + 1}")
            return httpx.Response(
                200,
                json={"access_token": minted[-1], "token_type": "Bearer", "expires_in": 2591999},
            )
        if request.headers["Authorization"].removeprefix("Bearer ") in revoked:
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"data": {"viewer": {"id": "app-user"}}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(linear), **kwargs),
    )
    monkeypatch.setattr(linear_client, "linear_app_auth", LinearAppAuth())

    assert await linear_client.linear_graphql("query { viewer { id } }") == {
        "viewer": {"id": "app-user"}
    }
    revoked.add("token-1")
    assert await linear_client.linear_graphql("query { viewer { id } }") == {
        "viewer": {"id": "app-user"}
    }
    assert minted == ["token-1", "token-2"]
