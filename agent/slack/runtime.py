"""Slack's part in an agent run: its tools, its prompt section, and DM restrictions.

The agent asks this runtime what Slack adds to a run instead of branching on
Slack itself. A run takes part in Slack when it carries a trusted Slack thread:
started from Slack, or from a schedule, incident or dashboard follow-up whose
``slack_thread`` came from the thread's own metadata.
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, ClassVar

from agent.integrations.base import IntegrationName
from agent.integrations.linear.create_issue import create_linear_issue
from agent.integrations.linear.token import linear_app_configured
from agent.prompts import prompt
from agent.run_config import RunConfig
from agent.slack.dm import is_concierge_thread
from agent.threads.summary import DASHBOARD_SOURCE
from agent.tools import (
    manage_code_channel,
    manage_incident,
    slack_add_reaction,
    slack_attach_html,
    slack_list_channel_members,
    slack_list_channels,
    slack_move_thread,
    slack_no_reply_needed,
    slack_post_message,
    slack_read_thread_messages,
    slack_reply,
    slack_start_new_thread,
    start_thread,
)

if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware

    from agent.integrations.base import AgentTool
    from agent.middleware.dynamic_tools import IntegrationGroup

# Tools that may act in someone's DM but not in their concierge DM.
DM_EXCLUDED_TOOLS: frozenset[str] = frozenset({"slack_add_reaction"})

_SLACK_TOOLS: tuple[AgentTool, ...] = (
    manage_code_channel,
    manage_incident,
    slack_add_reaction,
    slack_attach_html,
    slack_list_channel_members,
    slack_list_channels,
    slack_move_thread,
    slack_no_reply_needed,
    slack_post_message,
    slack_read_thread_messages,
    slack_reply,
    slack_start_new_thread,
)


def slack_ask_mode(cfg: RunConfig) -> bool:
    """A `/oswe` question: one ephemeral answer, no Slack thread to post into."""
    return (
        cfg.slack_ask is True
        and cfg.slack_thread is not None
        and bool(cfg.slack_thread.triggering_user_id.strip())
    )


def slack_tools_enabled(cfg: RunConfig) -> bool:
    """Return whether the run has trusted Slack source context.

    A web follow-up counts: its `slack_thread` is copied from the thread's own
    metadata, never from the client, and keeping the tools registered across a
    surface switch is what keeps the prompt prefix cacheable.
    """
    if cfg.source not in {"slack", "schedule", "incidents_agent", DASHBOARD_SOURCE}:
        return False
    if cfg.slack_thread is None:
        return False
    if slack_ask_mode(cfg):
        return bool(cfg.slack_thread.channel_id.strip())
    return bool(cfg.slack_thread.channel_id.strip() and cfg.slack_thread.thread_ts.strip())


def slack_concierge_run(cfg: RunConfig) -> bool:
    """Whether this run answers in a bot DM its owner runs in concierge mode."""
    return (
        slack_tools_enabled(cfg)
        and cfg.slack_thread is not None
        and is_concierge_thread(cfg.slack_thread.channel_context, cfg.slack_thread.thread_ts)
    )


def _tool_name(tool: AgentTool) -> str:
    name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
    return name if isinstance(name, str) else ""


class SlackRuntime:
    name: ClassVar[IntegrationName] = "slack"

    def tools(self, cfg: RunConfig) -> Sequence[AgentTool]:
        if not slack_tools_enabled(cfg):
            return ()
        return (
            *((start_thread,) if slack_concierge_run(cfg) else ()),
            *_SLACK_TOOLS,
            *((create_linear_issue,) if linear_app_configured() else ()),
        )

    def restrict_tools(self, cfg: RunConfig, tools: list[AgentTool]) -> list[AgentTool]:
        if not slack_concierge_run(cfg):
            return tools
        return [tool for tool in tools if _tool_name(tool) not in DM_EXCLUDED_TOOLS]

    def source_guidance(self, cfg: RunConfig) -> str | None:
        if not slack_tools_enabled(cfg):
            return None
        breakout = cfg.slack_breakout is True
        if cfg.source == "schedule":
            return prompt("system/source-schedule", breakout=breakout, slack=True)
        if cfg.source != "slack":
            return None
        if slack_ask_mode(cfg):
            by_the_way = bool(cfg.slack_by_the_way_thread_ts)
            return prompt(f"system/source-{'slack-by-the-way' if by_the_way else 'slack-ask'}")
        return prompt("system/source-slack", breakout=breakout, slack=True)

    def tool_groups(self, cfg: RunConfig) -> Mapping[str, IntegrationGroup]:
        return {}

    def middleware(self, cfg: RunConfig) -> Sequence[AgentMiddleware]:
        return ()


slack_runtime = SlackRuntime()
