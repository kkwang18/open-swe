import pytest

from agent.integrations.linear import client as linear_client

APP = "app-user"


@pytest.mark.parametrize(
    ("state", "delegate", "moved_to"),
    [
        ("unstarted", APP, "in-progress"),
        ("triage", APP, "in-progress"),
        ("started", APP, None),
        ("completed", APP, None),
        ("unstarted", "someone-else", None),
        ("unstarted", None, None),
    ],
)
async def test_only_a_delegated_unstarted_issue_moves_to_the_first_started_status(
    monkeypatch, state, delegate, moved_to
):
    updates: list[str] = []

    async def linear_graphql(query, variables=None):
        if "issueUpdate" in query:
            updates.append(variables["input"]["stateId"])
            return {"issueUpdate": {"success": True}}
        return {
            "issue": {
                "state": {"type": state},
                "delegate": {"id": delegate} if delegate else None,
                "team": {
                    "states": {
                        "nodes": [
                            {"id": "in-review", "position": 3.0},
                            {"id": "in-progress", "position": 2.0},
                        ]
                    }
                },
            }
        }

    monkeypatch.setattr(linear_client, "linear_graphql", linear_graphql)
    monkeypatch.setattr(linear_client, "_app_user_id", APP)

    assert await linear_client.start_delegated_issue("issue-1") is (moved_to is not None)
    assert updates == ([moved_to] if moved_to else [])
