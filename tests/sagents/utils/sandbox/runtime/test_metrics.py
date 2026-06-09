import json
import os
from pathlib import Path
import subprocess
import sys

from sagents.utils.sandbox.runtime import agent
from sagents.utils.sandbox.runtime.metrics import (
    JsonLogReporter,
    command_hash,
    redact_command,
)
from sagents.utils.sandbox.runtime.protocol import CommandExecutionEvent


def test_command_hash_is_stable_and_does_not_reveal_command():
    digest = command_hash("echo secret-token")

    assert digest == command_hash("echo secret-token")
    assert "secret" not in digest
    assert len(digest) == 64


def test_redact_command_masks_common_secret_assignments():
    command = "TOKEN=abc password='hunter2' api_key=xyz echo ok"

    redacted = redact_command(command)

    assert "abc" not in redacted
    assert "hunter2" not in redacted
    assert "xyz" not in redacted
    assert "TOKEN=<redacted>" in redacted
    assert "password=<redacted>" in redacted
    assert "api_key=<redacted>" in redacted


def test_redact_command_masks_quoted_secret_values_with_whitespace():
    command = 'TOKEN="abc def" PASSWORD=\'hunter two\' echo ok'

    redacted = redact_command(command)

    assert "abc def" not in redacted
    assert "hunter two" not in redacted
    assert "TOKEN=<redacted>" in redacted
    assert "PASSWORD=<redacted>" in redacted


def test_redact_command_masks_secret_keys_with_digits():
    command = "KEY2=secret API_KEY_2=another SECRET123=third echo ok"

    redacted = redact_command(command)

    assert "secret" not in redacted
    assert "another" not in redacted
    assert "third" not in redacted
    assert "KEY2=<redacted>" in redacted
    assert "API_KEY_2=<redacted>" in redacted
    assert "SECRET123=<redacted>" in redacted


def test_redact_command_does_not_mask_unrelated_key_substrings():
    command = "monkey=banana MONKEY=banana KEYSTONE=value KEY=value"

    redacted = redact_command(command)

    assert "monkey=banana" in redacted
    assert "MONKEY=banana" in redacted
    assert "KEYSTONE=value" in redacted
    assert "KEY=<redacted>" in redacted


def test_redact_command_masks_concatenated_secret_env_names():
    command = "APIKEY=secret DBPASSWORD=hunter monkey=banana"

    redacted = redact_command(command)

    assert "secret" not in redacted
    assert "hunter" not in redacted
    assert "APIKEY=<redacted>" in redacted
    assert "DBPASSWORD=<redacted>" in redacted
    assert "monkey=banana" in redacted


def test_json_log_reporter_writes_one_event_per_line():
    writes = []
    reporter = JsonLogReporter(write_line=writes.append)
    event = CommandExecutionEvent(
        event="shell_execution_finished",
        sandbox_id="sandbox-1",
        session_id="session-1",
        command_id="cmd-1",
        command_hash="abc123",
        command_length=7,
        workdir="/sage-workspace",
        exit_code=0,
        duration_ms=5,
        stdout_bytes=2,
        stderr_bytes=0,
        timeout=False,
    )

    reporter.report(event)

    assert len(writes) == 1
    payload = json.loads(writes[0])
    assert payload["event"] == "shell_execution_finished"
    assert payload["sandbox_id"] == "sandbox-1"


def test_metric_agent_smoke_starts_and_exits_on_check_flag():
    result = subprocess.run(
        [sys.executable, "-m", "sagents.utils.sandbox.runtime.agent", "--check"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0
    assert "sage-sandbox-metric-agent ok" in result.stdout


def test_metric_agent_smoke_runs_from_outside_repo_with_pythonpath():
    repo_root = Path(__file__).resolve().parents[5]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    result = subprocess.run(
        [sys.executable, "-m", "sagents.utils.sandbox.runtime.agent", "--check"],
        cwd="/tmp",
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0
    assert "sage-sandbox-metric-agent ok" in result.stdout


def test_metric_agent_reap_children_drains_waitpid(monkeypatch):
    calls = []
    results = [(123, 0), (456, 0), (0, 0)]

    def waitpid(pid, options):
        calls.append((pid, options))
        return results.pop(0)

    monkeypatch.setattr(agent.os, "waitpid", waitpid)

    assert agent.reap_children() == 2
    assert calls == [
        (-1, agent.os.WNOHANG),
        (-1, agent.os.WNOHANG),
        (-1, agent.os.WNOHANG),
    ]


def test_metric_agent_reap_children_handles_no_children(monkeypatch):
    def waitpid(_pid, _options):
        raise ChildProcessError

    monkeypatch.setattr(agent.os, "waitpid", waitpid)

    assert agent.reap_children() == 0
