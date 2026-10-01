from langchain_core.tools import StructuredTool

from agent.integrations.linear import tools as linear_tools


async def test_status_tool_can_only_change_the_status(monkeypatch):
    sent: list[dict[str, object]] = []

    async def save_issue(**kwargs: object) -> str:
        sent.append(kwargs)
        return "ok"

    def mcp_tool(name: str) -> StructuredTool:
        async def call(**kwargs: object) -> str:
            return name

        return StructuredTool.from_function(coroutine=call, name=name, description=name)

    async def mcp_tools():
        offered = {name: mcp_tool(name) for name in linear_tools.READ_TOOLS | {"save_comment"}}
        fields = ("id", "state", "assignee", "description")
        offered["save_issue"] = StructuredTool.from_function(
            coroutine=save_issue,
            name="save_issue",
            description="save",
            args_schema={
                "type": "object",
                "properties": {field: {"type": "string"} for field in fields},
            },
        )
        return offered

    monkeypatch.setattr(linear_tools, "_mcp_tools", mcp_tools)

    loaded = {tool.name: tool for tool in await linear_tools.load_linear_tools()}
    await loaded[linear_tools.STATUS_TOOL].ainvoke(
        {"issue": "ENG-1", "status": "In Progress", "assignee": "someone-else"}
    )

    assert sorted(loaded) == sorted(linear_tools.linear_tool_group().tool_names)
    assert "linear_save_comment" not in loaded
    assert sent == [{"id": "ENG-1", "state": "In Progress"}]
