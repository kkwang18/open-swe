"""The Linear agent app's own credential.

A client-credentials token acts as the app user on every public team, so one
token serves all Linear calls: session activities, issue status, and tools.
"""

import asyncio
from collections.abc import AsyncGenerator
from time import monotonic

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from agent.config import ENV

TOKEN_URL = "https://api.linear.app/oauth/token"
# Linear revokes the app's existing tokens when one is minted with a different
# scope set, so every mint must ask for exactly this.
SCOPES = "read,write,app:assignable,app:mentionable"


class LinearAppAuthError(RuntimeError):
    """The app token could not be minted; safe to log, never contains the secret."""


class _Token(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    access_token: SecretStr = Field(min_length=1)
    token_type: str
    expires_in: float = Field(gt=0)


def linear_app_configured() -> bool:
    return ENV.LINEAR_CLIENT_ID.is_set() and ENV.LINEAR_CLIENT_SECRET.is_set()


class LinearAppAuth(httpx.Auth):
    """Bearer auth with the app token, minted on first use and re-minted once on 401."""

    def __init__(self) -> None:
        self._token = ""
        self._good_until = 0.0
        self._lock = asyncio.Lock()

    async def access_token(self, rejected: str | None = None) -> str:
        async with self._lock:
            if self._token and self._token != rejected and monotonic() < self._good_until:
                return self._token
            if not linear_app_configured():
                raise LinearAppAuthError("LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET are not set")
            async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as client:
                response = await client.post(
                    TOKEN_URL,
                    data={"grant_type": "client_credentials", "scope": SCOPES},
                    auth=(ENV.LINEAR_CLIENT_ID.get(), ENV.LINEAR_CLIENT_SECRET.get()),
                )
            if not response.is_success:
                raise LinearAppAuthError(
                    f"Linear token request failed (HTTP {response.status_code}); "
                    "check the client credentials and that client credentials are enabled"
                )
            token = _Token.model_validate(response.json())
            if token.token_type.lower() != "bearer":
                raise LinearAppAuthError("Linear returned a non-bearer token")
            self._token = token.access_token.get_secret_value()
            # Tokens last 30 days; renew a day early so a long-lived process never
            # sends an expired one.
            self._good_until = monotonic() + token.expires_in - min(86400, token.expires_in / 10)
            return self._token

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self.access_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            await response.aread()
            request.headers["Authorization"] = f"Bearer {await self.access_token(rejected=token)}"
            yield request


linear_app_auth = LinearAppAuth()
