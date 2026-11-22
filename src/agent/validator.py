"""The validator: a WorkflowSpec against the deck as it is now. Plain code, no model.

The schema (spec.py) already guarantees that a spec is well formed: known steps, values within limits, rules
that test something measured earlier. What it cannot know is the deck. The validator asks the inventory and
checks what would go wrong on the hardware:

  errors (the plan cannot run)                     warnings (it can, but a person should know)
  - a source or destination bay has no rack        - some wells of a source row are empty: those are
  - a source row holds no vial at all                skipped
  - a destination well is already taken
  - two processes take the same vials, or put
    vials into the same destination wells

`submit_workflow` runs it, so Claude gets the errors back and can fix the plan before it is accepted. The
result also lists the vials each process will move, which the compiler builds threads for: only wells that
hold a vial, so an empty well never stalls a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .inventory import Inventory
from .spec import WorkflowSpec


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    vials: dict[str, list[str]] = field(default_factory=dict)     # process id -> the source wells it will move

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self) -> str:
        lines = [f"error: {e}" for e in self.errors] + [f"warning: {w}" for w in self.warnings]
        return "\n".join(lines) or "no problems found"


def validate(spec: WorkflowSpec, inventory: Inventory) -> Report:
    report, racks = Report(), {}

    def rack(bay: int) -> dict:
        if bay not in racks:
            racks[bay] = inventory.rack(bay)
        return racks[bay]

    taken_from: dict[tuple, str] = {}       # (bay, well) -> the process that takes the vial standing there
    placed_into: dict[tuple, str] = {}      # (bay, well) -> the process that puts a vial there
    clashes: dict[tuple, list] = {}         # (what, bay, process, other process) -> the wells they share

    for p in spec.processes:
        src, dst = p.source, p.destination
        if not rack(src.bay)["present"]:
            report.errors.append(f"{p.id}: bay {src.bay} has no rack to take vials from")
            continue
        if not rack(dst.bay)["present"]:
            report.errors.append(f"{p.id}: bay {dst.bay} has no rack to put vials into")
            continue
        occupied = set(rack(src.bay)["occupied"])

        vials = []
        for row in src.rows:
            row_wells = [w for w in src.wells() if w[0] == row]
            here = [w for w in row_wells if w in occupied]
            if not here:
                report.errors.append(f"{p.id}: row {row} of bay {src.bay} holds no vials")
            elif len(here) < len(row_wells):
                empty = [w for w in row_wells if w not in occupied]
                report.warnings.append(f"{p.id}: {len(empty)} empty well(s) in row {row} of bay {src.bay} "
                                       f"will be skipped ({_short(empty)})")
            vials += here
        report.vials[p.id] = vials

        for well in vials:
            if (src.bay, well) in taken_from:
                clashes.setdefault(("take the vials in", src.bay, p.id, taken_from[(src.bay, well)]), []).append(well)
            taken_from.setdefault((src.bay, well), p.id)

        # A destination well must be free, unless the vial is going back to the well it came from.
        dst_taken = set(rack(dst.bay)["occupied"])
        blocked = [w for w in vials if w in dst_taken and dst.bay != src.bay]
        if blocked:
            report.errors.append(f"{p.id}: {len(blocked)} destination well(s) in bay {dst.bay} already hold a "
                                 f"vial ({_short(blocked)})")
        for well in vials:
            if (dst.bay, well) in placed_into and placed_into[(dst.bay, well)] != p.id:
                clashes.setdefault(("put vials into", dst.bay, p.id, placed_into[(dst.bay, well)]), []).append(well)
            placed_into.setdefault((dst.bay, well), p.id)

    for (what, bay, one, other), wells in clashes.items():
        report.errors.append(f"{one} and {other} both {what} bay {bay} {_short(wells)}")
    return report


def _short(wells: list[str], show: int = 6) -> str:
    return ", ".join(wells[:show]) + (f" and {len(wells) - show} more" if len(wells) > show else "")
