"""The agent's window: plan with Claude in a chat, run the plan on the cell, watch it, stop it.

    .venv-orca/bin/python -m agent.ui

Two steps, one page each, with a step bar on top:

  1 Describe  a chat with Claude: say what should happen (or pick one of examples/agent_requests/) and Send.
              Claude plans against the deck (the live controller, or the demo deck); its replies, questions and
              tool calls show in the conversation, and an answer is just the next message. A plan ready to run
              shows in green with the job size and the deck check, Details (the plan's structure, in a popup)
              and Run this
  2 Run       asks for approval, checks the deck once more, then compiles the plan and runs it through Orca and
              the controller; shown: Orca's phase for the run, every vial's status, and a feed of what Orca
              does. Stop is Orca's operator stop (pause, then a confirmed abort); the heater is switched off
  ⚙         the controller's host and port, and the demo deck; the pill next to it shows whether the
            controller answers (checked every few seconds)

Planning and running each happen in a QThread of their own, so the window stays responsive: the planner waits
on the API, and the run needs its own asyncio event loop for Orca. They report back through Qt signals.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import logging
import os
import sys
import time

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtNetwork import QTcpSocket
from PySide6.QtGui import QAction, QColor, QFont, QPalette, QTextDocument
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
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
DESCRIBE, RUN = range(2)                           # the steps, in order
STEP_NAMES = ["Describe", "Run"]
FIRST_MESSAGE_HINT = ("Message Claude, e.g. Scan and weigh row C of bay 3, then put the vials in the same wells of "
                      "bay 8.  (Shift+Enter: new line)")


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
        from .scheduler.core import ControllerConfig

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
        self.plans: list = []                           # each confirmed plan of the conversation, in order
        self.abandoned: list = []                       # planners of conversations left while Claude was working
        self.rows: dict[str, int] = {}

        # The window walks through two steps, one page at a time, with a step bar on top:
        #   1 Describe: a chat with Claude: the conversation, the message box, Send, the examples. A plan marked
        #               ready shows in the chat in green, with Details (a popup) and Run this
        #   2 Run:      run it on the cell, watch every vial, stop it

        # step 1: describe, as a chat: the conversation above, the message box and Send below. The conversation
        # shows everything that happens while Claude plans, in order: your messages, each tool call as Claude makes
        # it, Claude's replies. Planning is the only time Claude is involved; the Run step's feed is Orca's alone.
        self.reply = QTextBrowser(openLinks=False)
        self.reply.anchorClicked.connect(self._link)    # "Details" and "Run this" under a confirmed plan
        self.reply.setPlaceholderText("Describe what you want to do, or pick an example. Claude plans with you here; "
                                      "a plan ready to run shows here with Run this.")
        self.message = QPlainTextEdit(placeholderText=FIRST_MESSAGE_HINT)   # cleared once the conversation starts
        self.message.setFixedHeight(self.message.fontMetrics().lineSpacing() * 3 + 16)
        self.message.installEventFilter(self)           # Enter sends
        self.send_button = QPushButton("Send", clicked=self.send)
        self.send_button.setDefault(True)
        self.load_button = QPushButton("pick from this list")
        self.load_button.setMenu(self._examples_menu())
        self.new_button = QPushButton("New conversation", flat=True, clicked=lambda: self.new_conversation())
        # one row under the message box: the examples and New conversation on the left, Send on the right
        buttons_row = QHBoxLayout()
        buttons_row.addWidget(QLabel("Type in what you want, or"))
        for button in (self.load_button, self.new_button):
            buttons_row.addWidget(button)
        buttons_row.addStretch()
        buttons_row.addWidget(self.send_button)
        # where the controller is and the demo deck: behind the gear in the header
        self.settings = SettingsDialog(self)
        self.demo_deck = self.settings.demo_deck
        describe_page = self._page(self.reply, self.message, buttons_row)
        describe_page.layout().setStretch(0, 1)         # extra height goes to the conversation

        # step 2: run
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
        run_page = self._page(buttons, self.table, self.feed)     # no frame, like Describe
        run_page.layout().setStretch(1, 1)

        self.pages = QStackedWidget()
        for page in (describe_page, run_page):
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

    def _page(self, *parts) -> QWidget:
        page, layout = QWidget(), QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        for part in parts:
            (layout.addLayout if isinstance(part, QHBoxLayout) else layout.addWidget)(part)
        page.setLayout(layout)
        return page

    # ---------------------------------------------------------------- the steps
    def reachable_steps(self) -> set:
        """Describe always; Run once a plan is ready to run on the cell (or while a run is going, so it can be
        watched)."""
        steps = {DESCRIBE}
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
        running = self.runner is not None and self.runner.isRunning()
        self.run_button.setEnabled(RUN in reachable and self.result is not None and self.result.ready and not running)

    def _examples_menu(self) -> QMenu:
        menu = QMenu(self)
        for example in all_examples():
            action = QAction(f"{example.name}   ({'plan' if example.expect_plan else 'no plan'} expected)", menu)
            action.setToolTip(example.notes)
            action.triggered.connect(lambda _=False, e=example: self.send_example(e.request))
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

    def send_example(self, request: str) -> None:
        """An example picked from the list: sent at once, as a new conversation."""
        self.new_conversation(request)
        self.send()

    def new_conversation(self, text: str = "") -> None:
        """Start again (New conversation, or an example picked): forget the conversation and the plan, and clear
        their views, back on the Describe step. Claude may still be working on the old conversation: its answer
        is dropped when it comes (the API call cannot be taken back). A run still going keeps its Run step."""
        if self.planning():
            self.abandoned.append(self.planner)         # kept until it ends: Qt must not delete a running thread
        self.abandoned = [p for p in self.abandoned if p.isRunning()]
        self.planner = None
        self.message.setPlainText(text)
        self.message.setFocus()
        self._chat_buttons(not self.planning())
        self.conversation, self.result, self.report = [], None, None
        self.reply.clear()
        self.activity, self.plans = [], []
        self.message.setPlaceholderText(FIRST_MESSAGE_HINT)
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
    def eventFilter(self, obj, event) -> bool:
        """Enter in the message box sends it; Shift+Enter starts a new line."""
        if (obj is self.message and event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and not event.modifiers() & Qt.ShiftModifier):
            self.send()
            return True
        return super().eventFilter(obj, event)

    def send(self) -> None:
        """Send the message box's text: it continues the conversation, whether Claude asked something or a plan
        is ready already (then it refines that plan)."""
        text = self.message.toPlainText().strip()
        if not text or self.planning():
            return
        self.message.clear()
        self._ask(text)

    def planning(self) -> bool:
        return self.planner is not None and self.planner.isRunning()

    def _chat_buttons(self, enabled: bool) -> None:
        """Send waits while Claude is planning; an example or New conversation starts again at any time."""
        self.send_button.setEnabled(enabled)

    def _from_abandoned(self) -> bool:
        """A signal from the planner of a conversation that was left: ignored."""
        return self.sender() is not None and self.sender() in self.abandoned

    def _ask(self, text: str) -> None:
        self.result = self.report = None
        self.activity.append(("you", text))
        self._show_conversation(planning=True)
        self._chat_buttons(False)
        self.set_state("planning")
        self.pages.setCurrentIndex(DESCRIBE)            # the conversation is where planning shows
        self.update_steps()
        self.planner = PlanWorker(text, self._inventory(), history=self.conversation)
        self.planner.tool_call.connect(self.tool_called)
        self.planner.planned.connect(self.planned)
        self.planner.failed.connect(self.planning_failed)
        self.planner.start()

    def tool_called(self, call: str) -> None:
        if self._from_abandoned():
            return
        self.activity.append(("tool", call))
        self._show_conversation(planning=True)

    def planning_failed(self, message: str) -> None:
        if self._from_abandoned():
            return
        self.activity.append(("failed", message))
        self._show_conversation()
        self._chat_buttons(True)
        self.set_state("idle")
        self.update_steps()

    def planned(self, result) -> None:
        if self._from_abandoned():
            return
        self.result, self.report, self.conversation = result, result.report, result.conversation
        self.activity.append(("plan" if result.ready else "claude", result.reply))   # "plan": a confirmed plan
        if result.ready:
            self.plans.append(result)
        self._show_conversation()
        self._chat_buttons(True)
        self.update_steps()
        if not result.ready:
            # Not ready to run: Claude asked something, explained what is in the way, or wants a choice confirmed
            # before it marks its plan ready. The reply is in the conversation; the next message answers it.
            self.set_state("Claude is waiting for your answer")
            self.message.setFocus()
            return
        self.run_button.setText("Run on the cell")
        # The plan is in the conversation, in green: Claude's words, the job size and the deck check, with
        # Details (its structure, in a popup) and Run this. The window stays on the chat.
        self.set_state("planned: Run this to run it" if not self.demo_deck.isChecked()
                       else "planned against the demo deck: connect a controller (⚙) and plan again to run it")

    def _link(self, url) -> None:
        """The links under a confirmed plan: Details shows its structure in a popup; Run this takes that plan (an
        earlier one too, after refining) to the Run step."""
        if url.scheme() == "details":
            self.show_centered(PlanDetails(self.plans[int(url.path())].spec, self))
            return
        if url.scheme() != "run" or self.planning():
            return
        if self.runner is None or not self.runner.isRunning():   # a run going keeps its plan; just show it
            result = self.plans[int(url.path())]
            if result is not self.result:
                self.result, self.report = result, result.report
                self.update_steps()
        self.go_to(RUN)

    def _show_conversation(self, planning: bool = False) -> None:
        """Everything so far, in order, newest last, as a chat: your messages on the right, Claude's replies on the
        left, its tool calls as small grey lines between them. `planning`: Claude is still working."""
        palette = self.reply.palette()
        base, accent = palette.color(QPalette.Base), palette.color(QPalette.Highlight)
        mine = _mix(base, accent, 0.25).name()                         # a light (or dark) tint of the accent
        theirs = palette.color(QPalette.AlternateBase).name()
        confirmed = _mix(base, QColor("#2e7d32"), 0.25).name()          # a plan marked ready: the pill's green, light
        blocks, plans = [], 0
        runnable = not self.demo_deck.isChecked()       # a demo-deck plan can be reviewed, not run
        for kind, text in self.activity:
            if kind == "you":
                blocks.append(_bubble(html.escape(text).replace("\n", "<br>"), mine, right=True))
            elif kind == "plan":                        # a confirmed plan: green, its size, Details and Run this
                result = self.plans[plans]
                run = (f'&nbsp;&nbsp; <a href="run:{plans}" style="color: white; background-color: #2e7d32; '
                       f'text-decoration: none; font-weight: 600">&nbsp; Run this ▶ &nbsp;</a>') if runnable else ""
                links = f'<p align="right"><a href="details:{plans}">Details</a>{run}</p>'
                job = f'<p style="font-size: small">{_job_html(result.spec, result.report)}</p>'
                blocks.append(_bubble(_markdown_html(text) + job + links, confirmed, right=False))
                plans += 1
            elif kind == "claude":
                blocks.append(_bubble(_markdown_html(text), theirs, right=False))
            elif kind == "tool":
                blocks.append(f'<p style="color: gray; font-size: small; margin: 2px 10px">Claude calls '
                              f'<code>{html.escape(text)}</code></p>')
            else:
                blocks.append(f'<p style="color: #c62828; margin: 6px 10px"><b>Planning failed:</b> '
                              f'{html.escape(text)}</p>')
        if planning:
            blocks.append('<p style="color: gray; margin: 6px 10px"><i>Claude is planning ...</i></p>')
        self.reply.setHtml("".join(blocks))
        self.message.setPlaceholderText("" if self.activity else FIRST_MESSAGE_HINT)   # the hint only before it starts
        self.reply.verticalScrollBar().setValue(self.reply.verticalScrollBar().maximum())

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
        self.send_button.setEnabled(False)            # the plan stays as it is while it runs
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
        self.send_button.setEnabled(True)
        self.update_steps()
        # The plan can run again (a swap undoes itself the second time): Run checks the deck again first and
        # refuses with the reason if the plan no longer fits, e.g. the source wells are now empty.
        self.run_button.setEnabled(self.result is not None and self.result.ready)
        self.run_button.setText("Run again" if outcome == "completed" else "Run on the cell")


class PlanDetails(QDialog):
    """A plan's structure, for whoever wants to see it: processes, steps, rules, routes and transit wells."""

    def __init__(self, spec: WorkflowSpec, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Details: {spec.name}")
        self.resize(560, 420)
        self.tree = QTreeWidget(headerHidden=True)
        self.tree.addTopLevelItem(_spec_tree(spec))
        self.tree.expandAll()
        buttons = QDialogButtonBox(QDialogButtonBox.Close, rejected=self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tree)
        layout.addWidget(buttons)


def _spec_tree(spec: WorkflowSpec) -> QTreeWidgetItem:
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
    return root


def _job_html(spec: WorkflowSpec, report: Report) -> str:
    """The size of the job and the deck check, for the confirmed plan's reply."""
    lines, moves, heater = [], 0, [0, 0]
    for p in spec.processes:
        n = len(report.vials.get(p.id, []))
        heats = [s for s in p.steps if isinstance(s, Heat)]
        optional = sum(1 for s in heats if any(r.then == "skip" for r in s.rules))
        moves += n * (len(p.steps) + 1)
        heater[0] += n * (len(heats) - optional)
        heater[1] += n * len(heats)
        lines.append(f"{html.escape(p.id)}: {n} vials, " + (f"{len(p.steps)} step(s)" if p.steps else "rack-to-rack move"))
    moves += len(report.routes)                        # a vial going via a transit well moves once more
    size = f"Job size: {sum(len(v) for v in report.vials.values())} vials, up to {moves} arm moves"
    if heater[1]:
        size += (f", {heater[0]}" + (f" to {heater[1]}" if heater[1] != heater[0] else "") + " heater visits")
    checks = "<br>".join(f"⚠ {html.escape(w)}" for w in report.warnings) or "Deck check: no problems found."
    return "<br>".join(lines) + f"<br><b>{size}</b><br>{checks}"


def _mix(a: QColor, b: QColor, share: float) -> QColor:
    return QColor(*(round(x + (y - x) * share) for x, y in zip(a.getRgb()[:3], b.getRgb()[:3])))


def _markdown_html(text: str) -> str:
    """Claude's Markdown as an HTML fragment (Qt's own Markdown reader), to sit inside a chat bubble."""
    doc = QTextDocument()
    doc.setMarkdown(text)
    body = re.search(r"<body[^>]*>(.*)</body>", doc.toHtml(), re.S)
    return body.group(1) if body else html.escape(text)


def _bubble(inner: str, colour: str, right: bool) -> str:
    """One chat message: a full-width row with the message in a coloured cell, pushed to the right (yours) or
    left (Claude's) by an empty cell."""
    cell = f'<td bgcolor="{colour}" style="padding: 8px">{inner}</td>'
    gap = '<td width="18%"></td>'
    return (f'<table width="100%" cellspacing="6" cellpadding="8"><tr>{gap + cell if right else cell + gap}'
            f'</tr></table>')


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
