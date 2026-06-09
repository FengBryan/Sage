import base64
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace

import pytest


RUNNER = [sys.executable, "-m", "sagents.utils.sandbox.runtime.runner"]


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def run_runner(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        [*RUNNER, *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=full_env,
        check=False,
    )


def run_runner_bytes(*args: str, input_data: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*RUNNER, *args],
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def make_tar_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_run_command_returns_stdout_stderr_and_exit_code(tmp_path):
    result = run_runner(
        "run",
        "--command-b64",
        b64("python3 -c \"import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)\""),
        "--workdir",
        str(tmp_path),
        "--timeout",
        "5",
    )

    payload = json.loads(result.stdout)

    assert result.returncode == 0
    assert payload["success"] is False
    assert payload["stdout"] == "out\n"
    assert payload["stderr"] == "err\n"
    assert payload["exit_code"] == 3
    assert payload["timeout"] is False


def test_run_command_applies_env_and_workdir(tmp_path):
    result = run_runner(
        "run",
        "--command-b64",
        b64("python3 -c \"import os; print(os.getcwd()); print(os.environ['SAGE_X'])\""),
        "--workdir",
        str(tmp_path),
        "--timeout",
        "5",
        "--env-json-b64",
        b64(json.dumps({"SAGE_X": "42"})),
    )

    payload = json.loads(result.stdout)

    assert payload["success"] is True
    assert payload["stdout"] == f"{tmp_path}\n42\n"


def test_run_command_timeout_returns_structured_failure(tmp_path):
    result = run_runner(
        "run",
        "--command-b64",
        b64("python3 -c \"import time; time.sleep(2)\""),
        "--workdir",
        str(tmp_path),
        "--timeout",
        "1",
    )

    payload = json.loads(result.stdout)

    assert result.returncode == 0
    assert payload["success"] is False
    assert payload["timeout"] is True
    assert payload["exit_code"] == 124
    assert payload["error_type"] == "timeout"
    assert "timed out" in payload["error_message"]


def test_run_command_decodes_non_utf8_output_with_replacement(tmp_path):
    result = run_runner(
        "run",
        "--command-b64",
        b64("python3 -c \"import sys; sys.stdout.buffer.write(b'\\\\xff')\""),
        "--workdir",
        str(tmp_path),
        "--timeout",
        "5",
    )

    payload = json.loads(result.stdout)

    assert result.returncode == 0
    assert payload["success"] is True
    assert payload["stdout"] == "\ufffd"


def test_run_command_timeout_kills_child_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    command = (
        "python3 -c "
        f"\"import pathlib, time; pathlib.Path({str(pid_file)!r}).write_text(str(__import__('os').getpid())); time.sleep(30)\" "
        ">/dev/null 2>&1 & wait"
    )

    result = run_runner(
        "run",
        "--command-b64",
        b64(command),
        "--workdir",
        str(tmp_path),
        "--timeout",
        "1",
    )

    payload = json.loads(result.stdout)
    child_pid = int(pid_file.read_text(encoding="utf-8"))

    try:
        deadline = time.monotonic() + 2
        while _process_is_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        assert result.returncode == 0
        assert payload["timeout"] is True
        assert not _process_is_alive(child_pid)
    finally:
        if _process_is_alive(child_pid):
            os.kill(child_pid, 9)


def test_run_command_emits_event_with_redacted_command_when_requested(tmp_path):
    result = run_runner(
        "run",
        "--command-b64",
        b64("TOKEN=secret python3 -c \"print('ok')\""),
        "--workdir",
        str(tmp_path),
        "--timeout",
        "5",
        "--sandbox-id",
        "sandbox-1",
        "--session-id",
        "session-1",
        "--command-id",
        "command-1",
        "--log-command",
    )

    payload = json.loads(result.stdout)
    event = json.loads(result.stderr)

    assert payload["success"] is True
    assert event["event"] == "shell_execution_finished"
    assert event["sandbox_id"] == "sandbox-1"
    assert event["session_id"] == "session-1"
    assert event["command_id"] == "command-1"
    assert event["command_redacted"] == "TOKEN=<redacted> python3 -c \"print('ok')\""
    assert "secret" not in event["command_redacted"]


def test_run_command_emits_event_without_command_when_log_command_not_requested(tmp_path):
    result = run_runner(
        "run",
        "--command-b64",
        b64("TOKEN=secret python3 -c \"print('ok')\""),
        "--workdir",
        str(tmp_path),
        "--timeout",
        "5",
        "--sandbox-id",
        "sandbox-1",
        "--session-id",
        "session-1",
        "--command-id",
        "command-1",
    )

    payload = json.loads(result.stdout)
    event = json.loads(result.stderr)

    assert payload["success"] is True
    assert event["event"] == "shell_execution_finished"
    assert event["sandbox_id"] == "sandbox-1"
    assert event["session_id"] == "session-1"
    assert event["command_id"] == "command-1"
    assert "command_redacted" not in event


def test_run_command_event_counts_raw_output_bytes_for_non_utf8(tmp_path):
    result = run_runner(
        "run",
        "--command-b64",
        b64("python3 -c \"import sys; sys.stdout.buffer.write(b'\\\\xff'); sys.stderr.buffer.write(b'\\\\xfe\\\\xfd')\""),
        "--workdir",
        str(tmp_path),
        "--timeout",
        "5",
        "--sandbox-id",
        "sandbox-1",
        "--session-id",
        "session-1",
    )

    payload = json.loads(result.stdout)
    event = json.loads(result.stderr)

    assert payload["stdout"] == "\ufffd"
    assert payload["stderr"] == "\ufffd\ufffd"
    assert event["stdout_bytes"] == 1
    assert event["stderr_bytes"] == 2


def test_run_command_does_not_emit_event_without_sandbox_and_session(tmp_path):
    result = run_runner(
        "run",
        "--command-b64",
        b64("python3 -c \"print('ok')\""),
        "--workdir",
        str(tmp_path),
        "--timeout",
        "5",
    )

    assert result.returncode == 0
    assert result.stderr == ""


def test_run_command_does_not_emit_event_with_only_sandbox_id(tmp_path):
    result = run_runner(
        "run",
        "--command-b64",
        b64("python3 -c \"print('ok')\""),
        "--workdir",
        str(tmp_path),
        "--timeout",
        "5",
        "--sandbox-id",
        "sandbox-1",
    )

    assert result.returncode == 0
    assert result.stderr == ""


def test_run_command_does_not_emit_event_with_only_session_id(tmp_path):
    result = run_runner(
        "run",
        "--command-b64",
        b64("python3 -c \"print('ok')\""),
        "--workdir",
        str(tmp_path),
        "--timeout",
        "5",
        "--session-id",
        "session-1",
    )

    assert result.returncode == 0
    assert result.stderr == ""


def test_file_write_read_append_and_stat(tmp_path):
    target = tmp_path / "nested" / "note.txt"
    binary_target = tmp_path / "nested" / "blob.bin"

    write = run_runner(
        "file-write",
        "--path",
        str(target),
        "--mode",
        "overwrite",
        "--content-b64",
        b64("hello"),
    )
    append = run_runner(
        "file-write",
        "--path",
        str(target),
        "--mode",
        "append",
        "--content-b64",
        b64(" world"),
    )
    binary_write = run_runner(
        "file-write",
        "--path",
        str(binary_target),
        "--mode",
        "overwrite",
        "--content-b64",
        base64.b64encode(b"\x00\xff\x80").decode("ascii"),
    )
    read = run_runner("file-read", "--path", str(target))
    stat = run_runner("file-stat", "--path", str(target))
    binary_stat = run_runner("file-stat", "--path", str(binary_target))

    assert write.returncode == 0
    assert append.returncode == 0
    assert json.loads(read.stdout)["content"] == "hello world"
    stat_payload = json.loads(stat.stdout)
    assert stat_payload["is_file"] is True
    assert stat_payload["is_dir"] is False
    assert stat_payload["size"] == 11
    assert binary_write.returncode == 0
    assert binary_target.read_bytes() == b"\x00\xff\x80"
    assert json.loads(binary_stat.stdout)["size"] == 3


def test_file_list_delete_and_mkdir(tmp_path):
    hidden = tmp_path / ".hidden"
    visible = tmp_path / "visible.txt"
    nested_dir = tmp_path / "dir"
    nested_file = nested_dir / "child.txt"
    hidden.write_text("x", encoding="utf-8")
    visible.write_text("y", encoding="utf-8")
    nested_dir.mkdir()
    nested_file.write_text("z", encoding="utf-8")

    listed = run_runner("file-list", "--path", str(tmp_path))
    listed_hidden = run_runner("file-list", "--path", str(tmp_path), "--include-hidden")
    mkdir = run_runner("file-mkdir", "--path", str(tmp_path / "created" / "deep"))
    delete_file = run_runner("file-delete", "--path", str(visible))
    delete_dir = run_runner("file-delete", "--path", str(nested_dir))

    entries = json.loads(listed.stdout)["entries"]
    names = [item["name"] for item in entries]
    hidden_names = {item["name"] for item in json.loads(listed_hidden.stdout)["entries"]}

    assert names == ["dir", "visible.txt"]
    assert entries[0]["is_dir"] is True
    assert entries[1]["is_file"] is True
    assert hidden_names == {".hidden", "dir", "visible.txt"}
    assert mkdir.returncode == 0
    assert (tmp_path / "created" / "deep").is_dir()
    assert delete_file.returncode == 0
    assert not visible.exists()
    assert delete_dir.returncode == 0
    assert not nested_dir.exists()


def test_tar_extract_extracts_file_and_directory_entries(tmp_path):
    tar_bytes = make_tar_bytes(
        {
            "note.txt": b"hello",
            "nested/blob.bin": b"\x00\xffbinary",
        }
    )

    result = run_runner_bytes("tar-extract", "--dest", str(tmp_path), input_data=tar_bytes)

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"ok": True}
    assert (tmp_path / "note.txt").read_bytes() == b"hello"
    assert (tmp_path / "nested" / "blob.bin").read_bytes() == b"\x00\xffbinary"


def test_tar_extract_with_size_reads_exact_bytes_without_waiting_for_eof(monkeypatch, tmp_path):
    from sagents.utils.sandbox.runtime import runner

    tar_bytes = make_tar_bytes({"note.txt": b"hello"})

    class FixedSizeOnlyBuffer:
        def read(self, size=-1):
            if size < 0:
                raise AssertionError("tar_extract should not wait for stdin EOF")
            return tar_bytes[:size]

    monkeypatch.setattr(
        runner.sys,
        "stdin",
        SimpleNamespace(buffer=FixedSizeOnlyBuffer()),
    )

    result = runner.tar_extract(
        SimpleNamespace(dest=str(tmp_path), size_bytes=len(tar_bytes))
    )

    assert result == 0
    assert (tmp_path / "note.txt").read_bytes() == b"hello"


def test_tar_extract_with_size_rejects_short_stdin(tmp_path):
    tar_bytes = make_tar_bytes({"note.txt": b"hello"})

    result = run_runner_bytes(
        "tar-extract",
        "--dest",
        str(tmp_path),
        "--size-bytes",
        str(len(tar_bytes) + 1),
        input_data=tar_bytes,
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["error_type"] == "invalid_tar"
    assert "Expected" in payload["error_message"]
    assert not (tmp_path / "note.txt").exists()


@pytest.mark.parametrize(
    "member_name",
    ["../escape.txt", "/tmp/escape.txt", "safe/../../escape.txt"],
)
def test_tar_extract_rejects_path_traversal_entries(tmp_path, member_name):
    outside = tmp_path.parent / "escape.txt"
    tar_bytes = make_tar_bytes({member_name: b"secret"})

    result = run_runner_bytes("tar-extract", "--dest", str(tmp_path), input_data=tar_bytes)

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["error_type"] == "invalid_tar_path"
    assert not outside.exists()


def test_file_delete_unlinks_symlink_to_directory_without_deleting_target(tmp_path):
    target_dir = tmp_path / "target"
    target_file = target_dir / "kept.txt"
    symlink = tmp_path / "target-link"
    target_dir.mkdir()
    target_file.write_text("still here", encoding="utf-8")
    symlink.symlink_to(target_dir, target_is_directory=True)

    delete = run_runner("file-delete", "--path", str(symlink))

    assert delete.returncode == 0
    assert not symlink.exists()
    assert target_dir.is_dir()
    assert target_file.read_text(encoding="utf-8") == "still here"


def test_file_list_classifies_symlinks_without_crashing_on_broken_symlink(tmp_path):
    target_dir = tmp_path / "target-dir"
    target_file = tmp_path / "target.txt"
    dir_link = tmp_path / "dir-link"
    file_link = tmp_path / "file-link"
    broken = tmp_path / "missing-link"
    target_dir.mkdir()
    target_file.write_text("target", encoding="utf-8")
    dir_link.symlink_to(target_dir, target_is_directory=True)
    file_link.symlink_to(target_file)
    broken.symlink_to(tmp_path / "missing-target")

    listed = run_runner("file-list", "--path", str(tmp_path))

    assert listed.returncode == 0
    entries = {item["name"]: item for item in json.loads(listed.stdout)["entries"]}
    assert entries["dir-link"]["is_dir"] is True
    assert entries["dir-link"]["is_file"] is False
    assert entries["file-link"]["is_file"] is True
    assert entries["file-link"]["is_dir"] is False
    assert entries["missing-link"]["is_file"] is False
    assert entries["missing-link"]["is_dir"] is False


def test_background_start_read_exit_and_size_lifecycle(tmp_path):
    bg_dir = tmp_path / ".sage" / "bg"
    start = run_runner(
        "bg-start",
        "--command-b64",
        b64("python3 -c \"import sys; print('background done'); sys.stderr.buffer.write(b'\\\\xff')\""),
        "--workdir",
        str(tmp_path),
        "--bg-dir",
        str(bg_dir),
    )

    assert start.returncode == 0
    start_payload = json.loads(start.stdout)
    task_id = start_payload["task_id"]
    assert start_payload["pid"] > 0
    assert start_payload["log_path"] == str(bg_dir / f"{task_id}.log")
    assert start_payload["exit_path"] == str(bg_dir / f"{task_id}.exit")
    assert (bg_dir / f"{task_id}.pid").read_text(encoding="utf-8") == str(start_payload["pid"])

    state = _wait_for_background_exit(task_id, bg_dir)
    read = json.loads(
        run_runner("bg-read", "--task-id", task_id, "--bg-dir", str(bg_dir)).stdout
    )
    size = json.loads(
        run_runner("bg-size", "--task-id", task_id, "--bg-dir", str(bg_dir)).stdout
    )

    assert state["alive"] is False
    assert state["exit_code"] == 0
    assert "background done" in read["text"]
    assert "\ufffd" in read["text"]
    assert read["size"] == size["size"]
    assert size["size"] >= len("background done\n".encode("utf-8")) + 1


def test_background_read_range_and_kill_process_group(tmp_path):
    bg_dir = tmp_path / ".sage" / "bg"
    child_pid_file = tmp_path / "child.pid"
    command = (
        "python3 -c "
        f"\"import os, pathlib, subprocess, sys, time; "
        f"pathlib.Path({str(child_pid_file)!r}).write_text('', encoding='utf-8'); "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid), encoding='utf-8'); "
        "print('ready', flush=True); time.sleep(30)\""
    )
    start = run_runner(
        "bg-start",
        "--command-b64",
        b64(command),
        "--workdir",
        str(tmp_path),
        "--bg-dir",
        str(bg_dir),
    )

    assert start.returncode == 0
    task_id = json.loads(start.stdout)["task_id"]
    try:
        _wait_for_file_text(child_pid_file, timeout=3)
        _wait_for_log_text(task_id, bg_dir, "ready", timeout=3)
        ranged = json.loads(
            run_runner(
                "bg-read-range",
                "--task-id",
                task_id,
                "--bg-dir",
                str(bg_dir),
                "--offset",
                "0",
                "--max-bytes",
                "1024",
            ).stdout
        )
        killed = json.loads(
            run_runner("bg-kill", "--task-id", task_id, "--bg-dir", str(bg_dir), "--force").stdout
        )
        state = _wait_for_background_exit(task_id, bg_dir)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))

        deadline = time.monotonic() + 3
        while _process_is_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        assert "ready" in ranged["text"]
        assert ranged["offset"] > 0
        assert ranged["size"] >= ranged["offset"]
        assert killed["ok"] is True
        assert state["alive"] is False
        assert state["exit_code"] is not None
        assert not _process_is_alive(child_pid)
    finally:
        state = json.loads(
            run_runner("bg-state", "--task-id", task_id, "--bg-dir", str(bg_dir)).stdout
        )
        if state["alive"]:
            run_runner("bg-kill", "--task-id", task_id, "--bg-dir", str(bg_dir), "--force")


def test_background_rejects_invalid_task_id_without_path_traversal(tmp_path):
    bg_dir = tmp_path / ".sage" / "bg"
    bg_dir.mkdir(parents=True)
    outside_log = tmp_path / "escape.log"
    outside_log.write_text("outside secret", encoding="utf-8")

    result = run_runner("bg-read", "--task-id", "../escape", "--bg-dir", str(bg_dir))

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["error_type"] == "invalid_task_id"
    assert "outside secret" not in result.stdout


def test_background_read_max_bytes_zero_returns_empty_text_with_size(tmp_path):
    bg_dir = tmp_path / ".sage" / "bg"
    task_id = "shtask_manual"
    bg_dir.mkdir(parents=True)
    (bg_dir / f"{task_id}.log").write_text("hello background", encoding="utf-8")

    read = run_runner(
        "bg-read",
        "--task-id",
        task_id,
        "--bg-dir",
        str(bg_dir),
        "--max-bytes",
        "0",
    )

    payload = json.loads(read.stdout)
    assert read.returncode == 0
    assert payload == {"text": "", "size": len("hello background".encode("utf-8"))}


def test_background_sigterm_does_not_report_exit_before_ignored_process_stops(tmp_path):
    bg_dir = tmp_path / ".sage" / "bg"
    child_pid_file = tmp_path / "ignored.pid"
    command = (
        "python3 -c "
        f"\"import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        "print('ready', flush=True); time.sleep(30)\""
    )
    start = run_runner(
        "bg-start",
        "--command-b64",
        b64(command),
        "--workdir",
        str(tmp_path),
        "--bg-dir",
        str(bg_dir),
    )

    assert start.returncode == 0
    start_payload = json.loads(start.stdout)
    task_id = start_payload["task_id"]
    exit_path = bg_dir / f"{task_id}.exit"
    try:
        _wait_for_file_text(child_pid_file, timeout=3)
        _wait_for_log_text(task_id, bg_dir, "ready", timeout=3)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))

        killed = json.loads(
            run_runner("bg-kill", "--task-id", task_id, "--bg-dir", str(bg_dir)).stdout
        )
        state = json.loads(
            run_runner("bg-state", "--task-id", task_id, "--bg-dir", str(bg_dir)).stdout
        )

        assert killed["ok"] is True
        assert _process_is_alive(child_pid)
        assert not exit_path.exists()
        assert state["alive"] is True
        assert state["exit_code"] is None
    finally:
        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            if _process_is_alive(child_pid):
                run_runner("bg-kill", "--task-id", task_id, "--bg-dir", str(bg_dir), "--force")
                deadline = time.monotonic() + 3
                while _process_is_alive(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if _process_is_alive(child_pid):
                    os.kill(child_pid, signal.SIGKILL)


def _wait_for_background_exit(task_id, bg_dir):
    deadline = time.monotonic() + 5
    last_state = None
    while time.monotonic() < deadline:
        last_state = json.loads(
            run_runner("bg-state", "--task-id", task_id, "--bg-dir", str(bg_dir)).stdout
        )
        if last_state["exit_code"] is not None:
            return last_state
        time.sleep(0.05)
    raise AssertionError(f"background task did not exit; last state={last_state}")


def _wait_for_file_text(path, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text:
                return text
        time.sleep(0.05)
    raise AssertionError(f"{path} was not populated")


def _wait_for_log_text(task_id, bg_dir, expected, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        read = json.loads(
            run_runner("bg-read", "--task-id", task_id, "--bg-dir", str(bg_dir)).stdout
        )
        if expected in read["text"]:
            return read
        time.sleep(0.05)
    raise AssertionError(f"{expected!r} was not written to background log")


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    proc_stat = f"/proc/{pid}/stat"
    if os.path.exists(proc_stat):
        try:
            with open(proc_stat, encoding="utf-8") as file:
                state = file.read().split()[2]
        except OSError:
            return False
        if state == "Z":
            return False
    return True
