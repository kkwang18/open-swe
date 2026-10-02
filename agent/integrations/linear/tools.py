"""Linear's hosted MCP tools for runs started from a Linear session, as the agent app.

Only reads and a status change are offered: replies go through the session, and
the app's token could otherwise rewrite any public issue in the workspace.
"""

import logging
from datetime import timedelta

from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel

from agent.integrations.linear.token import linear_app_auth
from agent.middleware.dynamic_tools import IntegrationGroup
from agent.prompts import load_prompt

logger = logging.getLogger(__name__)

LINEAR_MCP_URL = "https://mcp.linear.app/mcp"
GROUP_NAME = "Linear"
# Linear's names are generic (`get_issue`); a prefix keeps them distinct from any
# workspace MCP that uses the same ones.
PREFIX = "linear_"
_MCP_TIMEOUT = timedelta(seconds=30)

READ_TOOLS = frozenset(
    {
        "get_issue",
        "list_issues",
        "list_comments",
        "list_issue_statuses",
        "get_issue_status",
        "list_issue_labels",
        "list_projects",
        "get_project",
        "list_milestones",
        "get_milestone",
        "list_cycles",
        "list_teams",
        "get_team",
        "list_users",
        "get_user",
        "list_documents",
        "get_document",
        "get_attachment",
        "extract_images",
        "search_documentation",
    }
)
STATUS_TOOL = f"{PREFIX}update_issue_status"
_SAVE_ISSUE = "save_issue"


class _StatusChange(BaseModel):
    issue: str
    status: str


async def _mcp_tools() -> dict[str, BaseTool]:
    token = await linear_app_auth.access_token()
    client = MultiServerMCPClient(
        {
            "linear": {
                "transport": "streamable_http",
                "url": LINEAR_MCP_URL,
                "headers": {"Authorization": f"Bearer {token}"},
                "timeout": _MCP_TIMEOUT,
            }
        }
    )
    return {tool.name: tool for tool in await client.get_tools()}


def _status_tool(save_issue: BaseTool) -> BaseTool:
    async def update_issue_status(issue: str, status: str) -> object:
        return await save_issue.ainvoke({"id": issue, "state": status})

    return StructuredTool.from_function(
        coroutine=update_issue_status,
        name=STATUS_TOOL,
        description=load_prompt("tools/linear_update_issue_status.md"),
        args_schema=_StatusChange,
    )


async def load_linear_tools() -> list[BaseTool]:
    tools = await _mcp_tools()
    loaded = [
        tools[name].model_copy(update={"name": f"{PREFIX}{name}"})
        for name in sorted(READ_TOOLS)
        if name in tools
    ]
    if _SAVE_ISSUE in tools:
        loaded.append(_status_tool(tools[_SAVE_ISSUE]))
    missing = (READ_TOOLS | {_SAVE_ISSUE}) - tools.keys()
    if missing:
        logger.warning(
            "Linear MCP lacks expected tools", extra={"linear_missing_tools": sorted(missing)}
        )
    return loaded


def linear_tool_group() -> IntegrationGroup:
    return IntegrationGroup(
        tool_names=[*(f"{PREFIX}{name}" for name in sorted(READ_TOOLS)), STATUS_TOOL],
        load=load_linear_tools,
    )
