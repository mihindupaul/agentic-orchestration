"""The agent's window: type or load a request, let Claude plan it, check it, run it on the cell, watch it, stop it.

    .venv-orca/bin/python -m agent.ui

Three steps, one page each, with a step bar on top:

  1 Describe  type what should happen, or load one of examples/agent_requests/, then Start planning: Claude
              plans against the deck (the live controller, or the demo deck); its replies show in the
              conversation, and a question comes up in a popup
  2 Plan      the plan as a tree, the validator's findings and the size of the job; Describe in the step bar if
              it is not right, Go to run if it is (the step bar switches between steps at any time)
  3 Run       asks for approval, checks the deck once more, then compiles the plan and runs it through Orca and
              the controller; shown: Orca's phase for the run, every vial's status, and a feed of what Orca
              does. Stop is Orca's operator stop (pause, then a confirmed abort); the heater is switched off
  ⚙         the controller's host and port, and the demo deck; the pill next to it shows whether the
            controller answers (checked every few seconds)

Planning and running each happen in a QThread of their own, so the window stays responsive: the planner waits
on the API, and the run needs its own asyncio event loop for Orca. They report back through Qt signals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtNetwork import QTcpSocket
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
                               QFormLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QStackedWidget,
                               QTableWidget, QTableWidgetItem,
                               QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .env import load_env
from .examples import all_examples
from .inventory import ControllerInventory, DemoInventory
from .planner import PlanningError, plan_workflow
from .spec import Heat, WorkflowSpec
from .validator import Report, validate

COLUMNS = ["Vial", "Process", "Status", "Code", "Grams", "°C", "Rules"]
APP_NAME = "Agentic lab automation"               # the title bar: the project's name, as in the README
DESCRIBE, PLAN, RUN = range(3)                     # the steps, in order
STEP_NAMES = ["Describe", "Plan", "Run"]


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
                    self.tool_call.emit(f"{block.name}({json.dumps(block.input)[:90]})")
        try:
            self.planned.emit(plan_workflow(self.request, self.inventory, history=self.history,
                                             on_message=show))
        except PlanningError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:                       # network, credentials, controller not reachable ...
            self.failed.emit(f"{type(exc).__name__}: {exc}. Did you set ANTHROPIC_API_KEY in .env ?")


class RunWorker(QThread):
    """Compiles the plan and runs it through Orca, in an asyncio event loop of its own."""
    state = Signal(str)                                # building / running / finished
    ended = Signal(str, str)                           # outcome (completed / stopped / failed), detail

    def __init__(self, spec: WorkflowSpec, report: Report, host: str, port: int):
        super().__init__()
        self.spec, self.report, self.host, self.port = spec, report, host, port
        self.plan = self.loop = self.task = self.runtime = self.execution_id = None
        self.stopping = False

    def run(self) -> None:
        from orca_workcell.core import ControllerConfig

        from .compiler import compile_spec, run

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.plan = compile_spec(self.spec, ControllerConfig(ctl_host=self.host, ctl_port=self.port), self.report)
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

    def vials_finished(self) -> int:
        """Vials that reached their destination: Orca reports their threads as completed."""
        if self.runner_ready():
            try:
                return sum(t.status == "COMPLETED" for t in self.runtime.threads.list(self.execution_id))
            except Exception:
                pass
        return 0

    def runner_ready(self) -> bool:
        return self.runtime is not None and self.execution_id is not None

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


class ClickableLabel(QLabel):
    """A label that acts on a click (the gear: a symbol, not a button with a frame), with a hand cursor."""
    clicked = Signal()

    def __init__(self, text: str = "", **kwargs):
        super().__init__(text, **kwargs)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def click(self) -> None:
        self.clicked.emit()


class StepBar(QWidget):
    """1 Describe  >  2 Plan  >  3 Run: the current step highlighted; a step that can be shown is a button, a
    step that cannot yet (no plan to review, no plan ready to run) is greyed out."""

    def __init__(self, names: list, on_click, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        self.buttons = []
        for i, name in enumerate(names):
            if i:
                arrow = QLabel("›")
                arrow.setStyleSheet("color: gray; font-size: 16pt")
                layout.addWidget(arrow)
            button = QPushButton(f"{i + 1}  {name}", flat=True, clicked=lambda _=False, step=i: on_click(step))
            button.setCursor(Qt.PointingHandCursor)
            self.buttons.append(button)
            layout.addWidget(button)
        layout.addStretch()

    def show_steps(self, current: int, reachable: set) -> None:
        for i, button in enumerate(self.buttons):
            button.setEnabled(i in reachable)
            if i == current:
                style = "background: #1565c0; color: white; font-weight: 600"
            elif i in reachable:
                style = "color: #1565c0"
            else:
                style = "color: #9e9e9e"
            button.setStyleSheet(f"QPushButton {{ {style}; border-radius: 12px; padding: 4px 14px; "
                                 f"font-size: 13pt }}")


class SettingsDialog(QDialog):
    """The gear's dialog: where the controller is, and whether to plan against the demo deck instead."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Controller")
        self.host = QLineEdit(os.environ.get("CONTROLLER_HOST", "127.0.0.1"))
        self.port = QSpinBox(minimum=1, maximum=65535, value=int(os.environ.get("CONTROLLER_PORT", "8700")))
        self.demo_deck = QCheckBox("Demo deck (plan only, no controller)")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.addRow("Host", self.host)
        form.addRow("Port", self.port)
        form.addRow(self.demo_deck)
        form.addRow(buttons)

    def values(self) -> tuple:
        return self.host.text().strip(), self.port.value(), self.demo_deck.isChecked()

    def set_values(self, host: str, port: int, demo: bool) -> None:
        self.host.setText(host)
        self.port.setValue(port)
        self.demo_deck.setChecked(demo)


class ConnectionCheck(QObject):
    """Asks the controller for its status without blocking the window: a Qt socket, signals, a time limit.
    Reports "connected", "fault" (it answers, with a fault latched) or "offline", and a detail for the tooltip."""
    result = Signal(str, str)
    TIMEOUT_MS = 1500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket = None
        self.timer = QTimer(self, singleShot=True, interval=self.TIMEOUT_MS, timeout=self._timed_out)

    def check(self, host: str, port: int) -> None:
        address = f"{host}:{port}"
        if self.socket is not None:
            if address == self.address:
                return                                 # the last check is still waiting for its answer
            self._drop()                               # the address changed: its answer no longer matters
        self.address = address
        self.socket, self.buffer = QTcpSocket(self), b""
        self.socket.connected.connect(lambda: self.socket.write(b'{"id": 1, "cmd": "status"}\n'))
        self.socket.readyRead.connect(self._read)
        self.socket.errorOccurred.connect(lambda _: self._done("offline", f"no controller at {self.address}"))
        self.timer.start()
        self.socket.connectToHost(host, port)

    def _read(self) -> None:
        self.buffer += bytes(self.socket.readAll())
        if b"\n" not in self.buffer:
            return
        try:
            reply = json.loads(self.buffer.split(b"\n", 1)[0])
            fault = reply["status"]["fault"]
        except (ValueError, KeyError, TypeError):
            self._done("offline", f"{self.address} answered, but not like the controller")
            return
        if fault:
            self._done("fault", f"controller at {self.address}: fault: {fault} (make sim-reset clears it)")
        else:
            self._done("connected", f"controller at {self.address}")

    def _timed_out(self) -> None:
        self._done("offline", f"no answer from {self.address}")

    def _done(self, state: str, detail: str) -> None:
        if self.socket is None:
            return                                     # already reported (an error after the answer, say)
        self._drop()
        self.result.emit(state, detail)

    def _drop(self) -> None:
        self.timer.stop()
        socket, self.socket = self.socket, None
        for signal in (socket.connected, socket.readyRead, socket.errorOccurred):
            signal.disconnect()                        # an aborted socket must not report back
        socket.abort()
        socket.deleteLater()


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
        self.setWindowTitle(APP_NAME)
        self.resize(760, 900)
        self.result = self.report = self.planner = self.runner = None
        self.conversation: list = []                    # the turns so far, to continue with an answer
        self.activity: list = []                        # what the conversation view shows, in order: (kind, text)
        self.rows: dict[str, int] = {}

        # The window walks through three steps, one page at a time, with a step bar on top:
        #   1 Describe: the request, the examples, the conversation with Claude, Start planning
        #   2 Plan:     the plan to review; back to Describe if it is not right, on to Run if it is
        #   3 Run:      run it on the cell, watch every vial, stop it
        # A plan marked ready opens the Plan step by itself; the bar and the buttons go back and forth.

        # step 1: describe
        self.request = QPlainTextEdit(placeholderText="Tell the workcell in your own words, e.g. Scan and weigh row "
                                                      "C of bay 3, then put the vials in the same wells of bay 8.")
        self.request.setMaximumHeight(130)
        self.load_button = QPushButton("pick from this list")
        self.load_button.setMenu(self._examples_menu())
        self.plan_button = QPushButton("Start planning", clicked=self.plan)
        self.plan_button.setDefault(True)
        # where the controller is and the demo deck: behind the gear in the header
        self.settings = SettingsDialog(self)
        self.demo_deck = self.settings.demo_deck
        # "Describe what you want to do, or [pick from this list]" above the request; Start planning below it
        ask = QLabel("<b>Describe what you want to do</b>, or")
        ask.setStyleSheet("font-size: 13pt")
        prompt_row = QHBoxLayout()
        prompt_row.addWidget(ask)
        prompt_row.addWidget(self.load_button)
        prompt_row.addStretch()
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.plan_button)
        self.reply = QTextBrowser()                     # the conversation with Claude
        self.reply.setPlaceholderText("Claude's replies appear here. When Claude has a question, it asks it in a "
                                      "popup; a plan ready for review opens the Plan step.")
        # The conversation shows everything that happens while Claude plans, in order: the request, each tool
        # call as Claude makes it, Claude's reply, an answer, and so on. Planning is the only time Claude is
        # involved, so this is its whole activity; the Run step's feed is Orca's alone.
        describe_page = self._page(self._box("", prompt_row, self.request, row),
                                   self._box("Conversation with Claude", self.reply))
        describe_page.layout().setStretch(1, 1)         # extra height goes to the conversation

        # step 2: plan
        # in plain words first (Claude's own description of the plan), then the plan details as a tree
        self.plan_text = QLabel(wordWrap=True, textFormat=Qt.MarkdownText)
        self.plan_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.plan_text.setStyleSheet("background: palette(base); border: 1px solid palette(mid); border-radius: 6px; "
                                     "padding: 8px; font-size: 13pt")
        self.tree = QTreeWidget(headerHidden=True)
        self.findings = QLabel(wordWrap=True)
        self.to_run = QPushButton("Go to run  ▶", enabled=False, clicked=lambda: self.go_to(RUN))
        self.to_run.setDefault(True)
        plan_buttons = QHBoxLayout()                    # top left, like Run on the cell; back: the step bar
        plan_buttons.addWidget(self.to_run)
        plan_buttons.addStretch()
        plan_page = self._page(self._box("The plan", plan_buttons, self._caption("What will happen"), self.plan_text,
                                         self._caption("Plan details"), self.tree, self.findings))

        # step 3: run
        self.run_button = QPushButton("Run on the cell", enabled=False, clicked=self.run)
        self.stop_button = QPushButton("Stop", enabled=False, clicked=self.stop)
        # the run clock: elapsed time, vials at their destination, throughput (counted from Orca's start)
        self.clock = QLabel("")
        self.clock.setFont(QFont("Menlo", 13))
        self.run_started = self.run_finished = None
        buttons = QHBoxLayout()
        for w in (self.run_button, self.stop_button):
            buttons.addWidget(w)
        buttons.addStretch()
        buttons.addWidget(self.clock)
        # the state and Orca's phase live in the status bar; the state ignores its text width (a long message is
        # cut at the window's edge), so it never makes the window wider
        self.state = QLabel("idle")
        bold = QFont()
        bold.setBold(True)
        self.state.setFont(bold)
        self.state.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.phase = QLabel("Orca: -")
        self.statusBar().addWidget(self.state, 1)
        self.statusBar().addPermanentWidget(self.phase)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.feed = QPlainTextEdit(readOnly=True, maximumBlockCount=3000)
        self.feed.setFont(QFont("Menlo", 11))
        run_page = self._page(self._box("Run on the cell", buttons, self.table, self.feed))

        self.pages = QStackedWidget()
        for page in (describe_page, plan_page, run_page):
            self.pages.addWidget(page)
        self.step_bar = StepBar(STEP_NAMES, self.go_to)

        # on top: the step bar on the left; top right: whether the controller answers, and the gear for its settings
        self.connection = QLabel()
        self.gear = ClickableLabel("⚙", toolTip="Controller and demo deck")   # just the symbol, no button frame
        self.gear.clicked.connect(self.open_settings)
        self.gear.setStyleSheet("font-size: 20pt")
        top = QHBoxLayout()
        top.addWidget(self.step_bar)
        top.addStretch()
        self.connection.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        top.addWidget(self.connection, 0, Qt.AlignVCenter)
        top.addSpacing(8)
        top.addWidget(self.gear, 0, Qt.AlignVCenter)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addLayout(top)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(page)
        self.update_steps()

        self.timer = QTimer(self, interval=500, timeout=self.refresh)
        self.log_feed = Feed()
        self.log_feed.line.connect(self.say)
        logging.getLogger("orca").addHandler(self.log_feed)

        self.connection_state = None                    # connected / fault / offline / demo, once checked
        self.checker = ConnectionCheck(self)
        self.checker.result.connect(self.show_connection)
        self.check_timer = QTimer(self, interval=3000, timeout=self.check_connection)
        self.check_timer.start()
        self.check_connection()

    # ---------------------------------------------------------------- helpers
    def _box(self, title: str, *parts) -> QGroupBox:
        box, layout = QGroupBox(title), QVBoxLayout()
        for part in parts:
            (layout.addLayout if isinstance(part, QHBoxLayout) else layout.addWidget)(part)
        box.setLayout(layout)
        return box

    def show_centered(self, dialog: QDialog) -> int:
        """Show a dialog or message box modal, centred on the window. The platform places a dialog when it
        appears (macOS: the middle of the screen), so it is centred again just after it is shown."""
        def centre() -> None:
            frame = dialog.frameGeometry()
            frame.moveCenter(self.frameGeometry().center())
            dialog.move(frame.topLeft())
        dialog.adjustSize()
        centre()
        QTimer.singleShot(0, centre)                    # runs inside exec(), once the dialog is on screen
        return dialog.exec()

    def _caption(self, text: str) -> QLabel:
        caption = QLabel(text)
        caption.setStyleSheet("color: gray; font-weight: 600")
        return caption

    def _page(self, *parts) -> QWidget:
        page, layout = QWidget(), QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        for part in parts:
            (layout.addLayout if isinstance(part, QHBoxLayout) else layout.addWidget)(part)
        page.setLayout(layout)
        return page

    # ---------------------------------------------------------------- the steps
    def reachable_steps(self) -> set:
        """Describe always; Plan once there is a plan to look at; Run once it is ready to run on the cell (or
        while a run is going, so it can be watched)."""
        steps = {DESCRIBE}
        if self.result is not None and self.result.spec is not None:
            steps.add(PLAN)
        if self.result is not None and self.result.ready and not self.demo_deck.isChecked():
            steps.add(RUN)
        if self.runner is not None and (self.runner.isRunning() or self.rows):
            steps.add(RUN)
        return steps

    def go_to(self, step: int) -> None:
        if step not in self.reachable_steps():
            return
        self.pages.setCurrentIndex(step)
        self.update_steps()
        if step == RUN and self.state.text().startswith("planned"):
            self.set_state("ready: Run on the cell asks for approval, checks the deck, then runs")

    def update_steps(self) -> None:
        """The step bar and the buttons that move between steps, after anything that changes what can be shown."""
        reachable = self.reachable_steps()
        if self.pages.currentIndex() not in reachable:
            self.pages.setCurrentIndex(DESCRIBE)
        self.step_bar.show_steps(self.pages.currentIndex(), reachable)
        self.to_run.setEnabled(RUN in reachable)
        self.to_run.setToolTip("" if RUN in reachable else
                               "Planned against the demo deck: open ⚙, connect a controller and plan again to run"
                               if self.demo_deck.isChecked() else "")
        running = self.runner is not None and self.runner.isRunning()
        self.run_button.setEnabled(RUN in reachable and self.result is not None and self.result.ready and not running)

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
        host, port, _ = self.settings.values()
        return host, port

    # ---------------------------------------------------------------- the controller: settings and connection
    PILL = {"connected": ("● Connected", "#2e7d32"), "fault": ("● Controller fault", "#ef6c00"),
            "offline": ("● Not connected", "#c62828"), "demo": ("● Demo deck", "#757575")}

    def open_settings(self) -> None:
        before = self.settings.values()
        if self.show_centered(self.settings) != QDialog.Accepted:
            self.settings.set_values(*before)          # Cancel: nothing changes
            return
        if self.settings.values() != before:
            self.check_connection()

    def check_connection(self) -> None:
        if self.demo_deck.isChecked():
            self.show_connection("demo", "planning against the demo deck; the controller is not used")
        else:
            self.checker.check(*self._address())

    def show_connection(self, state: str, detail: str) -> None:
        if state != "demo" and self.demo_deck.isChecked():
            return                                     # an answer that arrived after switching to the demo deck
        text, colour = self.PILL[state]
        self.connection.setText(text)
        self.connection.setToolTip(detail)
        self.connection.setStyleSheet(f"color: white; background: {colour}; border-radius: 9px; "
                                      f"padding: 2px 10px; font-weight: 600")
        self.connection_state = state

    def _inventory(self):
        return DemoInventory() if self.demo_deck.isChecked() else ControllerInventory(*self._address())

    def load_request(self, text: str) -> None:
        """A new request (an example loaded): forget the previous conversation and plan, and clear their views,
        back on the Describe step. A run still going keeps its Run step."""
        self.request.setPlainText(text)
        self.conversation, self.result, self.report = [], None, None
        self.reply.clear()
        self.activity = []
        self.tree.clear()
        self.plan_text.clear()
        self.findings.clear()
        if self.runner is None or not self.runner.isRunning():
            self.table.setRowCount(0)
            self.rows = {}
            self.feed.clear()
            self.phase.setText("Orca: -")
            self.set_state("idle")
        self.pages.setCurrentIndex(DESCRIBE)
        self.update_steps()

    def say(self, text: str) -> None:
        self.feed.appendPlainText(text)

    def set_state(self, text: str) -> None:
        self.state.setText(text)
        self.state.setToolTip(text)                     # the whole message, when the status bar cuts it

    # ---------------------------------------------------------------- plan
    def plan(self) -> None:
        """Plan the request box's text as a new conversation."""
        request = self.request.toPlainText().strip()
        if request:
            self.conversation, self.activity = [], []   # a new plan (an answer continues the same one)
            self._ask(request)

    def _ask(self, text: str) -> None:
        self.result = self.report = None
        self.activity.append(("you", text))
        self._show_conversation(planning=True)
        self.tree.clear()
        self.plan_text.clear()
        self.findings.setText("<i>Claude is planning ...</i>")
        self.plan_button.setEnabled(False)
        self.set_state("planning")
        self.pages.setCurrentIndex(DESCRIBE)            # the conversation is where planning shows
        self.update_steps()
        self.planner = PlanWorker(text, self._inventory(), history=self.conversation)
        self.planner.tool_call.connect(self.tool_called)
        self.planner.planned.connect(self.planned)
        self.planner.failed.connect(self.planning_failed)
        self.planner.start()

    def tool_called(self, call: str) -> None:
        self.activity.append(("tool", call))
        self._show_conversation(planning=True)

    def planning_failed(self, message: str) -> None:
        self.activity.append(("failed", message))
        self._show_conversation()
        self.findings.setText("Planning failed: see the conversation.")
        self.plan_button.setEnabled(True)
        self.set_state("idle")
        self.update_steps()

    def planned(self, result) -> None:
        self.result, self.report, self.conversation = result, result.report, result.conversation
        self.activity.append(("claude", result.reply))
        self._show_conversation()
        self.plan_button.setEnabled(True)
        self.findings.clear()
        if result.spec is not None:                     # shown even when not ready, so the question has context
            self._show_spec(result.spec)
            self.plan_text.setText(result.reply or result.spec.summary)   # Claude's words; the summary if none
            self._show_findings(result.spec, result.report)
        self.update_steps()
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
        self.run_button.setText("Run on the cell")
        self.set_state("planned: review the plan, then go to run" if not self.demo_deck.isChecked()
                       else "planned (demo deck): review only")
        self.go_to(PLAN)                                # a plan ready: review it on its own step

    def ask_person(self, question: str) -> str | None:
        """Claude's question in a popup with an answer box. Returns the answer, or None for Abort (or no answer)."""
        dialog = QuestionDialog(question, self)
        return dialog.answer() if self.show_centered(dialog) == QDialog.Accepted else None

    def _show_conversation(self, planning: bool = False) -> None:
        """Everything so far, in order, newest last: your words, Claude's tool calls, its replies. `planning`:
        Claude is still working on the last request."""
        parts = []
        for kind, text in self.activity:
            if kind == "tool":
                parts.append(f"*Claude calls* `{text}`")
                continue
            if parts:
                parts.append("---")                     # a line before each new turn, not between tool calls
            parts.append({"you": "**You:** ", "claude": "**Claude:** ", "failed": "**Planning failed:** "}[kind] + text)
        if planning:
            parts.append("*Claude is planning ...*")
        self.reply.setMarkdown("\n\n".join(parts))
        self.reply.verticalScrollBar().setValue(self.reply.verticalScrollBar().maximum())

    def _show_spec(self, spec: WorkflowSpec) -> None:
        root = QTreeWidgetItem([f"{spec.name}: {spec.summary}"])
        for p in spec.processes:
            where = "routes below, otherwise " if p.routes else ""
            item = QTreeWidgetItem([f"{p.id}: bay {p.source.bay} {p.source.describe()} -> {where}{p.destination.describe()}"])
            for step in p.steps:
                values = ", ".join(f"{k} {v}" for k, v in step.model_dump(exclude={"op", "rules"}).items())
                s = QTreeWidgetItem([f"{step.op}" + (f" ({values})" if values else "")])
                for rule in step.rules:
                    then = rule.then if rule.then == "skip" else rule.then.model_dump(exclude_none=True)
                    s.addChild(QTreeWidgetItem([f"if {rule.when.describe()}: {then}"]))
                item.addChild(s)
            for route in p.routes:
                item.addChild(QTreeWidgetItem([f"route: if {route.when.describe()}: {route.then.describe()}"]))
            root.addChild(item)
        if spec.transit:
            root.addChild(QTreeWidgetItem(["transit wells (one vial of each swap waits there): "
                                           + ", ".join(t.describe() for t in spec.transit)]))
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
        moves += len(report.routes)                    # a vial going via a transit well moves once more
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
            self.show_centered(QMessageBox(QMessageBox.Warning, "Not run",
                                           "The deck no longer fits the plan:\n\n" + report.text(), parent=self))
            return
        vials = sum(len(v) for v in report.vials.values())
        approve = QMessageBox(QMessageBox.Question, "Approve the plan",
                              f"Run '{spec.name}' on the cell at {host}:{port}?\n\n{spec.summary}\n\n"
                              f"{vials} vials will be moved.", QMessageBox.Yes | QMessageBox.No, self)
        if self.show_centered(approve) != QMessageBox.Yes:
            return
        self._fill_table(spec, report)
        self.feed.clear()
        self.say(f"approved: {spec.name} ({vials} vials)")
        self.run_button.setEnabled(False)
        self.plan_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.runner = RunWorker(spec, report, host, port)
        self.run_started = self.run_finished = None
        self.clock.setText("")
        self.runner.state.connect(self._run_state)
        self.runner.ended.connect(self.run_ended)
        self.runner.start()
        self.timer.start()
        self.update_steps()

    def _run_state(self, state: str) -> None:
        if state == "running":
            self.run_started = time.monotonic()        # the clock starts when Orca starts, not while it builds
        if state == "finished" and self.run_started and not self.run_finished:
            self.run_finished = time.monotonic()
        self.set_state({"building": "building the Orca system (arm routes)", "running": "running"}.get(state, state))

    def _show_clock(self) -> None:
        if not self.run_started:
            return
        elapsed = (self.run_finished or time.monotonic()) - self.run_started
        done = self.runner.vials_finished()
        rate = done / (elapsed / 60) if elapsed > 1 else 0.0
        self.clock.setText(f"{int(elapsed // 60)}:{int(elapsed % 60):02d}   {done}/{len(self.rows)} vials   "
                           f"{rate:.1f} vials/min")

    def _fill_table(self, spec: WorkflowSpec, report: Report) -> None:
        from .compiler import vial_name
        self.table.setRowCount(0)
        self.rows = {}
        for p in spec.processes:
            for well, target in report.moves[p.id]:
                name = vial_name(p.source.bay, well)
                via = report.routes.get((p.source.bay, well))
                path = (f"bay {p.source.bay} {well}" + (f" -> (bay {via[0]} {via[1]})" if via else "")
                        + (" -> (a route decides)" if p.routes else f" -> bay {p.destination.bay} {target}"))
                self.rows[name] = self.table.rowCount()
                self.table.insertRow(self.table.rowCount())
                for col, text in enumerate([path, p.id, "waiting"]):
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
            if record.get("to", "").startswith("bay") and "route decides" in self.table.item(row, 0).text():
                start = self.table.item(row, 0).text().split(" -> ")[0]
                self.table.setItem(row, 0, QTableWidgetItem(f"{start} -> {record['to']}"))   # decided mid-flight
            values = [record["status"], record.get("code") or "", _num(record.get("grams"), 3),
                      _num(record.get("celsius"), 1), "; ".join(record.get("rules", []))]
            for col, text in enumerate(values, start=2):
                self.table.setItem(row, col, QTableWidgetItem(str(text)))
        if self.state.text().startswith("running"):
            self.set_state(f"running: {done} of {len(self.rows)} vials through their steps")
        self._show_clock()

    def stop(self) -> None:
        if self.runner:
            self.stop_button.setEnabled(False)
            self.set_state("stopping")
            self.say("stop requested: pausing, then aborting the Orca run")
            self.runner.stop()

    def run_ended(self, outcome: str, detail: str) -> None:
        self.timer.stop()
        if self.run_started and not self.run_finished:
            self.run_finished = time.monotonic()
        self.refresh()
        self.phase.setText(f"Orca: {self.runner.orca_phase()}")
        self.set_state({"completed": "completed", "stopped": "stopped: the heater is off; vials in an instrument "
                        "or the gripper stay there", "failed": f"failed: {detail}"}[outcome])
        self.say(f"run {outcome}" + (f": {detail}" if detail else ""))
        self.stop_button.setEnabled(False)
        self.plan_button.setEnabled(True)
        self.update_steps()
        # The plan can run again (a swap undoes itself the second time): Run checks the deck again first and
        # refuses with the reason if the plan no longer fits, e.g. the source wells are now empty.
        self.run_button.setEnabled(self.result is not None and self.result.ready)
        self.run_button.setText("Run again" if outcome == "completed" else "Run on the cell")


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
