"""The agent's window, headless (Qt's offscreen platform): it builds, loads examples, shows a plan, filters
Orca's log into the feed. Planning and running are tested elsewhere; no API, no cell here."""

import logging
import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("anthropic")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace  # noqa: E402

from PySide6.QtWidgets import QApplication, QLabel, QTextBrowser  # noqa: E402

from agent.inventory import DemoInventory  # noqa: E402
from agent.spec import WorkflowSpec  # noqa: E402
from agent.ui import Window  # noqa: E402
from agent.validator import validate  # noqa: E402


@pytest.fixture(scope="module")
def window():
    app = QApplication.instance() or QApplication([])
    w = Window()
    yield w, app
    logging.getLogger("orca").removeHandler(w.log_feed)


@pytest.fixture(autouse=True)
def no_api(monkeypatch):
    """Picking an example sends it: every test gets a planner that never calls the API (a test may put its own)."""
    from agent import ui

    class Silent(ui.PlanWorker):
        def run(self):
            pass
    monkeypatch.setattr(ui, "PlanWorker", Silent)


def _settle(w, app):
    """Let the planner (a stand-in) finish and its signals arrive."""
    if w.planner is not None:
        w.planner.wait(2000)
    app.processEvents()


def test_picking_an_example_sends_it_as_a_new_conversation(window):
    w, app = window
    actions = w.load_button.menu().actions()
    assert len(actions) >= 10
    w.new_conversation()
    w.activity = [("you", "an older request")]
    actions[0].trigger()
    _settle(w, app)
    assert w.activity[0][0] == "you" and w.activity[0][1].startswith("Heat the first four vials of column 6")
    assert len(w.activity) == 1 and w.message.toPlainText() == ""                # sent, the old one gone


def test_an_example_picked_while_claude_plans_drops_the_old_conversation(window, monkeypatch):
    import threading

    from agent import ui
    w, app = window
    w.new_conversation()
    release = threading.Event()

    class Slow(ui.PlanWorker):                               # answers only when released
        def run(self):
            release.wait(2)
            self.planned.emit(_plan_result(ready=True))
    monkeypatch.setattr(ui, "PlanWorker", Slow)
    w.message.setPlainText("the old request")
    w.send()
    old = w.planner
    assert w.load_button.isEnabled() and w.new_button.isEnabled() and not w.send_button.isEnabled()

    class Quiet(ui.PlanWorker):
        def run(self):
            pass
    monkeypatch.setattr(ui, "PlanWorker", Quiet)
    w.load_button.menu().actions()[0].trigger()             # a new conversation while Claude still works
    assert old in w.abandoned and w.planner is not old
    release.set()
    old.wait(2000)
    app.processEvents()
    assert w.plans == [] and "the old request" not in w.reply.toPlainText()  # the old answer is dropped
    assert w.reply.toPlainText().strip().startswith("Heat the first four vials")


def test_a_confirmed_plan_shows_its_size_and_deck_check_and_its_structure_in_details(window, monkeypatch):
    from PySide6.QtCore import QUrl

    from agent.ui import PlanDetails
    w, _ = window
    spec = WorkflowSpec.model_validate({"name": "t", "summary": "s", "processes": [
        {"id": "p", "source": {"bay": 3, "rows": "C"}, "destination": {"bay": 8}, "steps": [
            {"op": "scan"}, {"op": "weigh"},
            {"op": "heat", "celsius": 40, "hold_s": 5, "rules": [{"when": {"value": "grams", "lt": 2}, "then": "skip"}]}]}]})
    report = validate(spec, DemoInventory())
    w.new_conversation()
    w.planned(SimpleNamespace(spec=spec, ready=True, report=report, reply="Planned.", conversation=[]))
    text = w.reply.toPlainText()
    assert "12 vials, up to 48 arm moves, 0 to 12 heater visits" in text and "Deck check: no problems found." in text
    shown = []
    monkeypatch.setattr(w, "show_centered", lambda dialog: shown.append(dialog) or 0)
    w.reply.anchorClicked.emit(QUrl("details:0"))                        # Details: the structure, in a popup
    assert isinstance(shown[0], PlanDetails)
    assert shown[0].tree.topLevelItem(0).child(0).child(2).child(0).text(0) == "if grams < 2.0: skip"
    w._fill_table(spec, report)
    assert w.table.rowCount() == 12 and w.table.item(0, 0).text() == "bay 3 C1 -> bay 8 C1"


def test_the_feed_shows_moves_and_warnings_but_not_orca_noise(window):
    w, app = window
    w.feed.clear()
    logging.getLogger("orca").setLevel(logging.INFO)
    log = logging.getLogger("orca.test")
    log.info("arm pick vial_x from Location: bay3_C1: picked")
    log.info("Reservation abc granted for bay3_C1")
    log.warning("heater slow")
    app.processEvents()
    assert w.feed.toPlainText().splitlines() == ["I arm pick vial_x from Location: bay3_C1: picked", "W heater slow"]


QUESTION = SimpleNamespace(spec=None, ready=False, report=None, reply="Which temperature?", conversation=[
    {"role": "user", "content": "Heat row E"}, {"role": "assistant", "content": "Which temperature?"}])


def test_a_question_from_claude_stays_in_the_conversation_and_the_next_message_answers_it(window, monkeypatch):
    from agent import ui
    w, app = window
    w.new_conversation()
    w.planned(QUESTION)
    assert w.pages.currentIndex() == ui.DESCRIBE and w.state.text() == "Claude is waiting for your answer"
    assert "Which temperature?" in w.reply.toPlainText() and w.send_button.isEnabled()
    assert not w.run_button.isEnabled() and 'href="run:' not in w.reply.toHtml()

    histories = []

    class Planner(ui.PlanWorker):                                # records what it was given, then asks again
        def run(self):
            histories.append((self.request, self.history))
            self.planned.emit(QUESTION)
    monkeypatch.setattr(ui, "PlanWorker", Planner)
    w.message.setPlainText("50 C, 10 s")
    w.send()
    w.planner.wait(2000)
    app.processEvents()
    assert histories == [("50 C, 10 s", QUESTION.conversation)]           # the same conversation goes on
    assert w.message.toPlainText() == ""
    assert w.reply.toPlainText().index("Which temperature?") < w.reply.toPlainText().index("50 C, 10 s")


def test_enter_sends_and_shift_enter_starts_a_new_line(window, monkeypatch):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    w, app = window
    sent = []
    monkeypatch.setattr(w, "send", lambda: sent.append(w.message.toPlainText()))
    w.message.setPlainText("first line")
    w.message.moveCursor(w.message.textCursor().MoveOperation.End)
    app.sendEvent(w.message, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.ShiftModifier, "\r"))
    assert sent == [] and w.message.toPlainText().count("\n") == 1           # a new line, nothing sent
    app.sendEvent(w.message, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))
    assert sent == ["first line\n"]


def test_send_waits_while_claude_is_planning(window, monkeypatch):
    import threading

    from agent import ui
    w, app = window
    w.new_conversation()
    go = threading.Event()

    class Planner(ui.PlanWorker):
        def run(self):
            go.wait(2)
            self.planned.emit(QUESTION)
    monkeypatch.setattr(ui, "PlanWorker", Planner)
    w.message.setPlainText("Heat row E")
    w.send()
    try:
        assert not w.send_button.isEnabled() and w.new_button.isEnabled() and w.planning()
        w.message.setPlainText("again")
        w.send()                                                 # ignored while planning
        assert w.message.toPlainText() == "again"
        assert w.reply.toPlainText().strip().endswith("Claude is planning ...")
    finally:
        go.set()
        w.planner.wait(2000)
        app.processEvents()
    assert w.send_button.isEnabled() and not w.planning()


def test_picking_an_example_clears_the_previous_plan_and_run(window):
    w, app = window
    w.reply.setPlainText("old conversation")
    w.feed.appendPlainText("old feed line")
    w.table.setRowCount(3)
    w.run_button.setEnabled(True)
    w.conversation = [{"role": "user", "content": "old"}]
    w.load_button.menu().actions()[1].trigger()
    _settle(w, app)
    text = w.reply.toPlainText()
    assert "Heat the first four vials of column 6" in text and "column 12" in text and "old conversation" not in text
    assert not w.feed.toPlainText() and w.table.rowCount() == 0 and not w.run_button.isEnabled()
    assert w.conversation == []


def _plan_result(ready: bool):
    spec = WorkflowSpec.model_validate({"name": "t", "summary": "s", "processes": [
        {"id": "p", "source": {"bay": 3, "rows": "C"}, "destination": {"bay": 8}, "steps": [{"op": "scan"}]}]})
    return SimpleNamespace(spec=spec, ready=ready, report=validate(spec, DemoInventory()), reply="Planned.",
                           conversation=[{"role": "user", "content": "r"}, {"role": "assistant", "content": "Planned."}])


def test_run_is_offered_only_for_a_plan_marked_ready_and_the_window_stays_on_the_chat(window):
    from agent.ui import DESCRIBE, RUN
    w, _ = window
    w.demo_deck.setChecked(False)
    w.new_conversation()
    w.planned(_plan_result(ready=False))                 # accepted but not ready: Claude wants an answer
    assert not w.run_button.isEnabled() and RUN not in w.reachable_steps()
    w.planned(_plan_result(ready=True))
    assert w.run_button.isEnabled() and w.state.text().startswith("planned") and w.pages.currentIndex() == DESCRIBE


def test_a_message_after_a_ready_plan_refines_it_in_the_same_conversation(window, monkeypatch):
    from agent import ui
    w, app = window
    w.new_conversation()
    w.planned(_plan_result(ready=True))
    w.step_bar.buttons[ui.DESCRIBE].click()
    histories = []

    class Planner(ui.PlanWorker):
        def run(self):
            histories.append(self.history)
    monkeypatch.setattr(ui, "PlanWorker", Planner)
    w.message.setPlainText("make it 50 C")
    w.send()
    assert w.reachable_steps() == {ui.DESCRIBE}                       # the old plan is gone until the new one
    w.planner.wait(2000)
    app.processEvents()
    assert histories == [_plan_result(ready=True).conversation]
    w.new_conversation()                                     # Claude never answered: start again


def test_the_message_hint_shows_only_before_the_conversation_starts(window):
    w, _ = window
    w.new_conversation()
    assert w.message.placeholderText().startswith("Message Claude")
    w.planned(QUESTION)
    assert w.message.placeholderText() == ""
    w.new_conversation()
    assert w.message.placeholderText().startswith("Message Claude")


def test_new_conversation_clears_the_chat_and_the_plan(window):
    w, _ = window
    w.planned(_plan_result(ready=True))
    w.message.setPlainText("half typed")
    w.new_button.click()
    assert w.activity == [] and w.reply.toPlainText() == "" and w.message.toPlainText() == ""
    assert w.result is None and w.plans == [] and w.reachable_steps() == {w.pages.currentIndex()}


def test_the_window_shows_its_name_in_the_title_bar_and_the_steps_on_top(window):
    w, _ = window
    assert w.windowTitle() == "Agentic lab automation"
    top = w.centralWidget().layout().itemAt(0).layout()                       # the first row of the window
    assert top.itemAt(0).widget() is w.step_bar and top.indexOf(w.gear) == top.count() - 1


def test_the_window_walks_through_describe_and_run(window):
    from PySide6.QtCore import QUrl

    from agent.ui import DESCRIBE, RUN, STEP_NAMES
    w, app = window
    assert STEP_NAMES == ["Describe", "Run"]
    w.demo_deck.setChecked(False)
    w.load_button.menu().actions()[0].trigger()
    _settle(w, app)
    assert w.pages.currentIndex() == DESCRIBE and w.reachable_steps() == {DESCRIBE}
    assert not w.step_bar.buttons[RUN].isEnabled()

    w.planned(_plan_result(ready=False))                         # a question: still describing
    assert w.pages.currentIndex() == DESCRIBE and w.reachable_steps() == {DESCRIBE}

    w.planned(_plan_result(ready=True))                          # a plan ready: in the chat, with Run this
    assert w.pages.currentIndex() == DESCRIBE and w.step_bar.buttons[RUN].isEnabled()
    w.reply.anchorClicked.emit(QUrl("run:0"))
    assert w.pages.currentIndex() == RUN and w.run_button.isEnabled()
    w.step_bar.buttons[DESCRIBE].click()                         # back: the step bar
    assert w.pages.currentIndex() == DESCRIBE
    w.step_bar.buttons[RUN].click()
    assert w.pages.currentIndex() == RUN


def test_a_plan_on_the_demo_deck_can_be_seen_but_not_run(window):
    from agent.ui import RUN
    w, _ = window
    w.demo_deck.setChecked(True)
    try:
        w.new_conversation()
        w.planned(_plan_result(ready=True))
        assert RUN not in w.reachable_steps() and 'href="run:' not in w.reply.toHtml()
        assert 'href="details:0"' in w.reply.toHtml() and "demo deck" in w.state.text()
    finally:
        w.demo_deck.setChecked(False)


def test_a_long_status_does_not_make_the_window_wider(window):
    w, app = window
    w.set_state("idle")
    app.processEvents()
    narrow = w.minimumSizeHint().width()
    w.set_state("failed: " + "the controller refused the request because something long happened " * 5)
    app.processEvents()
    assert w.minimumSizeHint().width() == narrow
    assert w.state.toolTip().startswith("failed: the controller refused")     # readable in full on hover
    assert w.statusBar().isAncestorOf(w.state) and w.statusBar().isAncestorOf(w.phase)


def test_the_run_clock_shows_elapsed_time_vials_and_throughput(window, monkeypatch):
    w, _ = window
    w.rows = {f"v{i}": i for i in range(36)}
    w.runner = SimpleNamespace(vials_finished=lambda: 18)
    w.run_started, w.run_finished = 100.0, 100.0 + 150.0                         # 2:30 run
    w._show_clock()
    assert w.clock.text() == "2:30   18/36 vials   7.2 vials/min"
    w.runner = None


def test_a_completed_run_can_run_again(window):
    """A swap undoes itself on a second run; Run checks the deck again before each run anyway."""
    w, _ = window
    w.result = _plan_result(ready=True)
    w.runner = SimpleNamespace(plan=None, orca_phase=lambda: "completed", vials_finished=lambda: 8,
                               isRunning=lambda: False)
    w.run_started, w.run_finished = 100.0, 160.0
    try:
        w.run_ended("completed", "")
        assert w.run_button.isEnabled() and w.run_button.text() == "Run again"
        w.run_ended("failed", "the controller refused")
        assert w.run_button.isEnabled() and w.run_button.text() == "Run on the cell"
    finally:
        w.runner = None


def test_routes_show_in_the_plan_and_the_route_fills_in_when_decided(window):
    w, _ = window
    spec = WorkflowSpec.model_validate({"name": "t", "summary": "s", "processes": [
        {"id": "sort", "source": {"bay": 3, "wells": ["C1"]}, "steps": [{"op": "weigh"}],
         "routes": [{"when": {"value": "grams", "gt": 2}, "then": {"bay": 8}}], "destination": {"bay": 3}}]})
    report = validate(spec, DemoInventory())
    from agent.ui import _spec_tree
    w._fill_table(spec, report)
    root = _spec_tree(spec)                                  # kept: Qt deletes an unreferenced item
    process = root.child(0)
    assert process.text(0) == "sort: bay 3 wells C1 -> routes below, otherwise bay 3, same wells"
    assert process.child(1).text(0) == "route: if grams > 2.0: bay 8, same wells"
    assert w.table.item(0, 0).text() == "bay 3 C1 -> (a route decides)"
    record = {"process": "sort", "well": "C1", "status": "done", "grams": 2.3, "to": "bay 8 C1"}
    w.runner = SimpleNamespace(plan=SimpleNamespace(results={"vial_bay3_C1": record}), orca_phase=lambda: "running",
                               vials_finished=lambda: 1)
    w.refresh()
    assert w.table.item(0, 0).text() == "bay 3 C1 -> bay 8 C1"
    w.runner = None


# ------------------------------------------------------------------------- the gear and the connection pill
def _fake_controller(fault=None):
    """A one-thread TCP server that answers every line with the controller's status reply."""
    import json
    import socket
    import threading
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                if conn.recv(1024):
                    conn.sendall(json.dumps({"id": 1, "ok": True, "status": {"fault": fault}}).encode() + b"\n")
    threading.Thread(target=serve, daemon=True).start()
    return server


def _check_until(w, app, port, demo=False):
    import time
    w.settings.set_values("127.0.0.1", port, demo)
    w.connection_state = None
    w.check_connection()
    deadline = time.monotonic() + 5
    while w.connection_state is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return w.connection_state, w.connection.text()


def test_the_pill_shows_whether_the_controller_answers(window):
    w, app = window
    w.check_timer.stop()                                       # only the checks this test asks for
    ok, faulted = _fake_controller(), _fake_controller(fault="arm collision")
    try:
        assert _check_until(w, app, ok.getsockname()[1]) == ("connected", "● Connected")
        assert _check_until(w, app, faulted.getsockname()[1]) == ("fault", "● Controller fault")
        assert "arm collision" in w.connection.toolTip()
        closed = ok.getsockname()[1]
        ok.close()
        assert _check_until(w, app, closed) == ("offline", "● Not connected")
        assert _check_until(w, app, closed, demo=True) == ("demo", "● Demo deck")
    finally:
        ok.close()
        faulted.close()
        w.settings.set_values("127.0.0.1", 8700, False)


def test_the_gear_sits_top_right_and_holds_the_controller_settings(window):
    w, _ = window
    assert w.gear.text() == "⚙" and w.connection.parent() is w.gear.parent()
    w.settings.set_values("192.168.64.4", 8701, False)
    assert w._address() == ("192.168.64.4", 8701)
    assert w.demo_deck is w.settings.demo_deck
    w.settings.set_values("127.0.0.1", 8700, False)


def test_a_new_address_replaces_a_check_still_waiting(window):
    """Changing the port while a check is under way: the answer shown is the new address's, at once."""
    import time
    w, app = window
    w.check_timer.stop()
    ok = _fake_controller()
    try:
        w.checker.check("192.0.2.1", 9)                        # an address that never answers (TEST-NET-1)
        state, text = _check_until(w, app, ok.getsockname()[1])
        assert (state, text) == ("connected", "● Connected")
        time.sleep(0.1)
        app.processEvents()
        assert w.connection_state == "connected"               # the dropped check never reports back
    finally:
        ok.close()
        w.settings.set_values("127.0.0.1", 8700, False)


def test_the_conversation_shows_requests_tool_calls_and_replies_in_order(window, monkeypatch):
    from agent import ui
    w, app = window
    w.feed.clear()

    class Planner(ui.PlanWorker):                                # two tool calls, then a plan ready
        def run(self):
            self.tool_call.emit("describe_cell({})")
            self.tool_call.emit('rack_inventory({"bay": 3})')
            self.planned.emit(_plan_result(ready=True))
    monkeypatch.setattr(ui, "PlanWorker", Planner)
    w.load_button.menu().actions()[0].trigger()                  # an example: sent at once
    _settle(w, app)
    text = w.reply.toPlainText()
    order = [text.index(s) for s in ("Heat the first four", "Claude calls describe_cell({})",
                                     'Claude calls rack_inventory({"bay": 3})', "Planned.")]
    assert order == sorted(order) and "planning ..." not in text
    assert w.feed.toPlainText() == ""                            # Claude's activity stays out of the Run feed
    w.new_button.click()                                         # a new conversation
    assert w.reply.toPlainText() == "" and w.activity == []


def test_dialogs_open_centred_on_the_window(window):
    from PySide6.QtWidgets import QMessageBox
    w, _ = window
    w.move(200, 150)
    box = QMessageBox(QMessageBox.Question, "Approve the plan", "Run it?", QMessageBox.Yes | QMessageBox.No, w)
    box.exec = lambda: QMessageBox.Yes                           # do not block the test
    assert w.show_centered(box) == QMessageBox.Yes
    centre = box.geometry().center()
    assert abs(centre.x() - w.frameGeometry().center().x()) <= 2
    assert abs(centre.y() - w.frameGeometry().center().y()) <= 2


def test_the_gear_is_a_plain_symbol_that_opens_the_settings_on_a_click(window, monkeypatch):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QAbstractButton, QApplication
    w, _ = window
    assert not isinstance(w.gear, QAbstractButton) and w.gear.cursor().shape() == Qt.PointingHandCursor
    opened = []
    monkeypatch.setattr(w, "show_centered", lambda dialog: opened.append(dialog) or 0)
    centre = QPointF(w.gear.rect().center())
    for kind in (QMouseEvent.MouseButtonPress, QMouseEvent.MouseButtonRelease):
        QApplication.sendEvent(w.gear, QMouseEvent(kind, centre, centre, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    assert opened == [w.settings]


def test_a_confirmed_plan_shows_green_in_the_conversation(window):
    w, _ = window
    w.new_conversation()
    w.planned(QUESTION)
    w.planned(_plan_result(ready=True))
    assert [kind for kind, _ in w.activity] == ["claude", "plan"]
    html = w.reply.toHtml()
    from PySide6.QtGui import QColor
    from agent.ui import _mix
    green = _mix(w.reply.palette().base().color(), QColor("#2e7d32"), 0.25).name()
    assert green in html
    w.activity = w.activity[:-1]                                         # the question alone: no green
    w._show_conversation()
    assert green not in w.reply.toHtml()


def test_run_this_under_a_confirmed_plan_goes_to_the_run_step_with_that_plan(window):
    from PySide6.QtCore import QUrl

    from agent.ui import RUN
    w, _ = window
    w.demo_deck.setChecked(False)
    w.new_conversation()
    first, second = _plan_result(ready=True), _plan_result(ready=True)
    w.planned(first)
    w.planned(second)                                                    # refined: a second confirmed plan
    assert w.reply.toHtml().count('href="run:') == 2
    w.reply.anchorClicked.emit(QUrl("run:0"))                            # the earlier one
    assert w.pages.currentIndex() == RUN and w.result is first and w.run_button.isEnabled()
    w.reply.anchorClicked.emit(QUrl("run:1"))
    assert w.result is second
    w.demo_deck.setChecked(True)                                         # a demo-deck plan cannot run
    try:
        w._show_conversation()
        assert 'href="run:' not in w.reply.toHtml()
    finally:
        w.demo_deck.setChecked(False)
