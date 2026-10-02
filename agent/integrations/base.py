"""The contract a first-party integration implements.

An integration owns everything provider-specific about a request: checking and
parsing its webhook, acknowledging it, deciding who asked and for which repo,
asking when that is unclear, and reporting progress and the outcome back where
the request came from. The core calls these hooks in a fixed order instead of
branching on ``source``.

Hooks are grouped by where they run. The HTTP handler must answer within the
provider's deadline, so its hooks do local work only. Everything that calls the
provider runs in the intake worker after the provider has its 200, or in the
agent run itself.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, TypedDict

from agent.run_config import Repo
from agent.webhooks.event_log import EventRefs

if TYPE_CHECKING:
    # The webhook routes import this module; the agent stack must stay out of the web app.
    from agent.middleware.dynamic_tools import IntegrationGroup

IntegrationName = Literal["slack", "github", "linear"]


@dataclass(frozen=True)
class Ignored:
    """A valid delivery the integration does not act on, such as the bot's own activity."""

    reason: str


@dataclass(frozen=True)
class Actor:
    """The person a run acts for, resolved from the provider account that made the request."""

    provider_user_id: str
    github_login: str
    email: str | None = None


@dataclass(frozen=True)
class Denied:
    """A request that must not run; ``message`` is shown to the person who asked."""

    message: str


@dataclass(frozen=True)
class SelectOption:
    value: str
    label: str


@dataclass(frozen=True)
class PendingQuestion:
    """Something only the requester can answer before the run can start.

    Saved with the original request so the answer resumes it rather than
    starting over.
    """

    kind: Literal["link_account", "select_repo"]
    prompt: str
    requester_id: str
    options: tuple[SelectOption, ...] = ()
    link_url: str | None = None


@dataclass(frozen=True)
class ToolCallStep:
    """One tool call the agent made, as the provider should show it."""

    action: str
    parameter: str
    result: str | None = None


PlanItemStatus = Literal["pending", "inProgress", "completed", "canceled"]


@dataclass(frozen=True)
class PlanItem:
    content: str
    status: PlanItemStatus


@dataclass(frozen=True)
class PlanUpdate:
    """The agent's whole current plan; each update replaces the previous one."""

    items: tuple[PlanItem, ...] = ()


RunStep = ToolCallStep | PlanUpdate


class EventLogFields(TypedDict):
    event_type: str
    delivery_id: str
    refs: EventRefs


class WebhookIngress[EventT](Protocol):
    """The HTTP handler's hooks: local work only, inside the provider's response deadline."""

    name: ClassVar[IntegrationName]

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool: ...

    def log_fields(self, headers: Mapping[str, str], body: bytes) -> EventLogFields: ...

    def parse(self, headers: Mapping[str, str], body: bytes) -> EventT | Ignored: ...

    def event_key(self, event: EventT) -> str:
        """Stable across provider retries of the same delivery; dedupes intake."""
        ...


class Integration[EventT, RefT](WebhookIngress[EventT], Protocol):
    """A provider Open SWE takes requests from and answers through.

    ``EventT`` is the parsed webhook event. ``RefT`` is what the integration needs
    to talk back to the place the request came from; it travels with the run.
    """

    # Intake worker: after the provider has its 200.
    def thread_id(self, event: EventT) -> str: ...

    def source_ref(self, event: EventT) -> RefT: ...

    async def acknowledge(self, ref: RefT, thread_url: str) -> None: ...

    async def resolve_actor(self, event: EventT) -> Actor | PendingQuestion | Denied: ...

    async def resolve_repo(
        self, event: EventT, actor: Actor
    ) -> Repo | PendingQuestion | Denied: ...

    def interpret_answer(
        self, question: PendingQuestion, event: EventT
    ) -> SelectOption | PendingQuestion: ...

    async def ask(self, ref: RefT, question: PendingQuestion) -> None: ...

    # Agent run: the backend, not the model, owns these replies.
    async def progress(self, ref: RefT, step: RunStep) -> None: ...

    async def reply(self, ref: RefT, text: str) -> None: ...

    async def fail(self, ref: RefT, text: str) -> None: ...

    def pr_backlink(self, ref: RefT) -> str | None:
        """A line for the PR body linking back to the request, if the provider has one."""
        ...

    def tools(self, ref: RefT, actor: Actor) -> IntegrationGroup | None:
        """Provider tools for runs started from this integration only."""
        ...
