"""The workflow spec: the structured process the planning agent must produce.

The agent never writes code. It describes *what* should happen as data, using only the building blocks
defined here. Later steps check the spec against the live cell (validator), turn it into an Orca workflow
(compiler) and run it after a person approves it.

    {
      "version": 1,
      "name": "rows_C_to_F",
      "processes": [
        {"id": "scan_weigh",
         "source": {"bay": 3, "rows": "CE"},
         "steps": [{"op": "scan", "tries": 3}, {"op": "weigh"}],
         "destination": {"bay": 8, "wells": "same"}},
        {"id": "heat",
         "source": {"bay": 3, "rows": "DF"},
         "steps": [{"op": "scan"}, {"op": "weigh"},
                   {"op": "heat", "celsius": 40, "hold_s": 20, "rules": [
                       {"when": {"value": "grams", "lt": 10}, "then": "skip"},
                       {"when": {"value": "code", "starts_with": "10"}, "then": {"celsius": 80}}]}],
         "destination": {"bay": 8, "wells": "same"}}
      ]
    }

Every process runs at the same time as the others; Orca shares the arm and the instruments between them.
The same Pydantic models serve three purposes: they become the tool's input schema that Claude sees,
they validate what Claude sends, and they are the input to the validator and compiler.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROW_LETTERS = "ABCDEFGH"
COLUMNS = range(1, 13)


class _Strict(BaseModel):
    # Unknown fields are an error, not silently ignored: a typo in a spec must not pass unnoticed.
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------------------- conditions
# Per-vial logic is data too: a step can carry rules ("skip the heat if the vial weighs under 10 g", "heat to
# 80 C if the code starts with 10"). A rule tests one value an earlier step measured for the same vial, with
# one comparison from a fixed list; the compiler evaluates it while the vial is processed. The model never
# writes code.

MEASURED_BY = {"code": "scan", "read": "scan", "grams": "weigh", "celsius": "heat"}   # value -> step that measures it
NUMBER_TESTS, TEXT_TESTS = ("lt", "le", "gt", "ge"), ("starts_with", "ends_with")
COMPARE = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge, "eq": operator.eq,
           "starts_with": lambda code, text: str(code).startswith(text),
           "ends_with": lambda code, text: str(code).endswith(text)}


class Condition(_Strict):
    """A test on a value an earlier step measured for this vial. Give exactly one comparison."""
    value: Literal["code", "read", "grams", "celsius"] = Field(
        description="code (text) and read (true/false) from scan, grams from weigh, celsius from heat.")
    lt: float | None = Field(None, description="value < this (grams, celsius)")
    le: float | None = Field(None, description="value <= this (grams, celsius)")
    gt: float | None = Field(None, description="value > this (grams, celsius)")
    ge: float | None = Field(None, description="value >= this (grams, celsius)")
    eq: bool | float | str | None = Field(None, description="value == this (any value; read takes true/false)")
    starts_with: str | None = Field(None, description="code starts with this text")
    ends_with: str | None = Field(None, description="code ends with this text")

    @model_validator(mode="after")
    def _one_fitting_comparison(self) -> "Condition":
        given = self.comparisons()
        if len(given) != 1:
            raise ValueError(f"give exactly one comparison, got {len(given)} ({', '.join(given) or 'none'})")
        (test, target), = given.items()
        if test in NUMBER_TESTS and self.value not in ("grams", "celsius"):
            raise ValueError(f"{test} compares numbers; {self.value} is not a number")
        if test in TEXT_TESTS and self.value != "code":
            raise ValueError(f"{test} compares text; only code is text")
        if test == "eq":
            kind = {"code": str, "read": bool}.get(self.value, float)
            if isinstance(target, bool) != (kind is bool) or (kind is str and not isinstance(target, str)):
                raise ValueError(f"eq on {self.value} needs a {kind.__name__}")
        return self

    def comparisons(self) -> dict:
        return {test: getattr(self, test) for test in (*NUMBER_TESTS, "eq", *TEXT_TESTS) if getattr(self, test) is not None}

    def holds(self, measured: dict) -> bool:
        """True if the vial's measured value passes the test. A value never measured (a code that did not
        read) passes no test, except read == false."""
        (test, target), = self.comparisons().items()
        value = measured.get(self.value)
        if value is None:
            return False
        return COMPARE[test](value, target)

    def describe(self) -> str:
        (test, target), = self.comparisons().items()
        symbol = {"lt": "<", "le": "<=", "gt": ">", "ge": ">=", "eq": "=="}.get(test, test.replace("_", " "))
        return f"{self.value} {symbol} {target!r}"


class ScanChange(_Strict):
    tries: int = Field(ge=1, le=10)


class HeatChange(_Strict):
    celsius: float | None = Field(None, ge=25, le=100)
    hold_s: float | None = Field(None, ge=0, le=3600)

    @model_validator(mode="after")
    def _changes_something(self) -> "HeatChange":
        if self.celsius is None and self.hold_s is None:
            raise ValueError("a heat change needs celsius or hold_s")
        return self


class ScanRule(_Strict):
    when: Condition
    then: Literal["skip"] | ScanChange


class WeighRule(_Strict):
    when: Condition
    then: Literal["skip"]


class HeatRule(_Strict):
    when: Condition
    then: Literal["skip"] | HeatChange = Field(description='"skip", or the values to heat with instead')


RULES_HELP = ("Checked in order for each vial; the first rule whose condition holds decides: skip this step, or "
              "run it with the changed values. If none holds, the step runs as written.")


# ----------------------------------------------------------------------------------------- steps
# The step menu. The agent can only combine these; adding a capability means adding a class here
# (and a compiler builder, and a check in the validator).

class Scan(_Strict):
    """Read the vial's 2D code in the code reader."""
    op: Literal["scan"]
    tries: int = Field(3, ge=1, le=10, description="Attempts before the vial is recorded as unreadable.")
    rules: list[ScanRule] = Field(default_factory=list, description=RULES_HELP)


class Weigh(_Strict):
    """Weigh the vial on the balance."""
    op: Literal["weigh"]
    rules: list[WeighRule] = Field(default_factory=list, description=RULES_HELP)


class Heat(_Strict):
    """Heat the vial in the heater well, then hold it at temperature."""
    op: Literal["heat"]
    celsius: float = Field(ge=25, le=100, description="Target temperature, 25 to 100 C.")
    hold_s: float = Field(ge=0, le=3600, description="Seconds to hold at temperature once stable.")
    rules: list[HeatRule] = Field(default_factory=list, description=RULES_HELP)


Step = Annotated[Union[Scan, Weigh, Heat], Field(discriminator="op")]


def decide(step, measured: dict):
    """What to do with `step` for a vial: (the step to run, or None to skip it; the rule that decided, or None)."""
    for rule in step.rules:
        if rule.when.holds(measured):
            if rule.then == "skip":
                return None, rule
            return step.model_copy(update=rule.then.model_dump(exclude_none=True)), rule
    return step, None


# ----------------------------------------------------------------------------------------- places

class Source(_Strict):
    """Where the vials come from: whole rows of one rack bay."""
    bay: int = Field(ge=1, le=8, description="Rack bay, 1 to 8.")
    rows: str = Field(pattern="^[A-H]+$", description='Row letters, e.g. "CE" for rows C and E (12 wells each).')

    def wells(self) -> list[str]:
        """Every well of the chosen rows, row by row: C1..C12, then E1..E12."""
        return [f"{row}{col}" for row in self.rows for col in COLUMNS]


class Destination(_Strict):
    """Where the vials end up."""
    bay: int = Field(ge=1, le=8, description="Rack bay, 1 to 8.")
    wells: Literal["same"] = Field("same", description="Each vial goes to the same well position it came from.")


# ----------------------------------------------------------------------------------------- the spec

class Process(_Strict):
    """One job: take the source vials through the steps, in order, then to the destination."""
    id: str = Field(pattern="^[a-z][a-z0-9_]*$", description='Short name, e.g. "scan_weigh".')
    source: Source
    steps: list[Step] = Field(default_factory=list, description=(
        "What happens to each vial, in order. Empty: the vial goes straight from its source well to the same "
        "well of the destination rack, a plain rack-to-rack move."))
    destination: Destination

    @model_validator(mode="after")
    def _rules_test_values_measured_before(self) -> "Process":
        """A rule can only test what an earlier step of this process measures: no grams before a weigh."""
        for i, step in enumerate(self.steps):
            earlier = {s.op for s in self.steps[:i]}
            for rule in step.rules:
                needed = MEASURED_BY[rule.when.value]
                if needed not in earlier:
                    raise ValueError(f"step {i} ({step.op}): a rule tests {rule.when.value}, which only a {needed} "
                                     f"step measures, and there is no {needed} before it")
        return self


class WorkflowSpec(_Strict):
    """A whole plan: one or more processes that run at the same time."""
    version: Literal[1] = 1
    name: str = Field(pattern="^[a-z][a-z0-9_]*$", description='Short name, e.g. "rows_c_to_f".')
    summary: str = Field(description="One or two sentences, for the person approving the plan.")
    processes: list[Process] = Field(min_length=1)
