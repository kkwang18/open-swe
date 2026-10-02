import pytest

from agent.integrations.linear import slack_origin


@pytest.mark.parametrize("concierge", [False, True], ids=["thread", "concierge-dm"])
async def test_the_slack_thread_an_issue_came_from_hears_its_pull_request(monkeypatch, concierge):
    origin = {
        "channel_id": "D1" if concierge else "C1",
        "thread_ts": "0" if concierge else "1700000000.000100",
        "agent_thread_id": "slack-thread",
        "user_id": "U1",
        "is_dm": concierge,
    }
    metadata = {
        slack_origin.ORIGIN_KEY: origin,
        "source_context": {
            "linear_issue": {"identifier": "OSWE-25", "url": "https://linear.app/x/OSWE-25"}
        },
    }
    replies: list[tuple[str, str, str]] = []
    top_level: list[tuple[str, str]] = []
    notes: list[str] = []

    class Threads:
        async def get(self, thread_id):
            return {"metadata": metadata}

    class Client:
        threads = Threads()

    async def post_slack_thread_reply(channel_id, thread_ts, text, **_):
        replies.append((channel_id, thread_ts, text))
        return True

    async def post_slack_top_level_message_with_ts(channel_id, text, **_):
        top_level.append((channel_id, text))
        return "1.0", None

    async def note_for_concierge(user_id, channel_id, note):
        notes.append(note)

    monkeypatch.setattr(slack_origin, "get_client", lambda: Client())
    monkeypatch.setattr(slack_origin, "post_slack_thread_reply", post_slack_thread_reply)
    monkeypatch.setattr(
        slack_origin, "post_slack_top_level_message_with_ts", post_slack_top_level_message_with_ts
    )
    monkeypatch.setattr(slack_origin, "note_for_concierge", note_for_concierge)

    await slack_origin.announce_pull_request(
        "issue-thread", "Pull request #9", "https://github.com/acme/web/pull/9"
    )

    text = (
        "<https://github.com/acme/web/pull/9|Pull request #9> is open for "
        "<https://linear.app/x/OSWE-25|OSWE-25>."
    )
    if concierge:
        assert top_level == [("D1", text)]
        assert replies == []
        assert notes == [text]
    else:
        assert replies == [("C1", "1700000000.000100", text)]
        assert top_level == []
        assert notes == []
