"""Tests for native remote operator mode primitives."""

from unittest.mock import Mock, patch

from gateway.operator_mode import (
    OperatorAction,
    OperatorActionOutcome,
    OperatorDecision,
    OperatorRun,
    TMUX_MAX_SESSIONS,
    classify_operator_run,
    detect_operator_decision,
    find_exact_run,
    list_tmux_operator_runs,
    resume_tmux_run,
)


def test_detects_approval_block():
    assert detect_operator_decision("Approval required: Allow once or Deny") == OperatorDecision.BLOCKED_APPROVAL


def test_detects_question_block():
    assert detect_operator_decision("Please choose an option") == OperatorDecision.BLOCKED_QUESTION
    assert detect_operator_decision("Proceed with deploy?") == OperatorDecision.BLOCKED_QUESTION


def test_detects_inactivity_block():
    assert detect_operator_decision("No activity for 15 minutes") == OperatorDecision.BLOCKED_INACTIVITY


def test_classify_operator_run_preserves_fields():
    run = OperatorRun(run_id="tmux:one", target="one", last_output="tests passed")
    classified = classify_operator_run(run)
    assert classified.status == OperatorDecision.COMPLETE
    assert classified.run_id == run.run_id
    assert classified.target == run.target


def test_tmux_inventory_missing_tmux_is_empty():
    with patch("gateway.operator_mode.shutil.which", return_value=None):
        assert list_tmux_operator_runs() == []


def test_tmux_inventory_lists_sessions_and_captures_status():
    calls = []

    def fake_run(cmd, text, capture_output, timeout, check):
        calls.append(cmd)
        if cmd[1] == "list-sessions":
            return Mock(returncode=0, stdout="agent-one\t0\t1710000000\n", stderr="")
        if cmd[1] == "capture-pane":
            return Mock(returncode=0, stdout="Approval required: approve or deny\n", stderr="")
        raise AssertionError(cmd)

    with patch("gateway.operator_mode.shutil.which", return_value="/usr/bin/tmux"), patch(
        "gateway.operator_mode.subprocess.run", side_effect=fake_run
    ):
        runs = list_tmux_operator_runs()

    assert len(runs) == 1
    assert runs[0].run_id == "tmux:agent-one"
    assert runs[0].target == "agent-one"
    assert runs[0].status == OperatorDecision.BLOCKED_APPROVAL
    assert calls[0][:3] == ["tmux", "list-sessions", "-F"]
    assert calls[1][:4] == ["tmux", "capture-pane", "-pt", "agent-one"]


def test_tmux_inventory_caps_sessions():
    calls = []
    sessions = "".join(f"agent-{i}\t0\t{i}\n" for i in range(TMUX_MAX_SESSIONS + 5))

    def fake_run(cmd, text, capture_output, timeout, check):
        calls.append(cmd)
        if cmd[1] == "list-sessions":
            return Mock(returncode=0, stdout=sessions, stderr="")
        if cmd[1] == "capture-pane":
            return Mock(returncode=0, stdout="running\n", stderr="")
        raise AssertionError(cmd)

    with patch("gateway.operator_mode.shutil.which", return_value="/usr/bin/tmux"), patch(
        "gateway.operator_mode.subprocess.run", side_effect=fake_run
    ):
        runs = list_tmux_operator_runs()

    assert len(runs) == TMUX_MAX_SESSIONS
    assert sum(1 for call in calls if call[1] == "capture-pane") == TMUX_MAX_SESSIONS


def test_tmux_inventory_stops_at_total_deadline():
    now_values = iter([0.0, 10.0])

    with patch("gateway.operator_mode.shutil.which", return_value="/usr/bin/tmux"), patch(
        "gateway.operator_mode.time.monotonic", side_effect=lambda: next(now_values)
    ), patch("gateway.operator_mode.subprocess.run") as mock_run:
        assert list_tmux_operator_runs() == []

    mock_run.assert_not_called()


def test_find_exact_run_refuses_missing_and_ambiguous():
    one = OperatorRun(run_id="1", target="same")
    two = OperatorRun(run_id="2", target="same")
    assert find_exact_run("same", [one]) == one
    assert find_exact_run("missing", [one]) is None
    assert find_exact_run("same", [one, two]) is None


def test_resume_tmux_refuses_target_mismatch_and_non_allowlisted_action():
    run = OperatorRun(run_id="tmux:agent", target="agent")
    mismatch = resume_tmux_run(run, OperatorAction.CONTINUE, target="other")
    assert mismatch.outcome == OperatorActionOutcome.REFUSED
    refused = resume_tmux_run(run, OperatorAction.STOP, target="agent")
    assert refused.outcome == OperatorActionOutcome.REFUSED


def test_resume_tmux_sends_allowlisted_action_to_exact_target():
    sent = []

    def fake_run(cmd, text, capture_output, timeout, check):
        sent.append(cmd)
        return Mock(returncode=0, stdout="", stderr="")

    run = OperatorRun(run_id="tmux:agent", target="agent")
    with patch("gateway.operator_mode.shutil.which", return_value="/usr/bin/tmux"), patch(
        "gateway.operator_mode.subprocess.run", side_effect=fake_run
    ):
        result = resume_tmux_run(run, OperatorAction.APPROVE_ONCE, target="agent")

    assert result.outcome == OperatorActionOutcome.SENT
    assert sent == [["tmux", "send-keys", "-t", "agent", "approve", "Enter"]]


def test_resume_tmux_has_no_text_override_parameter():
    run = OperatorRun(run_id="tmux:agent", target="agent")
    with patch("gateway.operator_mode.shutil.which", return_value="/usr/bin/tmux"):
        try:
            resume_tmux_run(run, OperatorAction.CONTINUE, target="agent", text="ship it")  # type: ignore[call-arg]
        except TypeError:
            return
    raise AssertionError("resume_tmux_run accepted an arbitrary text override")
