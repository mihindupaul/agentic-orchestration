"""The agent's window: type or load a request, let Claude plan it, check it, run it on the cell, watch it, stop it.

    .venv-orca/bin/python -m agent.ui

  Request   type what should happen, or load one of examples/agent_requests/
  Plan      Claude plans against the deck (the live controller, or the demo deck); shown: Claude's reply, the
            plan as a tree, the validator's findings and the size of the job
  Run       asks for approval, checks the deck once more, then compiles the plan and runs it through Orca and
            the controller; shown: Orca's phase for the run, every vial's status, and a feed of what Orca does
  Stop      Orca's operator stop (pause, then a confirmed abort); the heater is switched off afterwards

Planning and running each happen in a QThread of their own, so the window stays responsive: the planner waits
on the API, and the run needs its own asyncio event loop for Orca. They report back through Qt signals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .env import load_env
from .examples import all_examples
from .inventory import ControllerInventory, DemoInventory
from .planner import PlanningError, plan_workflow
from .spec import Heat, WorkflowSpec
from .validator import Report, validate

COLUMNS = ["Vial", "Process", "Status", "Code", "Grams", "°C", "Rules"]


# ------------------------------------------------------------------------------------------- workers
class PlanWorker(QThread):
    """Runs the planner (a blocking API conversation) off the window's thread."""
    tool_call = Signal(str)
    planned = Signal(object)
    failed = Signal(str)

    def __init__(self, request: str, inventory, history: list | None = None):
        super().__init__()
        self.request, self.inventory, self.history = request, inventory, history

    def run(self) -> None:
        def show(message) -> None:
            for block in message.content:
                if block.type == "tool_use":
                    self.tool_call.emit(f"Claude calls {block.name}({json.dumps(block.input)[:90]})")
        try:
            self.planned.emit(plan_workflow(self.request, self.inventory, history=self.history,
                                             on_message=show))
        except PlanningError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:                       # network, credentials, controller not reachable ...
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class RunWorker(QThread):
    """Compiles the plan and runs it through Orca, in an asyncio event loop of its own."""
    state = Signal(str)                                # building / running / finished
    ended = Signal(str, str)                           # outcome (completed / stopped / failed), detail

    def __init__(self, spec: WorkflowSpec, vials: dict, host: str, port: int):
        super().__init__()
        self.spec, self.vials, self.host, self.port = spec, vials, host, port
        self.plan = self.loop = self.task = self.runtime = self.execution_id = None
        self.stopping = False

    def run(self) -> None:
        from orca_workcell.core import ControllerConfig

        from .compiler import compile_spec, run

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.plan = compile_spec(self.spec, ControllerConfig(ctl_host=self.host, ctl_port=self.port), self.vials)
            self.task = self.loop.create_task(run(self.plan, on_state=self.state.emit, on_submitted=self._submitted))
            self.loop.run_until_complete(self.task)
            self.ended.emit("completed", "")
        except asyncio.CancelledError:
            self.ended.emit("stopped", "stopped before Orca started the run")
        except Exception as exc:
            self.ended.emit("stopped" if self.stopping else "failed", str(exc))
        finally:
            self.loop.close()

    def _submitted(self, runtime, execution_id: str) -> None:
        self.runtime, self.execution_id = runtime, execution_id

    def orca_phase(self) -> str:
        """Orca's own phase for this run (accepting, draining, stopping, completed, failed, aborted)."""
        if self.runtime is None:
            return "-"
        try:
            status = self.runtime.get_execution_status(self.execution_id)
        except Exception:
            return "-"
        return status.status.value + (" (paused)" if status.paused else "")

    def stop(self) -> None:
        """Orca's operator stop takes two calls: the first pauses the run and arms the abort, the second
        (confirmed) aborts it. Before Orca has started the run, the build is cancelled instead."""
        self.stopping = True
        if self.runtime is None:
            if self.loop and self.task:
                self.loop.call_soon_threadsafe(self.task.cancel)
            return

        async def stop_run():
            await self.runtime.stop_execution(self.execution_id)
            await self.runtime.stop_execution(self.execution_id, confirm=True)
        asyncio.run_coroutine_threadsafe(stop_run(), self.loop)


class QuestionDialog(QDialog):
    """Claude's question, an answer box, and OK / Abort."""

    def __init__(self, question: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Claude asks")
        self.resize(560, 420)
        text = QTextBrowser()
        text.setMarkdown(question)
        self.box = QPlainTextEdit(placeholderText="Your answer")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Abort)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        for w in (text, QLabel("Answer to continue planning, or Abort to stop:"), self.box, buttons):
            layout.addWidget(w)
        self.box.setFocus()

    def answer(self) -> str | None:
        return self.box.toPlainText().strip() or None


class Feed(QObject, logging.Handler):
    """Orca's log, filtered to what an operator wants to see, as a Qt signal (safe from any thread)."""
    line = Signal(str)
    KEEP = (" picked", " placed", "Action completed", "registered ")

    def __init__(self):
        QObject.__init__(self)
        logging.Handler.__init__(self, level=logging.INFO)

    def emit(self, record: logging.LogRecord) -> None:   # logging.Handler.emit
        text = record.getMessage()
        if record.levelno >= logging.WARNING or any(k in text for k in self.KEEP):
            self.line.emit(f"{record.levelname[0]} {text}")


# ------------------------------------------------------------------------------------------- window
class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Workcell planning agent")
        self.resize(760, 900)
        self.result = self.report = self.planner = self.runner = None
        self.conversation: list = []                    # the turns so far, to continue with an answer
        self.rows: dict[str, int] = {}

        # request
        self.request = QPlainTextEdit(placeholderText="What should happen? e.g. Scan and weigh row C of bay 3, "
                                                      "then put the vials in the same wells of bay 8.")
        self.load_button = QPushButton("Load example ▾")
        self.load_button.setMenu(self._examples_menu())
        self.plan_button = QPushButton("Plan", clicked=self.plan)
        self.controller = QLineEdit(f"{os.environ.get('CONTROLLER_HOST', '127.0.0.1')}:"
                                    f"{os.environ.get('CONTROLLER_PORT', '8700')}")
        self.demo_deck = QCheckBox("Demo deck (plan only)")
        row = QHBoxLayout()
        for w in (self.load_button, QLabel("Controller"), self.controller, self.demo_deck):
            row.addWidget(w)
        row.addStretch()
        row.addWidget(self.plan_button)
        request_box = self._box("Request", self.request, row)

        # plan
        self.reply = QTextBrowser()
        self.tree = QTreeWidget(headerHidden=True)
        self.findings = QLabel(wordWrap=True)
        plan_box = self._box("Plan", self.reply, self.tree, self.findings)

        # run
        self.run_button = QPushButton("Run on the cell", enabled=False, clicked=self.run)
        self.stop_button = QPushButton("Stop", enabled=False, clicked=self.stop)
        self.state = QLabel("idle")
        self.phase = QLabel("Orca: -")
        bold = QFont()
        bold.setBold(True)
        self.state.setFont(bold)
        buttons = QHBoxLayout()
        for w in (self.run_button, self.stop_button, self.state, self.phase):
            buttons.addWidget(w)
        buttons.addStretch()
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.feed = QPlainTextEdit(readOnly=True, maximumBlockCount=3000)
        self.feed.setFont(QFont("Menlo", 11))
        run_box = self._box("Run", buttons, self.table, self.feed)

        # the request on top; the plan and the run share the space below as two tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(plan_box, "Plan")
        self.tabs.addTab(run_box, "Run")
        main = QSplitter(Qt.Vertical)
        main.addWidget(request_box)
        main.addWidget(self.tabs)
        main.setSizes([200, 700])
        self.setCentralWidget(main)

        self.timer = QTimer(self, interval=500, timeout=self.refresh)
        self.log_feed = Feed()
        self.log_feed.line.connect(self.say)
        logging.getLogger("orca").addHandler(self.log_feed)

    # ---------------------------------------------------------------- helpers
    def _box(self, title: str, *parts) -> QGroupBox:
        box, layout = QGroupBox(title), QVBoxLayout()
        for part in parts:
            (layout.addLayout if isinstance(part, QHBoxLayout) else layout.addWidget)(part)
        box.setLayout(layout)
        return box

    def _examples_menu(self) -> QMenu:
        menu = QMenu(self)
        for example in all_examples():
            action = QAction(f"{example.name}   ({'plan' if example.expect_plan else 'no plan'} expected)", menu)
            action.setToolTip(example.notes)
            action.triggered.connect(lambda _=False, e=example: self.load_request(e.request))
            menu.addAction(action)
        menu.setToolTipsVisible(True)
        return menu

    def _address(self) -> tuple[str, int]:
        host, port = self.controller.text().strip().rsplit(":", 1)
        return host, int(port)

    def _inventory(self):
        return DemoInventory() if self.demo_deck.isChecked() else ControllerInventory(*self._address())

    def load_request(self, text: str) -> None:
        """A new request (an example loaded): forget the previous conversation and plan, and clear their views.
        A run still going keeps its Run tab."""
        self.request.setPlainText(text)
        self.conversation, self.result, self.report = [], None, None
        self.reply.clear()
        self.tree.clear()
        self.findings.clear()
        self.run_button.setEnabled(False)
        self.tabs.setCurrentIndex(0)
        if self.runner is None or not self.runner.isRunning():
            self.table.setRowCount(0)
            self.rows = {}
            self.feed.clear()
            self.phase.setText("Orca: -")
            self.set_state("idle")

    def say(self, text: str) -> None:
        self.feed.appendPlainText(text)

    def set_state(self, text: str) -> None:
        self.state.setText(text)

    # ---------------------------------------------------------------- plan
    def plan(self) -> None:
        """Plan the request box's text as a new conversation."""
        request = self.request.toPlainText().strip()
        if request:
            self.conversation = []
            self._ask(request)

    def _ask(self, text: str) -> None:
        self.result = self.report = None
        self._show_conversation(pending=text)
        self.tree.clear()
        self.findings.clear()
        self.run_button.setEnabled(False)
        self.plan_button.setEnabled(False)
        self.set_state("planning")
        self.tabs.setCurrentIndex(0)                    # the Plan tab
        self.planner = PlanWorker(text, self._inventory(), history=self.conversation)
        self.planner.tool_call.connect(self.say)
        self.planner.planned.connect(self.planned)
        self.planner.failed.connect(self.planning_failed)
        self.planner.start()

    def planning_failed(self, message: str) -> None:
        self._show_conversation()
        self.reply.append(f"\n**Planning failed:** {message}")
        self.plan_button.setEnabled(True)
        self.set_state("idle")

    def planned(self, result) -> None:
        self.result, self.report, self.conversation = result, result.report, result.conversation
        self._show_conversation()
        self.plan_button.setEnabled(True)
        if result.spec is not None:                     # shown even when not ready, so the question has context
            self._show_spec(result.spec)
            self._show_findings(result.spec, result.report)
        if not result.ready:
            # Not ready to run: Claude asked something, explained what is in the way, or wants a choice confirmed
            # before it marks its plan ready. Ask the person in a popup: an answer continues the same
            # conversation, Abort ends the planning.
            self.set_state("waiting for your answer")
            answer = self.ask_person(result.reply)
            if answer:
                self._ask(answer)
            else:
                self.findings.setText(self.findings.text() + "<br>Planning stopped: no plan ready to run.")
                self.set_state("planning stopped")
            return
        self.run_button.setEnabled(not self.demo_deck.isChecked())
        self.set_state("planned: review, then run" if not self.demo_deck.isChecked() else "planned (demo deck)")

    def ask_person(self, question: str) -> str | None:
        """Claude's question in a popup with an answer box. Returns the answer, or None for Abort (or no answer)."""
        dialog = QuestionDialog(question, self)
        return dialog.answer() if dialog.exec() == QDialog.Accepted else None

    def _show_conversation(self, pending: str | None = None) -> None:
        """The whole exchange so far, newest last; `pending` is the text Claude is working on now."""
        turns = [("**You:** " if t["role"] == "user" else "**Claude:** ") + t["content"] for t in self.conversation]
        if pending:
            turns += [f"**You:** {pending}", "*Claude is planning ...*"]
        self.reply.setMarkdown("\n\n---\n\n".join(turns))
        self.reply.verticalScrollBar().setValue(self.reply.verticalScrollBar().maximum())

    def _show_spec(self, spec: WorkflowSpec) -> None:
        root = QTreeWidgetItem([f"{spec.name}: {spec.summary}"])
        for p in spec.processes:
            item = QTreeWidgetItem([f"{p.id}: bay {p.source.bay} rows {p.source.rows} -> bay {p.destination.bay}, "
                                    f"same wells"])
            for step in p.steps:
                values = ", ".join(f"{k} {v}" for k, v in step.model_dump(exclude={"op", "rules"}).items())
                s = QTreeWidgetItem([f"{step.op}" + (f" ({values})" if values else "")])
                for rule in step.rules:
                    then = rule.then if rule.then == "skip" else rule.then.model_dump(exclude_none=True)
                    s.addChild(QTreeWidgetItem([f"if {rule.when.describe()}: {then}"]))
                item.addChild(s)
            root.addChild(item)
        self.tree.addTopLevelItem(root)
        self.tree.expandAll()

    def _show_findings(self, spec: WorkflowSpec, report: Report) -> None:
        lines, moves, heater = [], 0, [0, 0]
        for p in spec.processes:
            n = len(report.vials.get(p.id, []))
            heats = [s for s in p.steps if isinstance(s, Heat)]
            optional = sum(1 for s in heats if any(r.then == "skip" for r in s.rules))
            moves += n * (len(p.steps) + 1)
            heater[0] += n * (len(heats) - optional)
            heater[1] += n * len(heats)
            lines.append(f"{p.id}: {n} vials, " + (f"{len(p.steps)} step(s)" if p.steps else "rack-to-rack move"))
        size = f"Job size: {sum(len(v) for v in report.vials.values())} vials, up to {moves} arm moves"
        if heater[1]:
            size += (f", {heater[0]}" + (f" to {heater[1]}" if heater[1] != heater[0] else "") + " heater visits")
        checks = "<br>".join(f"⚠ {w}" for w in report.warnings) or "Deck check: no problems found."
        self.findings.setText("<br>".join(lines) + f"<br><b>{size}</b><br>{checks}")

    # ---------------------------------------------------------------- run
    def run(self) -> None:
        spec = self.result.spec
        host, port = self._address()
        report = validate(spec, ControllerInventory(host, port))      # the deck may have changed since planning
        if not report.ok:
            QMessageBox.warning(self, "Not run", "The deck no longer fits the plan:\n\n" + report.text())
            return
        vials = sum(len(v) for v in report.vials.values())
        answer = QMessageBox.question(self, "Approve the plan",
                                      f"Run '{spec.name}' on the cell at {host}:{port}?\n\n{spec.summary}\n\n"
                                      f"{vials} vials will be moved.")
        if answer != QMessageBox.Yes:
            return
        self._fill_table(spec, report)
        self.feed.clear()
        self.say(f"approved: {spec.name} ({vials} vials)")
        self.run_button.setEnabled(False)
        self.plan_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.tabs.setCurrentIndex(1)                    # the Run tab
        self.runner = RunWorker(spec, report.vials, host, port)
        self.runner.state.connect(lambda s: self.set_state(
            {"building": "building the Orca system (arm routes)", "running": "running"}.get(s, s)))
        self.runner.ended.connect(self.run_ended)
        self.runner.start()
        self.timer.start()

    def _fill_table(self, spec: WorkflowSpec, report: Report) -> None:
        from .compiler import vial_name
        self.table.setRowCount(0)
        self.rows = {}
        for p in spec.processes:
            for well in report.vials[p.id]:
                name = vial_name(p.source.bay, well)
                self.rows[name] = self.table.rowCount()
                self.table.insertRow(self.table.rowCount())
                for col, text in enumerate([f"bay {p.source.bay} {well}", p.id, "waiting"]):
                    self.table.setItem(self.rows[name], col, QTableWidgetItem(text))

    def refresh(self) -> None:
        """Every half second while running: Orca's phase and every vial's record."""
        if not self.runner:
            return
        self.phase.setText(f"Orca: {self.runner.orca_phase()}")
        results = self.runner.plan.results if self.runner.plan else {}
        done = 0
        for name, record in list(results.items()):
            row = self.rows.get(name)
            if row is None:
                continue
            done += record["status"] in ("done", "no_read")
            values = [record["status"], record.get("code") or "", _num(record.get("grams"), 3),
                      _num(record.get("celsius"), 1), "; ".join(record.get("rules", []))]
            for col, text in enumerate(values, start=2):
                self.table.setItem(row, col, QTableWidgetItem(str(text)))
        if self.state.text().startswith("running"):
            self.set_state(f"running: {done} of {len(self.rows)} vials through their steps")

    def stop(self) -> None:
        if self.runner:
            self.stop_button.setEnabled(False)
            self.set_state("stopping")
            self.say("stop requested: pausing, then aborting the Orca run")
            self.runner.stop()

    def run_ended(self, outcome: str, detail: str) -> None:
        self.timer.stop()
        self.refresh()
        self.phase.setText(f"Orca: {self.runner.orca_phase()}")
        self.set_state({"completed": "completed", "stopped": "stopped: the heater is off; vials in an instrument "
                        "or the gripper stay there", "failed": f"failed: {detail}"}[outcome])
        self.say(f"run {outcome}" + (f": {detail}" if detail else ""))
        self.stop_button.setEnabled(False)
        self.plan_button.setEnabled(True)
        self.run_button.setEnabled(outcome != "completed")


def _num(value, digits: int) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def main() -> None:
    load_env()                                         # ANTHROPIC_API_KEY from .env
    logging.getLogger("orca").setLevel(logging.INFO)
    app = QApplication(sys.argv)
    window = Window()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
