import json

from sagents.utils.sandbox.runtime.protocol import (
    BackgroundStartResponse,
    CommandExecutionEvent,
    CommandRunResponse,
    decode_json_line,
    encode_json_line,
)


def test_command_run_response_round_trips_json_line():
    response = CommandRunResponse(
        success=True,
        stdout="hello\n",
        stderr="",
        exit_code=0,
        duration_ms=12,
        timeout=False,
    )

    line = encode_json_line(response)
    payload = decode_json_line(line, CommandRunResponse)

    assert line.endswith("\n")
    assert payload == response


def test_command_event_excludes_raw_command_by_default():
    event = CommandExecutionEvent(
        event="shell_execution_finished",
        sandbox_id="sandbox-1",
        session_id="session-1",
        command_id="cmd-1",
        command_hash="abc123",
        command_length=14,
        workdir="/sage-workspace",
        exit_code=0,
        duration_ms=25,
        stdout_bytes=5,
        stderr_bytes=0,
        timeout=False,
    )

    data = json.loads(encode_json_line(event))

    assert "command" not in data
    assert "command_redacted" not in data
    assert data["command_hash"] == "abc123"


def test_background_start_response_has_stable_fields():
    response = BackgroundStartResponse(
        task_id="shtask_abc",
        pid=123,
        log_path="/sage-workspace/.sage/bg/shtask_abc.log",
        exit_path="/sage-workspace/.sage/bg/shtask_abc.exit",
    )

    data = json.loads(encode_json_line(response))

    assert data == {
        "task_id": "shtask_abc",
        "pid": 123,
        "log_path": "/sage-workspace/.sage/bg/shtask_abc.log",
        "exit_path": "/sage-workspace/.sage/bg/shtask_abc.exit",
    }
