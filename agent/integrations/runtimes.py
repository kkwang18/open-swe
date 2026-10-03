"""The integrations an agent run asks what they add: tools, prompt and middleware.

Imported by the agent only; the web app stays free of the agent stack.
"""

from agent.integrations.base import IntegrationRuntime
from agent.slack.runtime import slack_runtime

RUNTIMES: tuple[IntegrationRuntime, ...] = (slack_runtime,)
