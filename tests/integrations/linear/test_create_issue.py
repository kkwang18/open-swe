from agent.integrations.linear import create_issue as tool
from agent.integrations.linear.client import CreatedIssue, LinearTeam

TEAMS = [
    LinearTeam(id="t-eng", key="ENG", name="Engineering"),
    LinearTeam(id="t-ops", key="OPS", name="Operations"),
]


async def test_issue_from_slack_needs_a_team_and_credits_the_requester(monkeypatch):
    created: list[tuple[str, str, str]] = []
    origins: list[tuple[str, str, str]] = []

    async def list_teams():
        return TEAMS

    async def create_issue(team_id, title, description):
        created.append((team_id, title, description))
        return CreatedIssue(
            id="issue-7", identifier="ENG-7", url="https://linear.app/acme/issue/ENG-7"
        )

    async def record_slack_origin(issue_id, origin):
        origins.append((issue_id, origin.channel_id, origin.thread_ts))

    slack_thread = {
        "channel_id": "C1",
        "thread_ts": "1.0",
        "triggering_user_name": "Ada",
        "permalink": "https://acme.slack.com/archives/C1/p1",
    }
    monkeypatch.setattr(tool, "list_teams", list_teams)
    monkeypatch.setattr(tool, "create_issue", create_issue)
    monkeypatch.setattr(tool, "record_slack_origin", record_slack_origin)
    monkeypatch.setattr(
        tool, "get_config", lambda: {"configurable": {"slack_thread": slack_thread}}
    )

    unnamed = await tool.create_linear_issue("Fix login", "Users get a 500.")
    assert unnamed["success"] is False
    assert unnamed["teams"] == ["Engineering", "Operations"]
    assert created == []

    named = await tool.create_linear_issue("Fix login", "Users get a 500.", team="eng")
    assert named["success"] is True
    assert named["identifier"] == "ENG-7"
    assert created == [
        (
            "t-eng",
            "Fix login",
            "Users get a 500.\n\n---\nRequested by Ada in Slack: https://acme.slack.com/archives/C1/p1",
        )
    ]
    assert origins == [("issue-7", "C1", "1.0")]
