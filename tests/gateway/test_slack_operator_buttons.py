"""Tests for Slack remote operator status buttons."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules:
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    sys.modules["slack_bolt"] = slack_bolt
    sys.modules["slack_bolt.async_app"] = slack_bolt.async_app
    handler_mod = MagicMock()
    handler_mod.AsyncSocketModeHandler = MagicMock
    sys.modules["slack_bolt.adapter"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode.async_handler"] = handler_mod
    sdk_mod = MagicMock()
    sdk_mod.web = MagicMock()
    sdk_mod.web.async_client = MagicMock()
    sdk_mod.web.async_client.AsyncWebClient = MagicMock
    sys.modules["slack_sdk"] = sdk_mod
    sys.modules["slack_sdk.web"] = sdk_mod.web
    sys.modules["slack_sdk.web.async_client"] = sdk_mod.web.async_client


_ensure_slack_mock()

from gateway.config import PlatformConfig
from gateway.operator_mode import OperatorActionOutcome, OperatorDecision, OperatorRun
from gateway.platforms.slack import SlackAdapter


def _make_adapter():
    config = PlatformConfig(enabled=True, token="***")
    adapter = SlackAdapter(config)
    adapter._app = MagicMock()
    adapter._bot_user_id = "U_BOT"
    adapter._team_clients = {"T1": AsyncMock()}
    adapter._team_bot_user_ids = {"T1": "U_BOT"}
    adapter._channel_team = {"C1": "T1"}
    return adapter


@pytest.mark.asyncio
async def test_send_operator_status_card_posts_buttons():
    adapter = _make_adapter()
    mock_client = adapter._team_clients["T1"]
    mock_client.chat_postMessage = AsyncMock(return_value={"ts": "44.55"})
    run = OperatorRun(
        run_id="tmux:agent-one",
        target="agent-one",
        status=OperatorDecision.BLOCKED_APPROVAL,
        last_output="Approval required",
    )

    result = await adapter.send_operator_status_card("C1", run)

    assert result.success is True
    assert result.message_id == "44.55"
    assert adapter._operator_resolved["44.55"] is False
    assert adapter._operator_runs["tmux:agent-one"] == run
    kwargs = mock_client.chat_postMessage.call_args[1]
    assert kwargs["text"] == "Hermes operator status: tmux:agent-one is blocked_approval"
    blocks = kwargs["blocks"]
    elements = blocks[1]["elements"]
    action_ids = [element["action_id"] for element in elements]
    assert action_ids == [
        "hermes_operator_continue",
        "hermes_operator_approve_once",
        "hermes_operator_deny",
        "hermes_operator_summary",
    ]
    payload = json.loads(elements[0]["value"])
    assert payload == {"run_id": "tmux:agent-one", "target": "agent-one"}


@pytest.mark.asyncio
async def test_operator_continue_calls_resume_bridge_and_updates_message(monkeypatch):
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    adapter = _make_adapter()
    run = OperatorRun(run_id="tmux:agent-one", target="agent-one", last_output="waiting", metadata={"created": "171"})
    adapter._operator_runs[run.run_id] = run
    adapter._operator_resolved["44.55"] = False
    mock_client = adapter._team_clients["T1"]
    mock_client.chat_update = AsyncMock()
    ack = AsyncMock()
    body = {
        "message": {
            "ts": "44.55",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "operator card"}}],
        },
        "channel": {"id": "C1"},
        "user": {"id": "U1", "name": "sameer"},
    }
    action = {
        "action_id": "hermes_operator_continue",
        "value": json.dumps({"run_id": run.run_id, "target": run.target}),
    }
    bridge_result = MagicMock(outcome=OperatorActionOutcome.SENT, message="Action sent to tmux")

    current_run = OperatorRun(run_id="tmux:agent-one", target="agent-one", last_output="waiting now", metadata={"created": "171"})
    with patch("gateway.platforms.slack.list_tmux_operator_runs", return_value=[current_run]), patch(
        "gateway.platforms.slack.resume_tmux_run", return_value=bridge_result
    ) as mock_resume:
        await adapter._handle_operator_action(ack, body, action)

    ack.assert_called_once()
    mock_resume.assert_called_once()
    assert mock_resume.call_args.args[0] == current_run
    assert mock_resume.call_args.args[1].value == "continue"
    assert mock_resume.call_args.kwargs == {"target": "agent-one"}
    mock_client.chat_update.assert_called_once()
    update_kwargs = mock_client.chat_update.call_args[1]
    assert "Continue selected by sameer. Action sent to tmux" in update_kwargs["text"]
    assert "44.55" not in adapter._operator_resolved


@pytest.mark.asyncio
async def test_operator_double_click_does_not_call_resume(monkeypatch):
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    adapter = _make_adapter()
    run = OperatorRun(run_id="tmux:agent-one", target="agent-one")
    adapter._operator_runs[run.run_id] = run
    adapter._operator_resolved["44.55"] = True
    ack = AsyncMock()
    body = {"message": {"ts": "44.55", "blocks": []}, "channel": {"id": "C1"}, "user": {"id": "U1", "name": "sameer"}}
    action = {"action_id": "hermes_operator_continue", "value": json.dumps({"run_id": run.run_id, "target": run.target})}

    with patch("gateway.platforms.slack.resume_tmux_run") as mock_resume:
        await adapter._handle_operator_action(ack, body, action)

    ack.assert_called_once()
    mock_resume.assert_not_called()


@pytest.mark.asyncio
async def test_operator_action_reuses_slack_allowed_users(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_ALLOWED")
    adapter = _make_adapter()
    run = OperatorRun(run_id="tmux:agent-one", target="agent-one")
    adapter._operator_runs[run.run_id] = run
    adapter._operator_resolved["44.55"] = False
    ack = AsyncMock()
    body = {"message": {"ts": "44.55", "blocks": []}, "channel": {"id": "C1"}, "user": {"id": "U_DENIED", "name": "mallory"}}
    action = {"action_id": "hermes_operator_continue", "value": json.dumps({"run_id": run.run_id, "target": run.target})}

    with patch("gateway.platforms.slack.resume_tmux_run") as mock_resume:
        await adapter._handle_operator_action(ack, body, action)

    ack.assert_called_once()
    mock_resume.assert_not_called()
    assert adapter._operator_resolved["44.55"] is False


@pytest.mark.asyncio
async def test_operator_continue_refuses_reused_tmux_session(monkeypatch):
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    adapter = _make_adapter()
    run = OperatorRun(run_id="tmux:agent-one", target="agent-one", metadata={"created": "old"})
    adapter._operator_runs[run.run_id] = run
    adapter._operator_resolved["44.55"] = False
    mock_client = adapter._team_clients["T1"]
    mock_client.chat_update = AsyncMock()
    ack = AsyncMock()
    body = {"message": {"ts": "44.55", "blocks": []}, "channel": {"id": "C1"}, "user": {"id": "U1", "name": "sameer"}}
    action = {"action_id": "hermes_operator_continue", "value": json.dumps({"run_id": run.run_id, "target": run.target})}
    current = OperatorRun(run_id="tmux:agent-one", target="agent-one", metadata={"created": "new"})

    with patch("gateway.platforms.slack.list_tmux_operator_runs", return_value=[current]), patch(
        "gateway.platforms.slack.resume_tmux_run"
    ) as mock_resume:
        await adapter._handle_operator_action(ack, body, action)

    mock_resume.assert_not_called()
    update_kwargs = mock_client.chat_update.call_args[1]
    assert "Refused: tmux session metadata changed" in update_kwargs["text"]


@pytest.mark.asyncio
async def test_operator_pause_refuses_without_consuming_card(monkeypatch):
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    adapter = _make_adapter()
    run = OperatorRun(run_id="tmux:agent-one", target="agent-one")
    adapter._operator_runs[run.run_id] = run
    adapter._operator_resolved["44.55"] = False
    mock_client = adapter._team_clients["T1"]
    mock_client.chat_update = AsyncMock()
    ack = AsyncMock()
    body = {"message": {"ts": "44.55", "blocks": []}, "channel": {"id": "C1"}, "user": {"id": "U1", "name": "sameer"}}
    action = {"action_id": "hermes_operator_pause", "value": json.dumps({"run_id": run.run_id, "target": run.target})}

    with patch("gateway.platforms.slack.resume_tmux_run") as mock_resume:
        await adapter._handle_operator_action(ack, body, action)

    mock_resume.assert_not_called()
    assert adapter._operator_resolved["44.55"] is False
    update_kwargs = mock_client.chat_update.call_args[1]
    assert "Refused: Pause is not implemented yet" in update_kwargs["text"]
    action_ids = [element["action_id"] for element in update_kwargs["blocks"][-1]["elements"]]
    assert "hermes_operator_pause" not in action_ids
    assert "hermes_operator_stop" not in action_ids


@pytest.mark.asyncio
async def test_operator_status_card_escapes_untrusted_mrkdwn():
    adapter = _make_adapter()
    mock_client = adapter._team_clients["T1"]
    mock_client.chat_postMessage = AsyncMock(return_value={"ts": "44.55"})
    run = OperatorRun(
        run_id="tmux:<run>&`id`",
        target="agent>`one`",
        status=OperatorDecision.BLOCKED_QUESTION,
        last_output="before ``` injected <tag> & after",
    )

    await adapter.send_operator_status_card("C1", run)

    blocks = mock_client.chat_postMessage.call_args[1]["blocks"]
    section_text = blocks[0]["text"]["text"]
    assert "&lt;run&gt;&amp;'id'" in section_text
    assert "agent&gt;'one'" in section_text
    assert "<tag>" not in section_text
    assert "&lt;tag&gt; &amp;" in section_text
    assert section_text.count("```") == 2
