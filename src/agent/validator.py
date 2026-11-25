"""The validator: a WorkflowSpec against the deck as it is now. Plain code, no model.

The schema (spec.py) already guarantees that a spec is well formed: known steps, values within limits, rules
that test something measured earlier, a destination that fits its source. What it cannot know is the deck. The
validator asks the inventory and checks what would go wrong on the hardware:

  errors (the plan cannot run)                     warnings (it can, but a person should know)
  - a source or destination bay has no rack        - some wells of a source row are empty: those are
  - a source row holds no vial at all                skipped
  - a destination well holds a vial the plan
    does not move away
  - two processes take the same vials, or put
    vials into the same destination wells
  - moves that form a cycle (a swap) without a
    free transit well for each cycle
  - a well a route may send a vial to is taken,
    or used by another vial of the plan

A destination well may hold a vial now if the plan moves that vial out: a chain (A1 -> B1 while B1 -> C1)
runs because Orca does not put a vial into a well until the vial there has been picked. A cycle (A1 -> H1
while H1 -> A1) cannot: one vial of it has to wait somewhere else first. The agent names those places
(`spec.transit`, free wells it found with rack_inventory); the validator checks them and assigns one to each
cycle.

A process with routes decides each vial's destination while it runs, so every well a vial may end in (each
route's and the default) has to be free now and used by no other vial of the plan, unless it is the vial's own
well. That keeps vials with routes out of chains and swaps, whose order is fixed before the run.

`submit_workflow` runs it, so Claude gets the errors back and can fix the plan before it is accepted. The
result also lists the moves of each process, which the compiler builds threads for: only wells that hold a
vial, so an empty well never stalls a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .inventory import Inventory
from .spec import WorkflowSpec

Place = tuple      # (bay, well)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    vials: dict[str, list[str]] = field(default_factory=dict)       # process id -> the source wells it will move
    moves: dict[str, list[tuple]] = field(default_factory=dict)     # process id -> (source well, destination well)
    routes: dict[Place, Place] = field(default_factory=dict)        # source place -> the transit place it waits in
    candidates: dict[Place, list] = field(default_factory=dict)     # source place -> every place a route may send it

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

    taken_from: dict[Place, str] = {}       # (bay, well) -> the process that takes the vial standing there
    placed_into: dict[Place, str] = {}      # (bay, well) -> the process that puts a vial there
    clashes: dict[tuple, list] = {}         # (what, bay, process, other process) -> the wells they share
    goes_to: dict[Place, Place] = {}        # every move of the plan: source place -> destination place
    checked = []                            # (process, its moves) for the destination check below

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
        for group, group_wells in src.groups():           # a row, a column, or the chosen wells
            here = [w for w in group_wells if w in occupied]
            if not here:
                report.errors.append(f"{p.id}: {group} of bay {src.bay} holds no vials")
            elif len(here) < len(group_wells):
                empty = [w for w in group_wells if w not in occupied]
                report.warnings.append(f"{p.id}: {len(empty)} empty well(s) in {group} of bay {src.bay} "
                                       f"will be skipped ({_short(empty)})")
            vials += [w for w in here if w not in vials]
        report.vials[p.id] = vials
        moves = [(s, d) for s, d in p.pairs() if s in vials]     # an empty source well moves nothing
        report.moves[p.id] = moves
        checked.append((p, moves))

        for well, target in moves:
            if (src.bay, well) in taken_from:
                clashes.setdefault(("take the vials in", src.bay, p.id, taken_from[(src.bay, well)]), []).append(well)
            taken_from.setdefault((src.bay, well), p.id)
            if (dst.bay, target) in placed_into:
                clashes.setdefault(("put vials into", dst.bay, p.id, placed_into[(dst.bay, target)]), []).append(target)
            placed_into.setdefault((dst.bay, target), p.id)
            goes_to[(src.bay, well)] = (dst.bay, target)

    # A destination well must be free now, or hold a vial the plan moves out (or the very vial going there).
    for p, moves in checked:
        if p.routes:
            continue                                       # checked with its routes below
        dst_taken = set(rack(p.destination.bay)["occupied"])
        blocked = [d for s, d in moves if d in dst_taken and (p.destination.bay, d) not in goes_to]
        if blocked:
            report.errors.append(f"{p.id}: {len(blocked)} destination well(s) in bay {p.destination.bay} hold a "
                                 f"vial the plan does not move away ({_short(blocked)})")

    for (what, bay, one, other), wells in clashes.items():
        report.errors.append(f"{one} and {other} both {what} bay {bay} {_short(wells)}")

    _check_routes(checked, report, rack, goes_to)
    _check_transit(spec, report, rack, goes_to)
    return report


def _check_routes(checked: list, report: Report, rack, goes_to: dict[Place, Place]) -> None:
    """Every place a vial with routes may end in: a rack there, free now, and nobody else's."""
    for p, moves in checked:
        if not p.routes:
            continue
        for dst in p.candidates():
            if not rack(dst.bay)["present"]:
                report.errors.append(f"{p.id}: a route goes to bay {dst.bay}, which has no rack")
        if not report.ok:
            return
        for well, _ in moves:
            source = (p.source.bay, well)
            report.candidates[source] = [p.target(well, dst) for dst in p.candidates()]
    arriving = {dst: src for src, dst in goes_to.items()}  # place -> the vial whose default move ends there
    claimed: dict[Place, Place] = {}                       # place -> the vial (source place) that may end there
    for p, moves in checked:
        if not p.routes:
            continue
        taken, shared = [], []
        for well, _ in moves:
            source = (p.source.bay, well)
            for place in dict.fromkeys(report.candidates[source]):
                if place == source:
                    continue                               # back to its own well: free once it has left
                someone_else = (place in goes_to                                   # another vial's source
                                or arriving.get(place, source) != source           # another vial's destination
                                or claimed.get(place, source) != source)           # another routed vial's option
                if place[1] in rack(place[0])["occupied"]:
                    taken.append(place)
                elif someone_else:
                    shared.append(place)
                claimed.setdefault(place, source)
        if taken:
            report.errors.append(f"{p.id}: {len(taken)} well(s) a vial may be sent to hold a vial "
                                 f"({_short([f'bay {b} {w}' for b, w in taken])}); a vial with routes needs free wells")
        if shared:
            report.errors.append(f"{p.id}: {len(shared)} well(s) a vial may be sent to are used by other vials of the "
                                 f"plan ({_short([f'bay {b} {w}' for b, w in shared])})")


def cycles(goes_to: dict[Place, Place]) -> list[list[Place]]:
    """The moves that form cycles: [[A1, H1], ...] for A1 -> H1 and H1 -> A1. Every place has at most one move
    out and one in, so following the moves from a place either ends at a free well (a chain) or comes back."""
    found, seen = [], set()
    for start in goes_to:
        if start in seen:
            continue
        path, here = [], start
        while here in goes_to and here not in seen and here not in path:
            path.append(here)
            here = goes_to[here]
        if here in path:                                   # came back: the part from `here` on is a cycle
            loop = path[path.index(here):]
            if len(loop) > 1:                              # a vial put back into its own well is no cycle
                found.append(loop)
        seen.update(path)
    return found


def _check_transit(spec: WorkflowSpec, report: Report, rack, goes_to: dict[Place, Place]) -> None:
    used = set(goes_to) | set(goes_to.values()) | {place for options in report.candidates.values() for place in options}
    free = []
    for t in spec.transit:
        place = (t.bay, t.well)
        if not rack(t.bay)["present"]:
            report.errors.append(f"transit well {t.describe()}: bay {t.bay} has no rack")
        elif place in used:
            report.errors.append(f"transit well {t.describe()} is a source or destination of the plan; pick a well "
                                 f"the plan does not use")
        elif t.well in rack(t.bay)["occupied"]:
            report.errors.append(f"transit well {t.describe()} holds a vial; pick a free well")
        elif place in free:
            report.errors.append(f"transit well {t.describe()} is listed twice")
        else:
            free.append(place)

    loops = cycles(goes_to)
    if len(loops) > len(spec.transit):
        shown = "; ".join(" -> ".join(f"bay {b} {w}" for b, w in loop) + " -> back" for loop in loops[:3])
        more = f" and {len(loops) - 3} more" if len(loops) > 3 else ""
        report.errors.append(f"{len(loops)} set(s) of moves form a cycle ({shown}{more}): each needs a free "
                             f"transit well where one vial waits; add {len(loops) - len(spec.transit)} more to "
                             f"transit, free wells (rack_inventory) that the plan does not use")
        return
    if report.ok:
        for loop, place in zip(loops, free):
            report.routes[loop[0]] = place
        unused = len(free) - len(loops)
        if unused > 0:
            report.warnings.append(f"{unused} transit well(s) not needed: no cycle uses them")


def _short(wells: list[str], show: int = 6) -> str:
    return ", ".join(wells[:show]) + (f" and {len(wells) - show} more" if len(wells) > show else "")
