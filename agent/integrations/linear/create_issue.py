"""Creating a Linear issue from a Slack conversation, as the app."""

import logging

import httpx
from langgraph.config import get_config
from pydantic import JsonValue

from agent.integrations.linear.client import (
    CreatedIssue,
    LinearGraphQLError,
    LinearTeam,
    create_issue,
    find_open_issue,
    list_teams,
)
from agent.integrations.linear.slack_origin import SlackOrigin, record_slack_origin
from agent.run_config import RunConfig
from agent.slack.client import get_slack_permalink
from agent.slack.dm import CONCIERGE_TS
from agent.utils.dashboard_links import dashboard_thread_url

logger = logging.getLogger(__name__)


def _team(teams: list[LinearTeam], wanted: str) -> LinearTeam | None:
    """The team named by key or name; the only team when none is named."""
    name = wanted.strip().lower()
    if not name:
        return teams[0] if len(teams) == 1 else None
    exact = [team for team in teams if name in (team.key.lower(), team.name.lower())]
    if exact:
        return exact[0]
    partial = [team for team in teams if name in team.name.lower()]
    return partial[0] if len(partial) == 1 else None


async def _slack_link(cfg: RunConfig) -> str:
    slack = cfg.slack_thread
    if slack is None or slack.thread_ts == CONCIERGE_TS:
        return ""
    # The run's Slack context does not always carry the link; Slack has it.
    return slack.permalink or await get_slack_permalink(slack.channel_id, slack.thread_ts) or ""


def _requested_from_slack(cfg: RunConfig, link: str) -> str:
    slack = cfg.slack_thread
    if slack is None:
        return ""
    who = (
        f"Requested by {slack.triggering_user_name}" if slack.triggering_user_name else "Requested"
    )
    return f"{who} in Slack: {link}" if link else f"{who} in Slack."


async def _remember_slack_thread(cfg: RunConfig, issue: CreatedIssue, title: str) -> str | None:
    """Let the Slack thread hear when the issue's pull request opens and when it is done.

    Returns the Open SWE link to the issue's thread, where its work will happen.
    """
    slack = cfg.slack_thread
    if slack is None or not slack.channel_id or not slack.thread_ts:
        return None
    context = slack.channel_context
    origin = SlackOrigin(
        channel_id=slack.channel_id,
        thread_ts=slack.thread_ts,
        agent_thread_id=cfg.thread_id or "",
        user_id=slack.triggering_user_id,
        is_dm=context is not None and context.is_im is True,
    )
    try:
        thread_id = await record_slack_origin(issue.id, f"{issue.identifier}: {title}", origin)
    except Exception:
        logger.warning("Recording a Linear issue's Slack thread failed", exc_info=True)
        return None
    return dashboard_thread_url(thread_id)


async def create_linear_issue(title: str, description: str, team: str = "") -> dict[str, JsonValue]:
    """Implement the `create_linear_issue` tool; it creates the issue and starts no work."""
    if not title.strip():
        return {"success": False, "error": "The title is empty."}
    cfg = RunConfig.from_config(get_config())
    try:
        teams = await list_teams()
        match = _team(teams, team)
        if match is None:
            names: list[JsonValue] = [t.name for t in teams]
            reason = f"No team matches {team!r}." if team.strip() else "Several teams exist."
            return {"success": False, "error": f"{reason} Ask which team.", "teams": names}
        link = await _slack_link(cfg)
        # A run that lost its history may file the same request again; Linear remembers.
        # Only the thread link ties a match to this conversation: without it, a matching
        # title may be someone else's request.
        if link and (existing := await find_open_issue(match.id, title.strip(), link)):
            return {
                "success": True,
                "already_existed": True,
                "identifier": existing.identifier,
                "url": existing.url,
                "team": match.name,
                "next_step": "This request already has an open issue; share its link.",
            }
        footer = _requested_from_slack(cfg, link)
        body = f"{description.strip()}\n\n---\n{footer}" if footer else description.strip()
        issue = await create_issue(match.id, title.strip(), body)
    except (LinearGraphQLError, httpx.HTTPError) as exc:
        logger.warning("Creating a Linear issue failed", exc_info=True)
        return {"success": False, "error": f"Linear refused the request: {exc}"}
    work_url = await _remember_slack_thread(cfg, issue, title.strip())
    result: dict[str, JsonValue] = {
        "success": True,
        "identifier": issue.identifier,
        "url": issue.url,
        "team": match.name,
        "next_step": (
            "Share the issue link, and the Open SWE link if there is one, where its work will "
            "show; delegating the issue to Open SWE in Linear starts work, and this thread hears "
            "when its pull request opens and when it is done."
        ),
    }
    if work_url:
        result["open_swe_url"] = work_url
    return result
