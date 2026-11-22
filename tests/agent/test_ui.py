"""The agent's window, headless (Qt's offscreen platform): it builds, loads examples, shows a plan, filters
Orca's log into the feed. Planning and running are tested elsewhere; no API, no cell here."""

import logging
import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("anthropic")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace  # noqa: E402

from PySide6.QtWidgets import QApplication, QTextBrowser  # noqa: E402

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
    assert w.request.toPlainText().startswith("Scan and weigh every vial in the first two rows")


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
    assert w.table.rowCount() == 12 and w.table.item(0, 0).text() == "bay 3 C1"


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
    assert w.request.toPlainText().startswith("Scan and weigh rows C and E")
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
