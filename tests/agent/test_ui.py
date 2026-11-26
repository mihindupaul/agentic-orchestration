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


def test_examples_load_into_the_request(window):
    w, _ = window
    actions = w.load_button.menu().actions()
    assert len(actions) >= 10
    actions[0].trigger()
    assert w.request.toPlainText().startswith("Heat the first four vials of column 6")


def test_a_plan_is_shown_with_its_size_and_the_deck_check(window):
    w, _ = window
    spec = WorkflowSpec.model_validate({"name": "t", "summary": "s", "processes": [
        {"id": "p", "source": {"bay": 3, "rows": "C"}, "destination": {"bay": 8}, "steps": [
            {"op": "scan"}, {"op": "weigh"},
            {"op": "heat", "celsius": 40, "hold_s": 5, "rules": [{"when": {"value": "grams", "lt": 2}, "then": "skip"}]}]}]})
    report = validate(spec, DemoInventory())
    w._show_spec(spec)
    w._show_findings(spec, report)
    w._fill_table(spec, report)
    assert "12 vials, up to 48 arm moves, 0 to 12 heater visits" in w.findings.text()
    assert w.tree.topLevelItem(0).child(0).child(2).child(0).text(0) == "if grams < 2.0: skip"
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


def test_a_question_from_claude_pops_up_and_an_answer_continues_the_conversation(window, monkeypatch):
    w, _ = window
    asked, continued = [], []
    monkeypatch.setattr(w, "ask_person", lambda question: asked.append(question) or "50 C, 10 s")
    monkeypatch.setattr(w, "_ask", continued.append)
    w.planned(QUESTION)
    assert asked == ["Which temperature?"] and continued == ["50 C, 10 s"]
    assert w.conversation == QUESTION.conversation and not w.run_button.isEnabled()


def test_abort_stops_the_planning(window, monkeypatch):
    w, _ = window
    monkeypatch.setattr(w, "ask_person", lambda question: None)
    monkeypatch.setattr(w, "_ask", lambda text: pytest.fail("planning should have stopped"))
    w.planned(QUESTION)
    assert w.state.text() == "planning stopped" and not w.run_button.isEnabled()


def test_the_question_dialog_shows_the_question_and_returns_the_answer(window):
    from agent.ui import QuestionDialog
    dialog = QuestionDialog("Which **temperature**?")
    assert "Which temperature?" in dialog.findChild(QTextBrowser).toPlainText()
    assert dialog.answer() is None                      # nothing typed: no answer
    dialog.box.setPlainText("  50 C  ")
    assert dialog.answer() == "50 C"


def test_loading_an_example_clears_the_previous_plan_and_run(window):
    w, _ = window
    w.reply.setPlainText("old conversation")
    w.findings.setText("old findings")
    w.feed.appendPlainText("old feed line")
    w.table.setRowCount(3)
    w.run_button.setEnabled(True)
    w.conversation = [{"role": "user", "content": "old"}]
    w.load_button.menu().actions()[1].trigger()
    assert w.request.toPlainText().startswith("Heat the first four vials of column 6") and "column 12" in w.request.toPlainText()
    assert not w.reply.toPlainText() and not w.findings.text() and not w.feed.toPlainText()
    assert w.table.rowCount() == 0 and w.tree.topLevelItemCount() == 0 and not w.run_button.isEnabled()
    assert w.conversation == [] and w.state.text() == "idle"


def _plan_result(ready: bool):
    spec = WorkflowSpec.model_validate({"name": "t", "summary": "s", "processes": [
        {"id": "p", "source": {"bay": 3, "rows": "C"}, "destination": {"bay": 8}, "steps": [{"op": "scan"}]}]})
    return SimpleNamespace(spec=spec, ready=ready, report=validate(spec, DemoInventory()), reply="Planned.",
                           conversation=[{"role": "user", "content": "r"}, {"role": "assistant", "content": "Planned."}])


def test_run_is_offered_only_for_a_plan_marked_ready(window, monkeypatch):
    w, _ = window
    w.demo_deck.setChecked(False)
    asked = []
    monkeypatch.setattr(w, "ask_person", lambda question: asked.append(question) or None)
    w.planned(_plan_result(ready=False))                 # accepted but not ready: Claude wants an answer
    assert asked == ["Planned."] and not w.run_button.isEnabled() and w.tree.topLevelItemCount() == 1
    w.tree.clear()
    w.planned(_plan_result(ready=True))
    assert w.run_button.isEnabled() and w.state.text().startswith("planned") and asked == ["Planned."]


def test_the_window_shows_its_name_in_the_title_bar_and_on_top(window):
    w, _ = window
    assert w.windowTitle() == "Agentic Orchestration"
    assert "Words to Work" in w.centralWidget().findChild(QLabel).text()


def test_the_window_walks_through_describe_plan_and_run(window, monkeypatch):
    from agent.ui import DESCRIBE, PLAN, RUN
    w, _ = window
    w.demo_deck.setChecked(False)
    w.load_button.menu().actions()[0].trigger()
    assert w.pages.currentIndex() == DESCRIBE and w.reachable_steps() == {DESCRIBE}
    assert not w.step_bar.buttons[PLAN].isEnabled() and not w.to_run.isEnabled()

    monkeypatch.setattr(w, "ask_person", lambda question: None)
    w.planned(_plan_result(ready=False))                         # a question: still describing
    assert w.pages.currentIndex() == DESCRIBE and w.reachable_steps() == {DESCRIBE, PLAN}

    w.planned(_plan_result(ready=True))                          # a plan ready: the Plan step opens by itself
    assert w.pages.currentIndex() == PLAN and w.to_run.isEnabled() and w.step_bar.buttons[RUN].isEnabled()
    w.to_run.click()
    assert w.pages.currentIndex() == RUN and w.run_button.isEnabled()
    w.step_bar.buttons[DESCRIBE].click()                         # back: the step bar
    assert w.pages.currentIndex() == DESCRIBE
    w.step_bar.buttons[PLAN].click()                             # the bar moves between steps too
    assert w.pages.currentIndex() == PLAN


def test_a_plan_on_the_demo_deck_can_be_reviewed_but_not_run(window):
    from agent.ui import PLAN, RUN
    w, _ = window
    w.demo_deck.setChecked(True)
    try:
        w.planned(_plan_result(ready=True))
        assert w.pages.currentIndex() == PLAN and RUN not in w.reachable_steps() and not w.to_run.isEnabled()
        assert "demo deck" in w.to_run.toolTip()
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
    w.tree.clear()
    w._show_spec(spec)
    w._fill_table(spec, report)
    process = w.tree.topLevelItem(0).child(0)
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
    w.load_button.menu().actions()[0].trigger()
    w.feed.clear()

    class Planner(ui.PlanWorker):                                # two tool calls, then a plan ready
        def run(self):
            self.tool_call.emit("describe_cell({})")
            self.tool_call.emit('rack_inventory({"bay": 3})')
            self.planned.emit(_plan_result(ready=True))
    monkeypatch.setattr(ui, "PlanWorker", Planner)
    w.plan()
    w.planner.wait(2000)
    app.processEvents()
    text = w.reply.toPlainText()
    order = [text.index(s) for s in ("You: Heat the first four", "Claude calls describe_cell({})",
                                     'Claude calls rack_inventory({"bay": 3})', "Claude: Planned.")]
    assert order == sorted(order) and "planning ..." not in text
    assert w.feed.toPlainText() == ""                            # Claude's activity stays out of the Run feed
    w.load_button.menu().actions()[0].trigger()                  # a new request: a new conversation
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
