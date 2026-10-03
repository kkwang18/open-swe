from collections.abc import Iterable

import pytest

from agent.run_config import RunConfig
from agent.slack import runtime as slack_runtime_module
from agent.slack.runtime import slack_runtime

THREAD = {"channel_id": "C1", "thread_ts": "171.1", "triggering_user_id": "U1"}
CONCIERGE = {
    "channel_id": "D1",
    "thread_ts": "0",
    "triggering_user_id": "U1",
    "channel_context": {"channel_type": "im", "is_im": True},
}


def _names(tools: Iterable[object]) -> set[str]:
    return {getattr(tool, "name", None) or getattr(tool, "__name__", "") for tool in tools}


def _cfg(**raw: object) -> RunConfig:
    return RunConfig.model_validate(raw)


@pytest.fixture(autouse=True)
def linear_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_runtime_module, "linear_app_configured", lambda: False)


@pytest.mark.parametrize(
    "cfg",
    [
        _cfg(source="slack"),
        _cfg(source="linear", slack_thread=THREAD),
        _cfg(source="slack", slack_thread={"channel_id": "C1"}),
    ],
    ids=["no Slack thread", "not a Slack-capable source", "no thread to post into"],
)
def test_runs_without_trusted_slack_context_get_no_slack_tools(cfg: RunConfig) -> None:
    assert slack_runtime.tools(cfg) == ()
    assert slack_runtime.source_guidance(cfg) is None


def test_a_dashboard_follow_up_on_a_slack_thread_keeps_its_slack_tools() -> None:
    # Keeping them registered across the surface switch keeps the prompt prefix cacheable.
    tools = _names(slack_runtime.tools(_cfg(source="dashboard", slack_thread=THREAD)))

    assert {"slack_reply", "slack_read_thread_messages"} <= tools


def test_a_concierge_dm_can_start_threads_but_not_react() -> None:
    cfg = _cfg(source="slack", slack_thread=CONCIERGE)
    tools = list(slack_runtime.tools(cfg))

    assert "start_thread" in _names(tools)
    assert "slack_add_reaction" not in _names(slack_runtime.restrict_tools(cfg, tools))
    regular = _cfg(source="slack", slack_thread=THREAD)
    assert "slack_add_reaction" in _names(
        slack_runtime.restrict_tools(regular, list(slack_runtime.tools(regular)))
    )


@pytest.mark.parametrize(
    ("raw", "marker"),
    [
        ({"source": "slack"}, "slack_reply"),
        ({"source": "slack", "slack_ask": True}, "slash command"),
        ({"source": "schedule"}, "validated Slack destination"),
    ],
    ids=["thread", "ask", "schedule with a Slack destination"],
)
def test_slack_supplies_the_source_section_for_its_runs(
    raw: dict[str, object], marker: str
) -> None:
    guidance = slack_runtime.source_guidance(_cfg(slack_thread=THREAD, **raw))

    assert guidance is not None and marker in guidance
