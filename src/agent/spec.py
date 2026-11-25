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
         "destination": {"bay": 8}},                        # the same wells of bay 8
        {"id": "heat",
         "source": {"bay": 3, "rows": "DF"},
         "steps": [{"op": "scan"}, {"op": "weigh"},
                   {"op": "heat", "celsius": 40, "hold_s": 20, "rules": [
                       {"when": {"value": "grams", "lt": 10}, "then": "skip"},
                       {"when": {"value": "code", "starts_with": "10"}, "then": {"celsius": 80}}]}],
         "destination": {"bay": 8, "rows": "GH"}}          # row D to row G, row F to row H
      ]
    }

Every process runs at the same time as the others; Orca shares the arm and the instruments between them.
The same Pydantic models serve three purposes: they become the tool's input schema that Claude sees,
they validate what Claude sends, and they are the input to the validator and compiler.
"""

from __future__ import annotations

import operator
import re
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

WELL_PATTERN = re.compile(r"^[A-H](1[0-2]|[1-9])$")


class Source(_Strict):
    """Where the vials come from, in one rack bay: whole rows, whole columns, or chosen wells. Give exactly one."""
    bay: int = Field(ge=1, le=8, description="Rack bay, 1 to 8.")
    rows: str | None = Field(None, pattern="^[A-H]+$",
                             description='Whole rows: row letters, e.g. "CE" for rows C and E (12 wells each).')
    columns: list[int] | None = Field(None, description="Whole columns: column numbers 1-12, e.g. [1, 12] (8 wells each).")
    wells: list[str] | None = Field(None, description='Chosen wells, e.g. ["A6", "B6", "C6", "D6"] for the first four '
                                                      'vials of column 6.')

    @model_validator(mode="after")
    def _one_kind_of_source(self) -> "Source":
        given = [k for k in ("rows", "columns", "wells") if getattr(self, k)]
        if len(given) != 1:
            raise ValueError(f"give exactly one of rows, columns or wells, got {len(given)} ({', '.join(given) or 'none'})")
        if self.columns and (any(not 1 <= c <= len(COLUMNS) for c in self.columns) or len(set(self.columns)) < len(self.columns)):
            raise ValueError(f"columns are 1 to {len(COLUMNS)}, each once")
        if self.wells:
            self.wells = [w.upper() for w in self.wells]
            bad = [w for w in self.wells if not WELL_PATTERN.match(w)]
            if bad or len(set(self.wells)) < len(self.wells):
                raise ValueError(f"wells are A1 to H12, each once (not: {', '.join(bad) or 'a repeat'})")
        return self

    def positions(self) -> list[str]:
        """Every well this source takes, in order: row by row (C1..C12, E1..E12), column by column (A1..H1,
        A12..H12), or as listed."""
        if self.rows:
            return [f"{row}{col}" for row in self.rows for col in COLUMNS]
        if self.columns:
            return [f"{row}{col}" for col in self.columns for row in ROW_LETTERS]
        return list(self.wells)

    def groups(self) -> list[tuple[str, list[str]]]:
        """The wells in the groups a person named: ("row C", [...]), ("column 6", [...]) or ("wells", [...])."""
        if self.rows:
            return [(f"row {r}", [f"{r}{c}" for c in COLUMNS]) for r in self.rows]
        if self.columns:
            return [(f"column {c}", [f"{r}{c}" for r in ROW_LETTERS]) for c in self.columns]
        return [("wells " + ", ".join(self.wells), list(self.wells))]

    def describe(self) -> str:
        if self.rows:
            return f"rows {self.rows}"
        if self.columns:
            return "columns " + ", ".join(map(str, self.columns))
        return "wells " + ", ".join(self.wells)


class Destination(_Strict):
    """Where the vials end up: a rack bay (the same one is allowed) and which wells. Give at most one of rows,
    columns or wells; with none, each vial goes to the same well position it came from."""
    bay: int = Field(ge=1, le=8, description="Rack bay, 1 to 8; may be the source bay.")
    rows: str | None = Field(None, pattern="^[A-H]+$", description=(
        'For a source given as rows: one row letter per source row, in order; each vial keeps its column. '
        'Source rows "A" with destination rows "H" moves A1 to H1, A2 to H2, ...'))
    columns: list[int] | None = Field(None, description=(
        "For a source given as columns: one column number per source column, in order; each vial keeps its row."))
    wells: Literal["same"] | list[str] | None = Field(None, description=(
        'For a source given as wells: one destination well per source well, paired in order. "same" (or '
        'leaving all three out) keeps every vial\'s well position.'))

    @model_validator(mode="after")
    def _at_most_one_kind(self) -> "Destination":
        if self.wells == "same":
            self.wells = None
        given = [k for k in ("rows", "columns", "wells") if getattr(self, k)]
        if len(given) > 1:
            raise ValueError(f"give at most one of rows, columns or wells, got {', '.join(given)}")
        if self.columns and any(not 1 <= c <= len(COLUMNS) for c in self.columns):
            raise ValueError(f"columns are 1 to {len(COLUMNS)}")
        if self.wells:
            self.wells = [w.upper() for w in self.wells]
            bad = [w for w in self.wells if not WELL_PATTERN.match(w)]
            if bad:
                raise ValueError(f"wells are A1 to H12 (not: {', '.join(bad)})")
        return self

    def describe(self) -> str:
        if self.rows:
            return f"bay {self.bay} rows {self.rows}"
        if self.columns:
            return f"bay {self.bay} columns " + ", ".join(map(str, self.columns))
        if self.wells:
            return f"bay {self.bay} wells " + ", ".join(self.wells)
        return f"bay {self.bay}, same wells"


class TransitWell(_Strict):
    """A free well where a vial waits while the vial in its destination moves out (a swap)."""
    bay: int = Field(ge=1, le=8, description="Rack bay, 1 to 8.")
    well: str = Field(pattern=WELL_PATTERN.pattern, description='A free well, e.g. "E1".')

    def describe(self) -> str:
        return f"bay {self.bay} {self.well}"


# ----------------------------------------------------------------------------------------- the spec

class RouteRule(_Strict):
    when: Condition
    then: Destination = Field(description='Where the vial goes when the condition holds. Back to where it came '
                                          'from: the source bay, same wells ({"bay": <source bay>}).')


class Process(_Strict):
    """One job: take the source vials through the steps, in order, then to the destination."""
    id: str = Field(pattern="^[a-z][a-z0-9_]*$", description='Short name, e.g. "scan_weigh".')
    source: Source
    steps: list[Step] = Field(default_factory=list, description=(
        "What happens to each vial, in order. Empty: the vial goes straight from its source well to the same "
        "well of the destination rack, a plain rack-to-rack move."))
    destination: Destination = Field(description="Where the vials go; with routes, where a vial goes when no route "
                                                 "holds.")
    routes: list[RouteRule] = Field(default_factory=list, description=(
        "Per-vial destinations, decided once the vial's steps are done: checked in order, the first rule whose "
        "condition holds decides where the vial goes; if none holds, it goes to `destination`. A rule tests a "
        "value one of the process's steps measures."))

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
        ops = {s.op for s in self.steps}
        for i, route in enumerate(self.routes):
            needed = MEASURED_BY[route.when.value]
            if needed not in ops:
                raise ValueError(f"route {i}: tests {route.when.value}, which only a {needed} step measures, and the "
                                 f"process has no {needed} step")
        return self

    @model_validator(mode="after")
    def _destinations_fit_source(self) -> "Process":
        for name, dst in [("destination", self.destination)] + [(f"route {i}", r.then) for i, r in enumerate(self.routes)]:
            for kind, unit in (("rows", "row"), ("columns", "column"), ("wells", "well")):
                if getattr(dst, kind) is None:
                    continue
                if getattr(self.source, kind) is None:
                    raise ValueError(f"{name}: destination {kind} need a source given as {kind}: one destination "
                                     f"{unit} per source {unit}")
                if len(getattr(dst, kind)) != len(getattr(self.source, kind)):
                    raise ValueError(f"{name}: destination {kind} must name one {unit} per source {unit}: "
                                     f"{len(getattr(self.source, kind))} source, {len(getattr(dst, kind))} destination")
            targets = [w for _, w in self.pairs(dst)]
            if len(set(targets)) < len(targets):
                raise ValueError(f"{name}: two vials would go into the same destination well")
        return self

    def pairs(self, destination: Destination | None = None) -> list[tuple[str, str]]:
        """(source well, destination well) for every well the source takes, in the source's order, for
        `destination` (default: the process's own)."""
        src, dst = self.source, destination or self.destination
        if dst.rows:
            to_row = dict(zip(src.rows, dst.rows))
            return [(w, to_row[w[0]] + w[1:]) for w in src.positions()]
        if dst.columns:
            to_col = dict(zip(src.columns, dst.columns))
            return [(w, f"{w[0]}{to_col[int(w[1:])]}") for w in src.positions()]
        if dst.wells:
            return list(zip(src.wells, dst.wells))
        return [(w, w) for w in src.positions()]

    def candidates(self) -> list[Destination]:
        """Every destination a vial of this process may end in: the routes' and the default one."""
        return [r.then for r in self.routes] + [self.destination]

    def target(self, well: str, destination: Destination) -> tuple[int, str]:
        """(bay, well) where the vial from source `well` ends when it goes to `destination`."""
        return destination.bay, dict(self.pairs(destination))[well]


def decide_route(process: Process, measured: dict):
    """Where a vial goes: (the destination, the route rule that chose it, or None for the default)."""
    for rule in process.routes:
        if rule.when.holds(measured):
            return rule.then, rule
    return process.destination, None


class WorkflowSpec(_Strict):
    """A whole plan: one or more processes that run at the same time."""
    version: Literal[1] = 1
    name: str = Field(pattern="^[a-z][a-z0-9_]*$", description='Short name, e.g. "rows_c_to_f".')
    summary: str = Field(description="One or two sentences, for the person approving the plan.")
    processes: list[Process] = Field(min_length=1)
    transit: list[TransitWell] = Field(default_factory=list, description=(
        "Only when moves form a cycle (a swap: A1 to H1 while H1 to A1): one free well per cycle, chosen from "
        "rack_inventory, that no vial of the plan is taken from or put into. One vial of each cycle waits there "
        "until its destination is free. Leave empty otherwise."))
