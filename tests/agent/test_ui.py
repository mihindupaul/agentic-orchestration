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


def test_the_conversation_is_folded_away_and_opens_only_when_claude_asks(window, monkeypatch):
    w, _ = window
    w.load_button.menu().actions()[0].trigger()
    assert not w.conversation_toggle.isChecked() and w.reply.isHidden()          # folded by default
    monkeypatch.setattr(w, "ask_person", lambda question: None)
    w.planned(_plan_result(ready=False))                                         # a question: opened
    assert w.conversation_toggle.isChecked() and not w.reply.isHidden()
    w.planned(_plan_result(ready=True))                                          # a plan ready: folded again
    assert not w.conversation_toggle.isChecked() and w.reply.isHidden()


def test_a_conversation_opened_by_the_person_stays_open(window):
    w, _ = window
    w.load_button.menu().actions()[0].trigger()
    w.conversation_toggle.click()                                                # opened by hand
    w.planned(_plan_result(ready=True))
    assert w.conversation_toggle.isChecked() and not w.reply.isHidden()


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


def test_a_completed_run_can_run_again(window):
    """A swap undoes itself on a second run; Run checks the deck again before each run anyway."""
    w, _ = window
    w.runner = SimpleNamespace(plan=None, orca_phase=lambda: "completed", vials_finished=lambda: 8)
    w.run_started, w.run_finished = 100.0, 160.0
    w.run_ended("completed", "")
    assert w.run_button.isEnabled() and w.run_button.text() == "Run again"
    w.run_ended("failed", "the controller refused")
    assert w.run_button.isEnabled() and w.run_button.text() == "Run on the cell"
