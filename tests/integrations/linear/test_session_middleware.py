import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.integrations.linear import middleware as linear_middleware
from agent.integrations.linear import session as linear_session
from agent.integrations.linear.client import linear_activity_id


class _Request:
    def __init__(self, name: str, args: dict[str, object]) -> None:
        self.tool_call = {"name": name, "args": args, "id": f"call-{name}"}


async def test_progress_posts_in_order_and_the_answer_closes_the_session(monkeypatch):
    posted: list[tuple[str, object]] = []

    async def post_activity(session_id, content, *, activity_id=None, ephemeral=False):
        await asyncio.sleep(0.01 if content["type"] == "action" else 0)
        posted.append((content["type"], activity_id or content.get("parameter")))

    async def set_session_plan(session_id, plan):
        posted.append(("plan", [step["status"] for step in plan]))

    monkeypatch.setattr(linear_middleware, "post_activity", post_activity)
    monkeypatch.setattr(linear_session, "post_activity", post_activity)
    monkeypatch.setattr(linear_middleware, "set_session_plan", set_session_plan)

    async def add_session_link(session_id, label, url):
        posted.append(("link", url))

    monkeypatch.setattr(linear_middleware, "add_session_link", add_session_link)
    monkeypatch.setattr(
        linear_middleware,
        "get_config",
        lambda: {"configurable": {"thread_id": "t-1", "run_id": "r-1"}},
    )
    middleware = linear_middleware.LinearSessionMiddleware("session-1")

    async def handler(request):
        content = "ok"
        if request.tool_call["name"] == "open_pull_request":
            content = (
                '{"success": true, "url": "https://github.com/acme/web/pull/12", "number": 12}'
            )
        return ToolMessage(content=content, tool_call_id=request.tool_call["id"])

    await middleware.awrap_tool_call(_Request("execute", {"command": "pytest -q"}), handler)
    await middleware.awrap_tool_call(
        _Request("write_todos", {"todos": [{"content": "Fix", "status": "in_progress"}]}), handler
    )
    await middleware.awrap_tool_call(_Request("read_file", {"file_path": "README.md"}), handler)
    await middleware.awrap_tool_call(_Request("open_pull_request", {"title": "Docs"}), handler)
    state = {
        "messages": [
            HumanMessage(content="add a sentence to the README"),
            AIMessage(content="", tool_calls=[{"name": "execute", "args": {}, "id": "c1"}]),
            AIMessage(content="Added it in PR #12."),
        ]
    }
    await middleware.aafter_agent(state, runtime=None)

    assert posted == [
        ("action", "pytest -q"),
        ("plan", ["inProgress"]),
        ("action", "README.md"),
        ("action", '{"title": "Docs"}'),
        ("link", "https://github.com/acme/web/pull/12"),
        ("response", linear_activity_id("reply", "r-1")),
    ]


@pytest.mark.parametrize(
    ("answer", "asks"),
    [
        ("Which line should change? Quote it. I won't edit anything until you reply.", True),
        ("Done.\n\nWant me to add tests too?", True),
        ("Should I update the docs?\n\nOpened PR #3.", False),
        ("Opened https://github.com/acme/web/pull/2?tab=files for review.", False),
    ],
)
def test_a_question_in_the_last_paragraph_awaits_the_person(answer, asks):
    assert linear_session._ends_with_question(answer) is asks
