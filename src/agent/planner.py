"""The planning agent: a request in plain words in, a checked WorkflowSpec out.

This is the only module that talks to the Claude API. It gives Claude three things and lets the SDK's tool
runner do the rest:

  system prompt   prompts/system_prompt.md: the job, the cell in brief, how to plan, when not to submit
  tools           tools.py: describe_cell, rack_inventory, submit_workflow, mark_plan_ready
  the request     the person's words, as the first user message; later answers continue the conversation

The tool runner is the agent loop. It sends the request; when Claude answers with tool calls, it runs the
Python functions and sends their results back; it repeats until Claude answers without a tool call (or
`max_iterations` is reached). The spec comes out of the loop through `submit_workflow`, which leaves it in a
PlanBox; Claude's final text is kept for the person (what it planned, why it did not, or a question).

A conversation continues by planning again with the earlier turns as `history`: the person's words and Claude's
replies, as plain text. Claude sees the whole exchange and plans afresh (checking the deck again with its tools),
so an answer to its question, or a change to a plan it made ("make it 50 C"), works the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from typing import Callable

import anthropic

from .inventory import Inventory
from .spec import WorkflowSpec
from .tools import PlanBox, make_tools
from .validator import Report

MODEL = "claude-sonnet-5-5"           # half the price of Opus 5.5 per token; planning is a simple task
SYSTEM_PROMPT = resources.files("agent").joinpath("prompts/system_prompt.md").read_text()

# If a safety classifier declines a request, the API re-runs it on a fallback model it picks by the kind of
# decline, inside the same call ("default" routing). Without it a declined request just stops.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class PlanningError(RuntimeError):
    """The agent could not finish: the request was declined, or the loop was cut off."""


@dataclass
class PlanResult:
    spec: WorkflowSpec | None        # None: Claude did not submit (it explains why in `reply`)
    ready: bool                      # Claude marked the spec ready to run; False: it still wants the person's input
    report: Report | None            # the validator's findings for the accepted spec (warnings, vials to move)
    reply: str                       # Claude's final words to the person
    tool_calls: list = field(default_factory=list)       # (tool name, input) in the order Claude made them
    submissions: int = 0             # how many times submit_workflow was called (rejected ones included)
    conversation: list = field(default_factory=list)     # every turn so far, as text: pass it as the next `history`


def plan_workflow(request: str, inventory: Inventory, *, history: list | None = None,
                  client: anthropic.Anthropic | None = None, model: str = MODEL, effort: str = "high",
                  max_iterations: int = 12, on_message: Callable | None = None) -> PlanResult:
    """Ask Claude to plan `request` against the deck `inventory` reports. `history` is the `conversation` of an
    earlier result, to answer Claude's question or change its plan: `request` is then the person's next words.

    `effort` sets how hard Claude thinks (low / medium / high / xhigh / max); high is Sonnet 5.5's default too,
    stated here so the setting is visible. `on_message` is called with every message the loop produces, e.g. to show
    the tool calls as they happen."""
    client = client or anthropic.Anthropic()
    box = PlanBox()
    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=make_tools(inventory, box),
        messages=[*(history or []), {"role": "user", "content": request}],
        thinking={"type": "adaptive"},            # Claude decides when and how much to think
        output_config={"effort": effort},
        max_iterations=max_iterations,
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )

    calls, final = [], None
    for message in runner:                        # one message per round trip to the API
        calls += [(b.name, b.input) for b in message.content if b.type == "tool_use"]
        if on_message:
            on_message(message)
        final = message
    if final is None:
        raise PlanningError("the agent loop produced no reply")

    # Check why the last reply ended before trusting it.
    if final.stop_reason == "refusal":
        raise PlanningError(f"the request was declined: {getattr(final.stop_details, 'explanation', None)}")
    if final.stop_reason == "max_tokens":
        raise PlanningError("the reply was cut off at max_tokens")
    if final.stop_reason == "tool_use":
        raise PlanningError(f"the agent was still calling tools after {max_iterations} round trips")

    reply = "\n".join(b.text for b in final.content if b.type == "text").strip()
    conversation = [*(history or []), {"role": "user", "content": request},
                    {"role": "assistant", "content": reply or "(no reply)"}]
    return PlanResult(spec=box.spec, ready=box.ready and box.spec is not None, report=box.report, reply=reply,
                      tool_calls=calls, submissions=len(box.attempts), conversation=conversation)
