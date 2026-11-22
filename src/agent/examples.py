"""Example requests kept as text files in `examples/agent_requests/` at the top of the repository: demos of
what a person can ask, and test cases.

A file holds the request, plus `#` lines for people that are not sent to Claude. One of them says what the
agent should do: `# expect: plan` (hand in a spec) or `# expect: no plan` (explain or ask instead).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FOLDER = Path(__file__).resolve().parents[2] / "examples" / "agent_requests"      # <repo>/examples/agent_requests


@dataclass
class Example:
    name: str            # file name without .txt, e.g. "03_rules_weight_and_code"
    request: str         # what is sent to Claude
    expect_plan: bool    # True: a spec should come back; False: an explanation or a question
    notes: str           # the other # lines


def parse(name: str, text: str) -> Example:
    notes, request, expect = [], [], None
    for line in text.splitlines():
        if line.startswith("#"):
            note = line.lstrip("#").strip()
            if note.lower().startswith("expect:"):
                expect = note.split(":", 1)[1].strip().lower()
            else:
                notes.append(note)
        elif line.strip():
            request.append(line.strip())
    if expect not in ("plan", "no plan"):
        raise ValueError(f"{name}: needs a line '# expect: plan' or '# expect: no plan'")
    if not request:
        raise ValueError(f"{name}: has no request")
    return Example(name, " ".join(request), expect == "plan", "\n".join(notes))


def all_examples(folder: Path = FOLDER) -> list[Example]:
    return [parse(p.stem, p.read_text()) for p in sorted(folder.glob("*.txt"))]


def find(key: str, folder: Path = FOLDER) -> Example:
    """An example by number ("3", "03"), by name or name prefix, or a path to any text file."""
    path = Path(key)
    if path.suffix == ".txt" and path.is_file():
        return parse(path.stem, path.read_text())
    matches = [e for e in all_examples(folder)
               if e.name == key or e.name.startswith(key) or (key.isdigit() and e.name.split("_")[0] == f"{int(key):02d}")]
    if len(matches) != 1:
        raise LookupError(f"no single example matches {key!r} ({len(matches)} found); see python -m agent --list")
    return matches[0]
