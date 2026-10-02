"""Link a GitLab account to the signed-in Open SWE person.

The GitLab user id comes from GitLab's own OAuth, and the Open SWE person from
the dashboard session, so nobody can link an account they don't control. The
GitLab token is only used to read who signed in; it is revoked straight away.
Open SWE then checks that account's project access with the bot's token, so a
person can ask for GitLab work from Slack, Linear or the dashboard.
"""

import hmac
import html
import logging
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from agent.config import ENV
from agent.dashboard.oauth import (
    STATE_TTL_SECONDS,
    cookie_security,
    decode_state,
    frontend_base_url,
    hash_state_nonce,
    issue_state,
    new_state_nonce,
    optional_session,
    session_user_id,
)
from agent.integrations.gitlab.client import LINK_PATH, gitlab_linking_configured, gitlab_url
from agent.users import User
from agent.utils.dashboard_links import dashboard_api_base_url

logger = logging.getLogger(__name__)

router = APIRouter(tags=["gitlab"])

CALLBACK_PATH = f"{LINK_PATH}/callback"
_STATE_COOKIE = "osw_gitlab_link_state"


def _callback_url() -> str:
    return f"{dashboard_api_base_url().rstrip('/')}{CALLBACK_PATH}"


@router.get("/integrations/gitlab/link")
async def start_gitlab_link(request: Request) -> RedirectResponse:
    if not gitlab_linking_configured():
        raise HTTPException(500, "GitLab account linking is not configured")
    if optional_session(request) is None:
        return RedirectResponse(
            f"/dashboard/api/auth/login?{urlencode({'redirect_to': LINK_PATH})}", status_code=302
        )
    nonce = new_state_nonce()
    state = issue_state(redirect_to=frontend_base_url(), nonce_hash=hash_state_nonce(nonce))
    params = {
        "client_id": ENV.GITLAB_OAUTH_CLIENT_ID.get(),
        "redirect_uri": _callback_url(),
        "response_type": "code",
        "scope": "read_user",
        "state": state,
    }
    response = RedirectResponse(f"{gitlab_url()}/oauth/authorize?{urlencode(params)}", 302)
    secure, _ = cookie_security()
    response.set_cookie(
        key=_STATE_COOKIE,
        value=nonce,
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path=LINK_PATH,
    )
    return response


class _GitLabAccount(BaseModel):
    id: int
    username: str
    name: str = ""


async def _signed_in_account(code: str) -> _GitLabAccount:
    credentials = {
        "client_id": ENV.GITLAB_OAUTH_CLIENT_ID.get(),
        "client_secret": ENV.GITLAB_OAUTH_CLIENT_SECRET.get(),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as client:
        token_response = await client.post(
            f"{gitlab_url()}/oauth/token",
            data={
                **credentials,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _callback_url(),
            },
        )
        if not token_response.is_success:
            raise HTTPException(400, "GitLab sign-in failed; please try again")
        token = str(token_response.json().get("access_token") or "")
        try:
            account_response = await client.get(
                f"{gitlab_url()}/api/v4/user", headers={"Authorization": f"Bearer {token}"}
            )
            account_response.raise_for_status()
            return _GitLabAccount.model_validate_json(account_response.content)
        finally:
            revoke = await client.post(
                f"{gitlab_url()}/oauth/revoke", data={**credentials, "token": token}
            )
            if not revoke.is_success:
                logger.warning(
                    "Revoking the GitLab link token failed",
                    extra={"http_status": revoke.status_code},
                )


async def _session_user(session: dict[str, Any]) -> User | None:
    user_id = session_user_id(session)
    if user_id is not None:
        return await User.get(user_id)
    return await User.for_login("github", str(session.get("sub") or ""))


def _page(message: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>Open SWE</title>"
        "<body style='font:16px system-ui;margin:3rem auto;max-width:32rem'>"
        f"<p>{message}</p><p><a href='{html.escape(frontend_base_url())}/my-settings'>"
        "Back to Open SWE</a></p></body>"
    )


def _clear_state(response: Response) -> None:
    secure, _ = cookie_security()
    response.delete_cookie(_STATE_COOKIE, path=LINK_PATH, samesite="lax", secure=secure)


@router.get("/integrations/gitlab/link/callback")
async def gitlab_link_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> HTMLResponse:
    if error or not code or not state:
        logger.info("GitLab link did not complete", extra={"gitlab_oauth_error": error})
        return _page("Linking didn't finish. Open the link again to retry.")
    session = optional_session(request)
    if session is None:
        raise HTTPException(401, "Sign in to Open SWE first")
    nonce_hash = decode_state(state).get("nonce_hash")
    cookie_nonce = request.cookies.get(_STATE_COOKIE, "")
    if (
        not isinstance(nonce_hash, str)
        or not cookie_nonce
        or not hmac.compare_digest(hash_state_nonce(cookie_nonce), nonce_hash)
    ):
        raise HTTPException(400, "GitLab sign-in state mismatch; please try again")

    account = await _signed_in_account(code)
    user = await _session_user(session)
    if user is None:
        raise HTTPException(403, "Your Open SWE account isn't set up yet")
    await user.link("gitlab", str(account.id), login=account.username)
    logger.info("Linked a GitLab account", extra={"gitlab_user_id": account.id})
    response = _page(f"Linked your GitLab account <b>@{html.escape(account.username)}</b>.")
    _clear_state(response)
    return response
