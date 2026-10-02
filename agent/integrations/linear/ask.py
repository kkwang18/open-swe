"""Asking the person in a Linear agent session to choose between options."""

from pydantic import JsonValue

ASK_TOOL = "ask_with_options"
MAX_OPTIONS = 10
MAX_OPTION_CHARS = 80


async def ask_with_options(question: str, options: list[str]) -> dict[str, JsonValue]:
    """Implement the `ask_with_options` tool; the Linear session middleware posts the question."""
    text = question.strip()
    choices = list(dict.fromkeys(option.strip() for option in options if option.strip()))
    if not text:
        return {"success": False, "error": "The question is empty."}
    if not 2 <= len(choices) <= MAX_OPTIONS:
        return {"success": False, "error": f"Give between 2 and {MAX_OPTIONS} distinct options."}
    if too_long := [choice for choice in choices if len(choice) > MAX_OPTION_CHARS]:
        return {
            "success": False,
            "error": f"Keep each option within {MAX_OPTION_CHARS} characters: {too_long[0]!r}.",
        }
    return {
        "success": True,
        "question": text,
        "options": list[JsonValue](choices),
        "next_step": "End your turn now, with the question as your final message.",
    }
