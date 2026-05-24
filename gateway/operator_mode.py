"""Native remote operator mode primitives.

This module is intentionally small and deterministic. It does not call an LLM.
The first vertical slice focuses on conservative stuck detection, read-only tmux
inventory, and a narrow tmux resume bridge that refuses ambiguous targets.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Optional


class OperatorAction(str, Enum):
    """Operator actions exposed to adapters."""

    CONTINUE = "continue"
    APPROVE_ONCE = "approve_once"
    DENY = "deny"
    PAUSE = "pause"
    STOP = "stop"
    SUMMARY = "summary"


class OperatorDecision(str, Enum):
    """Conservative run state inferred from provided text/output."""

    RUNNING = "running"
    BLOCKED_APPROVAL = "blocked_approval"
    BLOCKED_QUESTION = "blocked_question"
    BLOCKED_INACTIVITY = "blocked_inactivity"
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class OperatorActionOutcome(str, Enum):
    """Outcome status of attempting an operator action."""

    SENT = "sent"
    REFUSED = "refused"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True)
class OperatorRun:
    """A remotely observable operator run.

    ``target`` is the exact native target identifier. For tmux runs this is the
    tmux session name. Resume calls must match this value exactly.
    """

    run_id: str
    target: str
    source: str = "tmux"
    status: OperatorDecision = OperatorDecision.UNKNOWN
    last_output: str = ""
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OperatorActionResult:
    """Structured result returned by action bridges."""

    outcome: OperatorActionOutcome
    message: str
    action: Optional[OperatorAction] = None
    target: str = ""


APPROVAL_PATTERNS = (
    r"\bapproval required\b",
    r"\bapprove\b.*\bdeny\b",
    r"\ballow once\b",
    r"\brequires approval\b",
    r"\bpermission to proceed\b",
)

QUESTION_PATTERNS = (
    r"\?\s*$",
    r"\bplease choose\b",
    r"\bselect an option\b",
    r"\b(y/n)\b",
    r"\bconfirm\b.*\bcontinue\b",
)

INACTIVITY_PATTERNS = (
    r"\bstill waiting\b",
    r"\bno activity\b",
    r"\binactive\b",
    r"\bstalled\b",
    r"\btimeout waiting\b",
)

COMPLETE_PATTERNS = (
    r"\btask complete\b",
    r"\bdone\b",
    r"\bfinished\b",
    r"\btests? passed\b",
)

TMUX_TIMEOUT_SECONDS = 2.0
TMUX_INVENTORY_DEADLINE_SECONDS = 5.0
TMUX_CAPTURE_LINES = 200
TMUX_MAX_SESSIONS = 25
RESUME_TEXT_BY_ACTION = {
    OperatorAction.CONTINUE: "continue",
    OperatorAction.APPROVE_ONCE: "approve",
    OperatorAction.DENY: "deny",
}
RESUME_ALLOWED_ACTIONS = frozenset(RESUME_TEXT_BY_ACTION)


def detect_operator_decision(text: str | None) -> OperatorDecision:
    """Return a conservative blocked/running status from supplied output text."""

    raw = (text or "").strip()
    if not raw:
        return OperatorDecision.UNKNOWN
    lowered = raw.lower()

    for pattern in APPROVAL_PATTERNS:
        if re.search(pattern, lowered, re.MULTILINE):
            return OperatorDecision.BLOCKED_APPROVAL
    for pattern in QUESTION_PATTERNS:
        if re.search(pattern, lowered, re.MULTILINE):
            return OperatorDecision.BLOCKED_QUESTION
    for pattern in INACTIVITY_PATTERNS:
        if re.search(pattern, lowered, re.MULTILINE):
            return OperatorDecision.BLOCKED_INACTIVITY
    for pattern in COMPLETE_PATTERNS:
        if re.search(pattern, lowered, re.MULTILINE):
            return OperatorDecision.COMPLETE
    return OperatorDecision.RUNNING


def classify_operator_run(run: OperatorRun) -> OperatorRun:
    """Return ``run`` with status inferred from its ``last_output``."""

    return OperatorRun(
        run_id=run.run_id,
        target=run.target,
        source=run.source,
        status=detect_operator_decision(run.last_output),
        last_output=run.last_output,
        updated_at=run.updated_at,
        metadata=dict(run.metadata),
    )


def _tmux_exists() -> bool:
    return shutil.which("tmux") is not None


def _run_tmux(args: list[str], *, timeout: float = TMUX_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _remaining_deadline_timeout(deadline: float) -> float | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(TMUX_TIMEOUT_SECONDS, remaining)


def _capture_tmux_pane(target: str, *, timeout: float = TMUX_TIMEOUT_SECONDS) -> str:
    result = _run_tmux(["capture-pane", "-pt", target, "-S", f"-{TMUX_CAPTURE_LINES}"], timeout=timeout)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def list_tmux_operator_runs() -> list[OperatorRun]:
    """Return tmux session inventory as operator runs.

    Missing tmux, timeouts, or tmux errors return an empty list. This function is
    read-only and timeout bound.
    """

    if not _tmux_exists():
        return []
    deadline = time.monotonic() + TMUX_INVENTORY_DEADLINE_SECONDS
    try:
        timeout = _remaining_deadline_timeout(deadline)
        if timeout is None:
            return []
        result = _run_tmux(
            ["list-sessions", "-F", "#{session_name}\t#{session_attached}\t#{session_created}"],
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    runs: list[OperatorRun] = []
    for line in result.stdout.splitlines()[:TMUX_MAX_SESSIONS]:
        timeout = _remaining_deadline_timeout(deadline)
        if timeout is None:
            break
        if not line.strip():
            continue
        parts = line.split("\t")
        session = parts[0].strip()
        if not session:
            continue
        output = ""
        try:
            output = _capture_tmux_pane(session, timeout=timeout)
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            output = ""
        status = detect_operator_decision(output)
        attached = parts[1].strip() if len(parts) > 1 else ""
        created = parts[2].strip() if len(parts) > 2 else ""
        runs.append(
            OperatorRun(
                run_id=f"tmux:{session}",
                target=session,
                source="tmux",
                status=status,
                last_output=output,
                metadata={"attached": attached, "created": created},
            )
        )
    return runs


def find_exact_run(target: str, runs: Iterable[OperatorRun]) -> Optional[OperatorRun]:
    """Find exactly one run by exact target. Ambiguous/missing targets refuse."""

    matches = [run for run in runs if run.target == target]
    if len(matches) != 1:
        return None
    return matches[0]


def resume_tmux_run(
    run: OperatorRun,
    action: OperatorAction | str,
    *,
    target: str,
) -> OperatorActionResult:
    """Send a narrow allowlisted action to a tmux target.

    The supplied target must exactly match ``run.target``. The action must be in
    ``RESUME_ALLOWED_ACTIONS``. The sent text is a deterministic mapping from
    action to literal input. Arbitrary text overrides are intentionally not
    supported for Slack button actions.
    """

    try:
        operator_action = action if isinstance(action, OperatorAction) else OperatorAction(action)
    except ValueError:
        return OperatorActionResult(OperatorActionOutcome.REFUSED, "Unknown operator action", target=target)

    if run.source != "tmux":
        return OperatorActionResult(OperatorActionOutcome.REFUSED, "Run is not a tmux target", operator_action, target)
    if target != run.target:
        return OperatorActionResult(OperatorActionOutcome.REFUSED, "Target does not exactly match run target", operator_action, target)
    if operator_action not in RESUME_ALLOWED_ACTIONS:
        return OperatorActionResult(OperatorActionOutcome.REFUSED, "Action is not resume-capable", operator_action, target)
    if not _tmux_exists():
        return OperatorActionResult(OperatorActionOutcome.SKIPPED, "tmux is not available", operator_action, target)

    send_text = RESUME_TEXT_BY_ACTION[operator_action]

    try:
        result = _run_tmux(["send-keys", "-t", target, send_text, "Enter"])
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        return OperatorActionResult(OperatorActionOutcome.ERROR, f"tmux send failed: {exc}", operator_action, target)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "tmux send failed").strip()[:200]
        return OperatorActionResult(OperatorActionOutcome.ERROR, detail, operator_action, target)
    return OperatorActionResult(OperatorActionOutcome.SENT, "Action sent to tmux", operator_action, target)
