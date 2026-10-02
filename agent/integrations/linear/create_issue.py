"""Creating a Linear issue from a Slack conversation, as the app."""

import logging

import httpx
from langgraph.config import get_config
from pydantic import JsonValue

from agent.integrations.linear.client import (
    LinearGraphQLError,
    LinearTeam,
    create_issue,
    list_teams,
)
from agent.run_config import RunConfig

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


def _requested_from_slack() -> str:
    slack = RunConfig.from_config(get_config()).slack_thread
    if slack is None:
        return ""
    who = (
        f"Requested by {slack.triggering_user_name}" if slack.triggering_user_name else "Requested"
    )
    return f"{who} in Slack: {slack.permalink}" if slack.permalink else f"{who} in Slack."


async def create_linear_issue(title: str, description: str, team: str = "") -> dict[str, JsonValue]:
    """Implement the `create_linear_issue` tool; it creates the issue and starts no work."""
    if not title.strip():
        return {"success": False, "error": "The title is empty."}
    try:
        teams = await list_teams()
        match = _team(teams, team)
        if match is None:
            names: list[JsonValue] = [t.name for t in teams]
            reason = f"No team matches {team!r}." if team.strip() else "Several teams exist."
            return {"success": False, "error": f"{reason} Ask which team.", "teams": names}
        footer = _requested_from_slack()
        body = f"{description.strip()}\n\n---\n{footer}" if footer else description.strip()
        issue = await create_issue(match.id, title.strip(), body)
    except (LinearGraphQLError, httpx.HTTPError) as exc:
        logger.warning("Creating a Linear issue failed", exc_info=True)
        return {"success": False, "error": f"Linear refused the request: {exc}"}
    return {
        "success": True,
        "identifier": issue.identifier,
        "url": issue.url,
        "team": match.name,
        "next_step": "Share the link; delegating the issue to Open SWE in Linear starts work.",
    }
