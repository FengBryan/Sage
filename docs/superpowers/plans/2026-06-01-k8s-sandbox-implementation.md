# Kubernetes Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-ready baseline Kubernetes sandbox provider that runs in-cluster, reuses Pods, binds an existing PVC workspace, executes through a project-maintained sandbox runtime image, emits JSON metric events, and supports file and background shell APIs.

**Architecture:** The Sage control plane remains in `KubernetesSandboxProvider`, which manages Pod lifecycle and calls Kubernetes exec. The sandbox container runs `sage-sandbox-metric-agent` as its main process and exposes `sage-shell-runner` as the command/file/background entrypoint invoked by exec. Runtime code is kept in a small Python package that can be copied into the sandbox image and unit-tested outside Kubernetes.

**Tech Stack:** Python 3.11+, Kubernetes Python client, pytest, stdlib `subprocess`, `tarfile`, `json`, `base64`, Unix domain sockets, existing Sage `ISandboxHandle` interfaces.

---

## File Structure

- Create `sagents/utils/sandbox/runtime/__init__.py`: package marker and public runtime version.
- Create `sagents/utils/sandbox/runtime/protocol.py`: dataclasses and JSON helpers shared by runner, agent, and provider.
- Create `sagents/utils/sandbox/runtime/metrics.py`: reporter interface, JSON log reporter, redaction, Unix socket metric client/server helpers.
- Create `sagents/utils/sandbox/runtime/runner.py`: CLI implementation for `run`, `file-*`, and `bg-*` subcommands.
- Create `sagents/utils/sandbox/runtime/agent.py`: metric agent main loop and socket server.
- Create `deploy/images/Dockerfile.sandbox`: project-maintained sandbox runtime image.
- Modify `sagents/utils/sandbox/providers/remote/kubernetes.py`: replace skeleton implementation with config parsing, Pod/PVC lifecycle, runner exec, tar copy, file APIs, background APIs.
- Modify `sagents/utils/sandbox/factory.py`: pass Kubernetes provider config fields without duplicating `namespace` and `resources`.
- Create `tests/sagents/utils/sandbox/runtime/test_protocol.py`: protocol serialization tests.
- Create `tests/sagents/utils/sandbox/runtime/test_metrics.py`: metric logging and redaction tests.
- Create `tests/sagents/utils/sandbox/runtime/test_runner.py`: command/file/background runner tests.
- Create `tests/sagents/utils/sandbox/test_kubernetes_provider.py`: mocked Kubernetes provider tests.
- Modify or create docs under `docs/en/architecture/ARCHITECTURE_SAGENTS_SANDBOX_OBS.md` only if implementation changes the public architecture description.

## Task 1: Runtime Protocol

**Files:**
- Create: `sagents/utils/sandbox/runtime/__init__.py`
- Create: `sagents/utils/sandbox/runtime/protocol.py`
- Test: `tests/sagents/utils/sandbox/runtime/test_protocol.py`

- [ ] **Step 1: Write failing protocol tests**

Create `tests/sagents/utils/sandbox/runtime/test_protocol.py`:

```python
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
```

- [ ] **Step 2: Run protocol tests and verify they fail**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_protocol.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'sagents.utils.sandbox.runtime'`.

- [ ] **Step 3: Implement runtime protocol dataclasses**

Create `sagents/utils/sandbox/runtime/__init__.py`:

```python
"""Sandbox runtime helpers packaged into Kubernetes sandbox images."""

RUNTIME_VERSION = "0.1.0"
```

Create `sagents/utils/sandbox/runtime/protocol.py`:

```python
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import Any, Type, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class CommandRunResponse:
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timeout: bool
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class CommandExecutionEvent:
    event: str
    sandbox_id: str
    session_id: str
    command_id: str
    command_hash: str
    command_length: int
    workdir: str
    exit_code: int
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int
    timeout: bool
    command_redacted: str | None = None


@dataclass(frozen=True)
class BackgroundStartResponse:
    task_id: str
    pid: int
    log_path: str
    exit_path: str


@dataclass(frozen=True)
class BackgroundReadResponse:
    text: str
    offset: int | None = None
    size: int | None = None


@dataclass(frozen=True)
class BackgroundStateResponse:
    alive: bool
    exit_code: int | None = None


def to_payload(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return {key: item for key, item in asdict(value).items() if item is not None}
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items() if item is not None}
    raise TypeError(f"Cannot encode value of type {type(value).__name__}")


def encode_json_line(value: Any) -> str:
    return json.dumps(to_payload(value), ensure_ascii=False, separators=(",", ":")) + "\n"


def decode_json_line(line: str, cls: Type[T]) -> T:
    data = json.loads(line)
    allowed = {field.name for field in fields(cls)}
    filtered = {key: value for key, value in data.items() if key in allowed}
    return cls(**filtered)
```

- [ ] **Step 4: Run protocol tests and verify they pass**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_protocol.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit protocol task**

Run:

```bash
git add sagents/utils/sandbox/runtime/__init__.py sagents/utils/sandbox/runtime/protocol.py tests/sagents/utils/sandbox/runtime/test_protocol.py
git commit -m "feat: add sandbox runtime protocol"
```

## Task 2: Metric Reporter And Redaction

**Files:**
- Create: `sagents/utils/sandbox/runtime/metrics.py`
- Test: `tests/sagents/utils/sandbox/runtime/test_metrics.py`

- [ ] **Step 1: Write failing metric tests**

Create `tests/sagents/utils/sandbox/runtime/test_metrics.py`:

```python
import json

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
```

- [ ] **Step 2: Run metric tests and verify they fail**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_metrics.py -v
```

Expected: FAIL with `ModuleNotFoundError` or missing `metrics` symbols.

- [ ] **Step 3: Implement reporter and redaction helpers**

Create `sagents/utils/sandbox/runtime/metrics.py`:

```python
from __future__ import annotations

import hashlib
import re
import sys
from typing import Callable, Protocol

from .protocol import CommandExecutionEvent, encode_json_line


class Reporter(Protocol):
    def report(self, event: CommandExecutionEvent) -> None:
        ...


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<key>[A-Za-z_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)[A-Za-z_]*)"
    r"\s*=\s*"
    r"(?P<quote>['\"]?)[^'\"\s]+(?P=quote)",
    re.IGNORECASE,
)


def command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", errors="ignore")).hexdigest()


def redact_command(command: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return f"{match.group('key')}=<redacted>"

    return _SECRET_ASSIGNMENT_RE.sub(replace, command)


class JsonLogReporter:
    def __init__(self, write_line: Callable[[str], None] | None = None) -> None:
        self._write_line = write_line or self._default_write_line

    def report(self, event: CommandExecutionEvent) -> None:
        self._write_line(encode_json_line(event).rstrip("\n"))

    @staticmethod
    def _default_write_line(line: str) -> None:
        print(line, file=sys.stdout, flush=True)
```

- [ ] **Step 4: Run metric tests and verify they pass**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_metrics.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit metric reporter task**

Run:

```bash
git add sagents/utils/sandbox/runtime/metrics.py tests/sagents/utils/sandbox/runtime/test_metrics.py
git commit -m "feat: add sandbox runtime metrics reporter"
```

## Task 3: Shell Runner Synchronous Commands

**Files:**
- Modify: `sagents/utils/sandbox/runtime/runner.py`
- Test: `tests/sagents/utils/sandbox/runtime/test_runner.py`

- [ ] **Step 1: Write failing runner command tests**

Create `tests/sagents/utils/sandbox/runtime/test_runner.py` with the first tests:

```python
import base64
import json
import os
import subprocess
import sys


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


def test_run_command_returns_stdout_stderr_and_exit_code(tmp_path):
    result = run_runner(
        "run",
        "--command-b64",
        b64("python -c \"import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)\""),
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
        b64("python -c \"import os; print(os.getcwd()); print(os.environ['SAGE_X'])\""),
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
        b64("python -c \"import time; time.sleep(2)\""),
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
```

- [ ] **Step 2: Run runner command tests and verify they fail**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_runner.py -v
```

Expected: FAIL because `sagents.utils.sandbox.runtime.runner` does not exist.

- [ ] **Step 3: Implement `sage-shell-runner run`**

Create `sagents/utils/sandbox/runtime/runner.py`:

```python
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .metrics import command_hash, redact_command
from .protocol import CommandExecutionEvent, CommandRunResponse, encode_json_line


TIMEOUT_EXIT_CODE = 124


def _decode_text(value: str | None, default: str = "") -> str:
    if not value:
        return default
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def _decode_env(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    payload = json.loads(_decode_text(value))
    return {str(key): str(item) for key, item in payload.items()}


def _emit_metric_event(args: argparse.Namespace, command: str, response: CommandRunResponse) -> None:
    if not getattr(args, "sandbox_id", None) or not getattr(args, "session_id", None):
        return
    event = CommandExecutionEvent(
        event="shell_execution_finished",
        sandbox_id=args.sandbox_id,
        session_id=args.session_id,
        command_id=getattr(args, "command_id", None) or uuid.uuid4().hex,
        command_hash=command_hash(command),
        command_length=len(command),
        workdir=args.workdir,
        exit_code=response.exit_code,
        duration_ms=response.duration_ms,
        stdout_bytes=len(response.stdout.encode("utf-8", errors="ignore")),
        stderr_bytes=len(response.stderr.encode("utf-8", errors="ignore")),
        timeout=response.timeout,
        command_redacted=redact_command(command) if getattr(args, "log_command", False) else None,
    )
    print(encode_json_line(event).rstrip("\n"), file=sys.stderr, flush=True)


def run_command(args: argparse.Namespace) -> int:
    command = _decode_text(args.command_b64)
    env = os.environ.copy()
    env.update(_decode_env(args.env_json_b64))
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=args.workdir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout,
            check=False,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        response = CommandRunResponse(
            success=completed.returncode == 0,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            duration_ms=duration_ms,
            timeout=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        response = CommandRunResponse(
            success=False,
            stdout=stdout if isinstance(stdout, str) else stdout.decode("utf-8", errors="ignore"),
            stderr=stderr if isinstance(stderr, str) else stderr.decode("utf-8", errors="ignore"),
            exit_code=TIMEOUT_EXIT_CODE,
            duration_ms=duration_ms,
            timeout=True,
            error_type="timeout",
            error_message=f"Command timed out after {args.timeout} seconds",
        )
    _emit_metric_event(args, command, response)
    sys.stdout.write(encode_json_line(response))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sage-shell-runner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--command-b64", required=True)
    run.add_argument("--workdir", required=True)
    run.add_argument("--timeout", type=int, required=True)
    run.add_argument("--env-json-b64")
    run.add_argument("--sandbox-id")
    run.add_argument("--session-id")
    run.add_argument("--command-id")
    run.add_argument("--log-command", action="store_true")
    run.set_defaults(func=run_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run runner command tests and verify they pass**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_runner.py -v
```

Expected: PASS for the three command tests.

- [ ] **Step 5: Commit runner command task**

Run:

```bash
git add sagents/utils/sandbox/runtime/runner.py tests/sagents/utils/sandbox/runtime/test_runner.py
git commit -m "feat: add sandbox shell runner command execution"
```

## Task 4: Runner File Subcommands

**Files:**
- Modify: `sagents/utils/sandbox/runtime/runner.py`
- Modify: `tests/sagents/utils/sandbox/runtime/test_runner.py`

- [ ] **Step 1: Add failing file subcommand tests**

Append to `tests/sagents/utils/sandbox/runtime/test_runner.py`:

```python
def test_file_write_read_append_and_stat(tmp_path):
    target = tmp_path / "nested" / "note.txt"

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
    read = run_runner("file-read", "--path", str(target))
    stat = run_runner("file-stat", "--path", str(target))

    assert write.returncode == 0
    assert append.returncode == 0
    assert json.loads(read.stdout)["content"] == "hello world"
    stat_payload = json.loads(stat.stdout)
    assert stat_payload["is_file"] is True
    assert stat_payload["is_dir"] is False
    assert stat_payload["size"] == 11


def test_file_list_delete_and_mkdir(tmp_path):
    hidden = tmp_path / ".hidden"
    visible = tmp_path / "visible.txt"
    hidden.write_text("x", encoding="utf-8")
    visible.write_text("y", encoding="utf-8")

    listed = run_runner("file-list", "--path", str(tmp_path))
    listed_hidden = run_runner("file-list", "--path", str(tmp_path), "--include-hidden")
    mkdir = run_runner("file-mkdir", "--path", str(tmp_path / "created"))
    delete = run_runner("file-delete", "--path", str(visible))

    names = {item["name"] for item in json.loads(listed.stdout)["entries"]}
    hidden_names = {item["name"] for item in json.loads(listed_hidden.stdout)["entries"]}

    assert names == {"visible.txt"}
    assert hidden_names == {".hidden", "visible.txt"}
    assert mkdir.returncode == 0
    assert (tmp_path / "created").is_dir()
    assert delete.returncode == 0
    assert not visible.exists()
```

- [ ] **Step 2: Run file tests and verify they fail**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_runner.py::test_file_write_read_append_and_stat tests/sagents/utils/sandbox/runtime/test_runner.py::test_file_list_delete_and_mkdir -v
```

Expected: FAIL with argparse invalid choice errors for `file-*`.

- [ ] **Step 3: Implement file subcommands**

Add these functions to `sagents/utils/sandbox/runtime/runner.py` before `build_parser()`:

```python
def _file_info(path: Path) -> dict[str, object]:
    st = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "size": st.st_size,
        "modified_time": st.st_mtime,
    }


def file_read(args: argparse.Namespace) -> int:
    path = Path(args.path)
    content = path.read_text(encoding=args.encoding)
    sys.stdout.write(json.dumps({"content": content}, ensure_ascii=False) + "\n")
    return 0


def file_write(args: argparse.Namespace) -> int:
    path = Path(args.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(args.content_b64.encode("ascii"))
    mode = "ab" if args.mode == "append" else "wb"
    with path.open(mode) as f:
        f.write(data)
    sys.stdout.write(json.dumps({"ok": True}) + "\n")
    return 0


def file_list(args: argparse.Namespace) -> int:
    path = Path(args.path)
    entries = []
    if path.is_dir():
        for child in path.iterdir():
            if child.name.startswith(".") and not args.include_hidden:
                continue
            entries.append(_file_info(child))
    entries.sort(key=lambda item: (not item["is_dir"], str(item["name"])))
    sys.stdout.write(json.dumps({"entries": entries}, ensure_ascii=False) + "\n")
    return 0


def file_stat(args: argparse.Namespace) -> int:
    path = Path(args.path)
    sys.stdout.write(json.dumps(_file_info(path), ensure_ascii=False) + "\n")
    return 0


def file_delete(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.is_dir():
        import shutil

        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    sys.stdout.write(json.dumps({"ok": True}) + "\n")
    return 0


def file_mkdir(args: argparse.Namespace) -> int:
    Path(args.path).mkdir(parents=True, exist_ok=True)
    sys.stdout.write(json.dumps({"ok": True}) + "\n")
    return 0
```

Add these parsers inside `build_parser()` before `return parser`:

```python
    read = sub.add_parser("file-read")
    read.add_argument("--path", required=True)
    read.add_argument("--encoding", default="utf-8")
    read.set_defaults(func=file_read)

    write = sub.add_parser("file-write")
    write.add_argument("--path", required=True)
    write.add_argument("--mode", choices=["overwrite", "append"], required=True)
    write.add_argument("--content-b64", required=True)
    write.set_defaults(func=file_write)

    list_parser = sub.add_parser("file-list")
    list_parser.add_argument("--path", required=True)
    list_parser.add_argument("--include-hidden", action="store_true")
    list_parser.set_defaults(func=file_list)

    stat = sub.add_parser("file-stat")
    stat.add_argument("--path", required=True)
    stat.set_defaults(func=file_stat)

    delete = sub.add_parser("file-delete")
    delete.add_argument("--path", required=True)
    delete.set_defaults(func=file_delete)

    mkdir = sub.add_parser("file-mkdir")
    mkdir.add_argument("--path", required=True)
    mkdir.set_defaults(func=file_mkdir)
```

- [ ] **Step 4: Run runner tests and verify they pass**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_runner.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit file subcommands**

Run:

```bash
git add sagents/utils/sandbox/runtime/runner.py tests/sagents/utils/sandbox/runtime/test_runner.py
git commit -m "feat: add sandbox runner file operations"
```

## Task 5: Runner Background Subcommands

**Files:**
- Modify: `sagents/utils/sandbox/runtime/runner.py`
- Modify: `tests/sagents/utils/sandbox/runtime/test_runner.py`

- [ ] **Step 1: Add failing background tests**

Append to `tests/sagents/utils/sandbox/runtime/test_runner.py`:

```python
import time


def test_background_start_read_exit_and_cleanup(tmp_path):
    bg_dir = tmp_path / ".sage" / "bg"
    start = run_runner(
        "bg-start",
        "--command-b64",
        b64("python -c \"print('background done')\""),
        "--workdir",
        str(tmp_path),
        "--bg-dir",
        str(bg_dir),
    )
    start_payload = json.loads(start.stdout)
    task_id = start_payload["task_id"]

    for _ in range(30):
        state = json.loads(
            run_runner("bg-state", "--task-id", task_id, "--bg-dir", str(bg_dir)).stdout
        )
        if state["exit_code"] is not None:
            break
        time.sleep(0.1)

    read = json.loads(
        run_runner("bg-read", "--task-id", task_id, "--bg-dir", str(bg_dir)).stdout
    )
    size = json.loads(
        run_runner("bg-size", "--task-id", task_id, "--bg-dir", str(bg_dir)).stdout
    )

    assert state["alive"] is False
    assert state["exit_code"] == 0
    assert "background done" in read["text"]
    assert size["size"] >= len("background done\n")


def test_background_range_and_kill(tmp_path):
    bg_dir = tmp_path / ".sage" / "bg"
    start = run_runner(
        "bg-start",
        "--command-b64",
        b64("python -c \"import time; print('ready', flush=True); time.sleep(30)\""),
        "--workdir",
        str(tmp_path),
        "--bg-dir",
        str(bg_dir),
    )
    task_id = json.loads(start.stdout)["task_id"]

    time.sleep(0.5)
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

    assert "ready" in ranged["text"]
    assert ranged["offset"] > 0
    assert killed["ok"] is True
```

- [ ] **Step 2: Run background tests and verify they fail**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_runner.py::test_background_start_read_exit_and_cleanup tests/sagents/utils/sandbox/runtime/test_runner.py::test_background_range_and_kill -v
```

Expected: FAIL with argparse invalid choice errors for `bg-*`.

- [ ] **Step 3: Implement background subcommands**

Add these imports to `sagents/utils/sandbox/runtime/runner.py`:

```python
import signal
```

Add these helpers before `build_parser()`:

```python
def _bg_dir(args: argparse.Namespace) -> Path:
    path = Path(args.bg_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _task_paths(bg_dir: Path, task_id: str) -> tuple[Path, Path, Path]:
    return (
        bg_dir / f"{task_id}.pid",
        bg_dir / f"{task_id}.log",
        bg_dir / f"{task_id}.exit",
    )


def bg_start(args: argparse.Namespace) -> int:
    bg_dir = _bg_dir(args)
    task_id = "shtask_" + uuid.uuid4().hex[:12]
    pid_path, log_path, exit_path = _task_paths(bg_dir, task_id)
    command = _decode_text(args.command_b64)
    launcher = (
        "import pathlib, subprocess, sys;"
        "cmd=sys.argv[1]; cwd=sys.argv[2]; log=sys.argv[3]; exitp=sys.argv[4];"
        "pathlib.Path(log).parent.mkdir(parents=True, exist_ok=True);"
        "f=open(log, 'ab', buffering=0);"
        "p=subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=f, stderr=subprocess.STDOUT);"
        "rc=p.wait(); pathlib.Path(exitp).write_text(str(rc), encoding='utf-8'); f.close()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", launcher, command, args.workdir, str(log_path), str(exit_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_path.write_text(str(process.pid), encoding="utf-8")
    sys.stdout.write(
        json.dumps(
            {
                "task_id": task_id,
                "pid": process.pid,
                "log_path": str(log_path),
                "exit_path": str(exit_path),
            }
        )
        + "\n"
    )
    return 0


def _read_pid(pid_path: Path) -> int | None:
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def bg_state(args: argparse.Namespace) -> int:
    pid_path, _, exit_path = _task_paths(_bg_dir(args), args.task_id)
    exit_code = None
    if exit_path.exists():
        exit_code = int(exit_path.read_text(encoding="utf-8").strip())
    sys.stdout.write(json.dumps({"alive": _alive(_read_pid(pid_path)) and exit_code is None, "exit_code": exit_code}) + "\n")
    return 0


def bg_read(args: argparse.Namespace) -> int:
    _, log_path, _ = _task_paths(_bg_dir(args), args.task_id)
    data = log_path.read_bytes() if log_path.exists() else b""
    tail = data[-args.max_bytes :]
    sys.stdout.write(json.dumps({"text": tail.decode("utf-8", errors="ignore"), "size": len(data)}) + "\n")
    return 0


def bg_read_range(args: argparse.Namespace) -> int:
    _, log_path, _ = _task_paths(_bg_dir(args), args.task_id)
    data = log_path.read_bytes() if log_path.exists() else b""
    start = min(args.offset, len(data))
    end = min(start + args.max_bytes, len(data))
    chunk = data[start:end]
    sys.stdout.write(json.dumps({"text": chunk.decode("utf-8", errors="ignore"), "offset": end, "size": len(data)}) + "\n")
    return 0


def bg_size(args: argparse.Namespace) -> int:
    _, log_path, _ = _task_paths(_bg_dir(args), args.task_id)
    sys.stdout.write(json.dumps({"size": log_path.stat().st_size if log_path.exists() else 0}) + "\n")
    return 0


def bg_kill(args: argparse.Namespace) -> int:
    pid_path, _, _ = _task_paths(_bg_dir(args), args.task_id)
    pid = _read_pid(pid_path)
    ok = False
    if pid:
        try:
            os.killpg(pid, signal.SIGKILL if args.force else signal.SIGTERM)
            ok = True
        except OSError:
            ok = False
    sys.stdout.write(json.dumps({"ok": ok}) + "\n")
    return 0
```

Add these parsers inside `build_parser()`:

```python
    bg_start_parser = sub.add_parser("bg-start")
    bg_start_parser.add_argument("--command-b64", required=True)
    bg_start_parser.add_argument("--workdir", required=True)
    bg_start_parser.add_argument("--bg-dir", required=True)
    bg_start_parser.set_defaults(func=bg_start)

    bg_state_parser = sub.add_parser("bg-state")
    bg_state_parser.add_argument("--task-id", required=True)
    bg_state_parser.add_argument("--bg-dir", required=True)
    bg_state_parser.set_defaults(func=bg_state)

    bg_read_parser = sub.add_parser("bg-read")
    bg_read_parser.add_argument("--task-id", required=True)
    bg_read_parser.add_argument("--bg-dir", required=True)
    bg_read_parser.add_argument("--max-bytes", type=int, default=8192)
    bg_read_parser.set_defaults(func=bg_read)

    bg_range_parser = sub.add_parser("bg-read-range")
    bg_range_parser.add_argument("--task-id", required=True)
    bg_range_parser.add_argument("--bg-dir", required=True)
    bg_range_parser.add_argument("--offset", type=int, required=True)
    bg_range_parser.add_argument("--max-bytes", type=int, required=True)
    bg_range_parser.set_defaults(func=bg_read_range)

    bg_size_parser = sub.add_parser("bg-size")
    bg_size_parser.add_argument("--task-id", required=True)
    bg_size_parser.add_argument("--bg-dir", required=True)
    bg_size_parser.set_defaults(func=bg_size)

    bg_kill_parser = sub.add_parser("bg-kill")
    bg_kill_parser.add_argument("--task-id", required=True)
    bg_kill_parser.add_argument("--bg-dir", required=True)
    bg_kill_parser.add_argument("--force", action="store_true")
    bg_kill_parser.set_defaults(func=bg_kill)
```

- [ ] **Step 4: Run background tests and verify they pass**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_runner.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit background runner task**

Run:

```bash
git add sagents/utils/sandbox/runtime/runner.py tests/sagents/utils/sandbox/runtime/test_runner.py
git commit -m "feat: add sandbox runner background tasks"
```

## Task 6: Metric Agent And Sandbox Image

**Files:**
- Create: `sagents/utils/sandbox/runtime/agent.py`
- Create: `deploy/images/Dockerfile.sandbox`
- Test: `tests/sagents/utils/sandbox/runtime/test_metrics.py`

- [ ] **Step 1: Add failing agent smoke test**

Append to `tests/sagents/utils/sandbox/runtime/test_metrics.py`:

```python
import subprocess
import sys


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
```

- [ ] **Step 2: Run agent smoke test and verify it fails**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_metrics.py::test_metric_agent_smoke_starts_and_exits_on_check_flag -v
```

Expected: FAIL because `sagents.utils.sandbox.runtime.agent` does not exist.

- [ ] **Step 3: Implement metric agent check mode and idle loop**

Create `sagents/utils/sandbox/runtime/agent.py`:

```python
from __future__ import annotations

import argparse
import signal
import sys
import time


def run_forever() -> int:
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("sage-sandbox-metric-agent started", flush=True)
    while running:
        time.sleep(1)
    print("sage-sandbox-metric-agent stopped", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sage-sandbox-metric-agent")
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        print("sage-sandbox-metric-agent ok")
        return 0
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Add sandbox Dockerfile**

Create `deploy/images/Dockerfile.sandbox`:

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    SAGE_SANDBOX_RUNTIME=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        coreutils \
        procps \
        tar \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/sage

COPY sagents /opt/sage/sagents

RUN ln -s /usr/local/bin/python /usr/local/bin/sage-shell-runner-python \
    && printf '#!/bin/sh\nexec python -m sagents.utils.sandbox.runtime.runner "$@"\n' > /usr/local/bin/sage-shell-runner \
    && printf '#!/bin/sh\nexec python -m sagents.utils.sandbox.runtime.agent "$@"\n' > /usr/local/bin/sage-sandbox-metric-agent \
    && chmod +x /usr/local/bin/sage-shell-runner /usr/local/bin/sage-sandbox-metric-agent

RUN mkdir -p /sage-workspace

WORKDIR /sage-workspace

CMD ["sage-sandbox-metric-agent"]
```

- [ ] **Step 5: Run agent and Dockerfile checks**

Run:

```bash
pytest tests/sagents/utils/sandbox/runtime/test_metrics.py -v
test -f deploy/images/Dockerfile.sandbox
```

Expected: PASS and `test -f` exits 0.

- [ ] **Step 6: Commit agent and image task**

Run:

```bash
git add sagents/utils/sandbox/runtime/agent.py deploy/images/Dockerfile.sandbox tests/sagents/utils/sandbox/runtime/test_metrics.py
git commit -m "feat: add sandbox metric agent image"
```

## Task 7: Kubernetes Provider Configuration And Pod Lifecycle

**Files:**
- Modify: `sagents/utils/sandbox/providers/remote/kubernetes.py`
- Modify: `sagents/utils/sandbox/factory.py`
- Test: `tests/sagents/utils/sandbox/test_kubernetes_provider.py`

- [ ] **Step 1: Write failing provider config tests**

Create `tests/sagents/utils/sandbox/test_kubernetes_provider.py`:

```python
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sagents.utils.sandbox.providers.remote.kubernetes import KubernetesSandboxProvider


def make_provider(**overrides):
    config = {
        "sandbox_id": "sandbox_ABC",
        "namespace": "sage",
        "image": "sage/sandbox-runtime:latest",
        "virtual_workspace": "/sage-workspace",
        "timeout": timedelta(seconds=30),
        "pvc": {
            "claim_name": "sage-sandbox-workspaces",
            "mount_path": "/sage-workspace",
            "sub_path_template": "sessions/{session_id}",
        },
        "session_id": "session-123",
    }
    config.update(overrides)
    return KubernetesSandboxProvider(**config)


def test_provider_requires_incluster_config(monkeypatch):
    import sagents.utils.sandbox.providers.remote.kubernetes as module

    provider = make_provider()
    loaded = []

    monkeypatch.setattr(module, "k8s_config", SimpleNamespace(load_incluster_config=lambda: loaded.append(True)))
    monkeypatch.setattr(module, "k8s_client", SimpleNamespace(CoreV1Api=lambda: Mock()))
    monkeypatch.setattr(provider, "_core_v1", Mock())
    monkeypatch.setattr(provider, "_ensure_pod", Mock())

    provider._load_kubernetes_config()

    assert loaded == [True]


def test_sub_path_template_requires_session_id():
    with pytest.raises(ValueError, match="session_id"):
        make_provider(session_id=None)._render_sub_path()


def test_pod_manifest_uses_pvc_subpath_service_account_and_root_defaults():
    provider = make_provider(service_account_name="sage-sandbox-runner")

    manifest = provider._build_pod_manifest()
    container = manifest["spec"]["containers"][0]

    assert manifest["metadata"]["name"].startswith("sage-sandbox-")
    assert manifest["spec"]["serviceAccountName"] == "sage-sandbox-runner"
    assert manifest["spec"]["securityContext"] == {"runAsUser": 0, "runAsGroup": 0}
    assert container["image"] == "sage/sandbox-runtime:latest"
    assert container["volumeMounts"][0]["subPath"] == "sessions/session-123"
    assert manifest["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "sage-sandbox-workspaces"
```

- [ ] **Step 2: Run provider config tests and verify they fail**

Run:

```bash
pytest tests/sagents/utils/sandbox/test_kubernetes_provider.py -v
```

Expected: FAIL because provider constructor and helper methods do not match the new contract.

- [ ] **Step 3: Refactor Kubernetes provider constructor and manifest helpers**

Modify `sagents/utils/sandbox/providers/remote/kubernetes.py` to include module-level imports that tests can patch:

```python
try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes import stream as k8s_stream
except ImportError:
    k8s_client = None
    k8s_config = None
    k8s_stream = None
```

Update `KubernetesSandboxProvider.__init__` to accept:

```python
def __init__(
    self,
    sandbox_id: str,
    namespace: str,
    image: str,
    timeout: timedelta = timedelta(minutes=30),
    workspace_mount: Optional[str] = None,
    mount_paths: Optional[List[MountPath]] = None,
    virtual_workspace: str = "/sage-workspace",
    resources: Optional[Dict[str, Any]] = None,
    service_account_name: Optional[str] = None,
    session_id: Optional[str] = None,
    pvc: Optional[Dict[str, Any]] = None,
    pod_labels: Optional[Dict[str, str]] = None,
    pod_annotations: Optional[Dict[str, str]] = None,
    pod_security_context: Optional[Dict[str, Any]] = None,
    container_security_context: Optional[Dict[str, Any]] = None,
    command_logging: Optional[Dict[str, Any]] = None,
):
```

Add helpers:

```python
def _load_kubernetes_config(self) -> None:
    if k8s_config is None or k8s_client is None:
        raise ImportError("kubernetes package is required. Install with: pip install kubernetes")
    k8s_config.load_incluster_config()
    self._k8s_client = k8s_client.CoreV1Api()


def _pod_name_for_sandbox(self) -> str:
    import re

    safe = re.sub(r"[^a-z0-9-]+", "-", self._sandbox_id.lower()).strip("-")
    safe = safe[:48].strip("-") or "sandbox"
    return f"sage-sandbox-{safe}"


def _render_sub_path(self) -> Optional[str]:
    template = (self.pvc or {}).get("sub_path_template")
    if not template:
        return None
    if "{session_id}" in template and not self.session_id:
        raise ValueError("remote_provider_config.session_id is required for pvc.sub_path_template")
    return template.format(session_id=self.session_id)


def _build_pod_manifest(self) -> Dict[str, Any]:
    if not self.namespace:
        raise ValueError("remote_provider_config.namespace is required")
    if not self.pvc or not self.pvc.get("claim_name"):
        raise ValueError("remote_provider_config.pvc.claim_name is required")
    mount_path = self.pvc.get("mount_path") or self._workspace_path
    volume_mount: Dict[str, Any] = {"name": "workspace", "mountPath": mount_path}
    sub_path = self._render_sub_path()
    if sub_path:
        volume_mount["subPath"] = sub_path
    container: Dict[str, Any] = {
        "name": "sandbox",
        "image": self.image,
        "imagePullPolicy": "IfNotPresent",
        "volumeMounts": [volume_mount],
        "resources": self.resources,
        "securityContext": self.container_security_context,
    }
    manifest: Dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": self._pod_name_for_sandbox(),
            "namespace": self.namespace,
            "labels": {"app": "sage-sandbox", "sandbox-id": self._sandbox_id, **self.pod_labels},
            "annotations": self.pod_annotations,
        },
        "spec": {
            "restartPolicy": "Never",
            "securityContext": self.pod_security_context,
            "containers": [container],
            "volumes": [
                {
                    "name": "workspace",
                    "persistentVolumeClaim": {"claimName": self.pvc["claim_name"]},
                }
            ],
        },
    }
    if self.service_account_name:
        manifest["spec"]["serviceAccountName"] = self.service_account_name
    return manifest
```

Set defaults in `__init__`:

```python
self.service_account_name = service_account_name
self.session_id = session_id
self.pvc = pvc or {}
self.pod_labels = pod_labels or {}
self.pod_annotations = pod_annotations or {}
self.pod_security_context = pod_security_context or {"runAsUser": 0, "runAsGroup": 0}
self.container_security_context = container_security_context or {}
self.command_logging = command_logging or {"enabled": False, "redact": True}
self._pod_name = self._pod_name_for_sandbox()
```

- [ ] **Step 4: Update factory Kubernetes config passing**

Modify the Kubernetes branch in `sagents/utils/sandbox/factory.py` so it passes explicit config without duplicating keys:

```python
elif config.remote_provider == "kubernetes":
    kubernetes_config = {
        k: v
        for k, v in provider_config.items()
        if k
        not in {
            "workspace_mount",
            "namespace",
            "resources",
            "service_account_name",
            "session_id",
            "pvc",
            "pod_labels",
            "pod_annotations",
            "pod_security_context",
            "container_security_context",
            "command_logging",
        }
    }
    return provider_class(
        **common_kwargs,
        namespace=provider_config.get("namespace", "default"),
        image=config.remote_image,
        resources=provider_config.get("resources", {}),
        service_account_name=provider_config.get("service_account_name"),
        session_id=provider_config.get("session_id"),
        pvc=provider_config.get("pvc"),
        pod_labels=provider_config.get("pod_labels", {}),
        pod_annotations=provider_config.get("pod_annotations", {}),
        pod_security_context=provider_config.get("pod_security_context"),
        container_security_context=provider_config.get("container_security_context"),
        command_logging=provider_config.get("command_logging"),
        **kubernetes_config,
    )
```

- [ ] **Step 5: Run provider config tests and verify they pass**

Run:

```bash
pytest tests/sagents/utils/sandbox/test_kubernetes_provider.py -v
```

Expected: PASS for the initial provider config tests.

- [ ] **Step 6: Commit provider config task**

Run:

```bash
git add sagents/utils/sandbox/providers/remote/kubernetes.py sagents/utils/sandbox/factory.py tests/sagents/utils/sandbox/test_kubernetes_provider.py
git commit -m "feat: configure kubernetes sandbox pods"
```

## Task 8: Kubernetes Provider Runner Exec And File APIs

**Files:**
- Modify: `sagents/utils/sandbox/providers/remote/kubernetes.py`
- Modify: `tests/sagents/utils/sandbox/test_kubernetes_provider.py`

- [ ] **Step 1: Add failing runner conversion and file tests**

Append to `tests/sagents/utils/sandbox/test_kubernetes_provider.py`:

```python
import asyncio
import base64
import json


def test_runner_response_converts_to_command_result(monkeypatch):
    provider = make_provider()

    async def fake_exec(args, stdin_data=None, timeout=30):
        return json.dumps(
            {
                "success": False,
                "stdout": "out",
                "stderr": "err",
                "exit_code": 7,
                "duration_ms": 1250,
                "timeout": False,
            }
        )

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    result = asyncio.run(provider.execute_command("echo hi", workdir="/sage-workspace", timeout=3))

    assert result.success is False
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.return_code == 7
    assert result.execution_time == 1.25


def test_file_list_converts_json_entries(monkeypatch):
    provider = make_provider()

    async def fake_exec(args, stdin_data=None, timeout=30):
        return json.dumps(
            {
                "entries": [
                    {
                        "path": "/sage-workspace/a.txt",
                        "is_file": True,
                        "is_dir": False,
                        "size": 3,
                        "modified_time": 10.5,
                    }
                ]
            }
        )

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    entries = asyncio.run(provider.list_directory("/sage-workspace"))

    assert len(entries) == 1
    assert entries[0].path == "/sage-workspace/a.txt"
    assert entries[0].size == 3


def test_execute_python_passes_code_as_base64(monkeypatch):
    provider = make_provider()
    captured = {}

    async def fake_execute_command(command, workdir=None, timeout=60, env_vars=None):
        captured["command"] = command
        return SimpleNamespace(success=True, stdout="ok", stderr="", execution_time=0.2)

    monkeypatch.setattr(provider, "execute_command", fake_execute_command)

    result = asyncio.run(provider.execute_python("print('secret code')"))

    assert result.success is True
    assert result.output == "ok"
    assert "secret code" not in captured["command"]
    assert "base64" in captured["command"]
```

- [ ] **Step 2: Run new provider tests and verify they fail**

Run:

```bash
pytest tests/sagents/utils/sandbox/test_kubernetes_provider.py::test_runner_response_converts_to_command_result tests/sagents/utils/sandbox/test_kubernetes_provider.py::test_file_list_converts_json_entries -v
```

Expected: FAIL because `_exec_runner` and conversions are not implemented.

- [ ] **Step 3: Implement runner exec helper and command conversion**

In `sagents/utils/sandbox/providers/remote/kubernetes.py`, add imports:

```python
import base64
import json
import shlex
import time
```

Add helper methods:

```python
def _b64(self, text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


async def _exec_runner(
    self,
    args: List[str],
    stdin_data: bytes | None = None,
    timeout: int = 30,
) -> str:
    if not self._is_initialized:
        await self.initialize()
    if k8s_stream is None:
        raise ImportError("kubernetes package is required. Install with: pip install kubernetes")
    command = ["sage-shell-runner", *args]
    if stdin_data is not None:
        ws = k8s_stream.stream(
            self._k8s_client.connect_get_namespaced_pod_exec,
            self._pod_name,
            self.namespace,
            command=command,
            stderr=True,
            stdin=True,
            stdout=True,
            tty=False,
            _preload_content=False,
            _request_timeout=timeout,
        )
        try:
            ws.write_stdin(base64.b64encode(stdin_data).decode("ascii"))
            ws.close()
            stdout_chunks = []
            stderr_chunks = []
            while ws.is_open():
                ws.update(timeout=1)
                if ws.peek_stdout():
                    stdout_chunks.append(ws.read_stdout())
                if ws.peek_stderr():
                    stderr_chunks.append(ws.read_stderr())
            if stderr_chunks and not stdout_chunks:
                raise RuntimeError("Kubernetes runner exec failed: " + "".join(stderr_chunks))
            return "".join(stdout_chunks)
        finally:
            ws.close()
    return k8s_stream.stream(
        self._k8s_client.connect_get_namespaced_pod_exec,
        self._pod_name,
        self.namespace,
        command=command,
        stderr=True,
        stdin=stdin_data is not None,
        stdout=True,
        tty=False,
        _request_timeout=timeout,
    )
```

Replace `execute_command()` with:

```python
async def execute_command(
    self,
    command: str,
    workdir: Optional[str] = None,
    timeout: int = 30,
    env_vars: Optional[Dict[str, str]] = None,
    background: bool = False,
) -> CommandResult:
    started = time.monotonic()
    runner_args = [
        "run",
        "--command-b64",
        self._b64(command),
        "--workdir",
        workdir or self._workspace_path,
        "--timeout",
        str(timeout),
        "--env-json-b64",
        self._b64(json.dumps(env_vars or {})),
        "--sandbox-id",
        self._sandbox_id,
    ]
    if self.session_id:
        runner_args.extend(["--session-id", self.session_id])
    if self.command_logging.get("enabled"):
        runner_args.append("--log-command")
    raw = await self._exec_runner(runner_args, timeout=timeout + 5)
    try:
        payload = json.loads(raw.strip().splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"Invalid sage-shell-runner response: {raw[:500]}") from exc
    return CommandResult(
        success=bool(payload.get("success")),
        stdout=str(payload.get("stdout", "")),
        stderr=str(payload.get("stderr", "")),
        return_code=int(payload.get("exit_code", 1)),
        execution_time=float(payload.get("duration_ms", int((time.monotonic() - started) * 1000))) / 1000.0,
    )
```

- [ ] **Step 4: Implement file API conversion through runner**

Replace file methods in `kubernetes.py` with:

```python
async def read_file(self, path: str, encoding: str = "utf-8") -> str:
    raw = await self._exec_runner(["file-read", "--path", path, "--encoding", encoding])
    return str(json.loads(raw)["content"])


async def write_file(
    self,
    path: str,
    content: str,
    encoding: str = "utf-8",
    mode: str = "overwrite",
) -> None:
    data = base64.b64encode(content.encode(encoding)).decode("ascii")
    await self._exec_runner(["file-write", "--path", path, "--mode", mode, "--content-b64", data])


async def file_exists(self, path: str) -> bool:
    try:
        await self._exec_runner(["file-stat", "--path", path])
        return True
    except Exception:
        return False


async def list_directory(self, path: str, include_hidden: bool = False) -> List[FileInfo]:
    args = ["file-list", "--path", path]
    if include_hidden:
        args.append("--include-hidden")
    raw = await self._exec_runner(args)
    entries = json.loads(raw).get("entries", [])
    return [
        FileInfo(
            path=str(entry["path"]),
            is_file=bool(entry["is_file"]),
            is_dir=bool(entry["is_dir"]),
            size=int(entry["size"]),
            modified_time=float(entry["modified_time"]),
        )
        for entry in entries
    ]


async def get_mtime(self, path: str) -> float:
    try:
        raw = await self._exec_runner(["file-stat", "--path", path])
        return float(json.loads(raw).get("modified_time", 0))
    except Exception:
        return 0


async def ensure_directory(self, path: str) -> None:
    await self._exec_runner(["file-mkdir", "--path", path])


async def delete_file(self, path: str) -> None:
    await self._exec_runner(["file-delete", "--path", path])
```

Replace `execute_python()` and `execute_javascript()` with base64-safe command construction:

```python
async def execute_python(
    self,
    code: str,
    requirements: Optional[List[str]] = None,
    workdir: Optional[str] = None,
    timeout: int = 60,
) -> ExecutionResult:
    code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    command = (
        "python -c "
        + shlex.quote(
            "import base64; exec(base64.b64decode("
            + repr(code_b64)
            + ").decode('utf-8'))"
        )
    )
    result = await self.execute_command(command, workdir, timeout)
    return ExecutionResult(
        success=result.success,
        output=result.stdout,
        error=result.stderr if not result.success else None,
        execution_time=result.execution_time,
        installed_packages=requirements or [],
    )


async def execute_javascript(
    self,
    code: str,
    packages: Optional[List[str]] = None,
    workdir: Optional[str] = None,
    timeout: int = 60,
) -> ExecutionResult:
    code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    command = "node -e " + shlex.quote(
        "eval(Buffer.from(" + repr(code_b64) + ", 'base64').toString())"
    )
    result = await self.execute_command(command, workdir, timeout)
    return ExecutionResult(
        success=result.success,
        output=result.stdout,
        error=result.stderr if not result.success else None,
        execution_time=result.execution_time,
        installed_packages=packages or [],
    )
```

- [ ] **Step 5: Run provider tests and verify they pass**

Run:

```bash
pytest tests/sagents/utils/sandbox/test_kubernetes_provider.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit runner exec and file APIs**

Run:

```bash
git add sagents/utils/sandbox/providers/remote/kubernetes.py tests/sagents/utils/sandbox/test_kubernetes_provider.py
git commit -m "feat: execute kubernetes sandbox commands via runner"
```

## Task 9: Kubernetes Provider Background APIs

**Files:**
- Modify: `sagents/utils/sandbox/providers/remote/kubernetes.py`
- Modify: `tests/sagents/utils/sandbox/test_kubernetes_provider.py`

- [ ] **Step 1: Add failing background provider tests**

Append to `tests/sagents/utils/sandbox/test_kubernetes_provider.py`:

```python
def test_background_provider_methods_call_runner(monkeypatch):
    provider = make_provider()
    calls = []

    async def fake_exec(args, stdin_data=None, timeout=30):
        calls.append(args)
        if args[0] == "bg-start":
            return json.dumps(
                {
                    "task_id": "shtask_abc",
                    "pid": 123,
                    "log_path": "/sage-workspace/.sage/bg/shtask_abc.log",
                    "exit_path": "/sage-workspace/.sage/bg/shtask_abc.exit",
                }
            )
        if args[0] == "bg-read":
            return json.dumps({"text": "hello", "size": 5})
        if args[0] == "bg-read-range":
            return json.dumps({"text": "he", "offset": 2, "size": 5})
        if args[0] == "bg-size":
            return json.dumps({"size": 5})
        if args[0] == "bg-state":
            return json.dumps({"alive": False, "exit_code": 0})
        if args[0] == "bg-kill":
            return json.dumps({"ok": True})
        raise AssertionError(args)

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    started = asyncio.run(provider.start_background("echo hello"))
    text = asyncio.run(provider.read_background_output("shtask_abc"))
    ranged = asyncio.run(provider.read_background_output_range("shtask_abc", 0, 2))
    size = asyncio.run(provider.get_background_output_size("shtask_abc"))
    alive = asyncio.run(provider.is_background_alive("shtask_abc"))
    exit_code = asyncio.run(provider.get_background_exit_code("shtask_abc"))
    killed = asyncio.run(provider.kill_background("shtask_abc", force=True))

    assert started["task_id"] == "shtask_abc"
    assert text == "hello"
    assert ranged == ("he", 2)
    assert size == 5
    assert alive is False
    assert exit_code == 0
    assert killed is True
    assert provider.supports_background() is True
```

- [ ] **Step 2: Run background provider test and verify it fails**

Run:

```bash
pytest tests/sagents/utils/sandbox/test_kubernetes_provider.py::test_background_provider_methods_call_runner -v
```

Expected: FAIL because background provider methods are not implemented.

- [ ] **Step 3: Implement background provider methods**

Add to `KubernetesSandboxProvider`:

```python
def _bg_dir(self, log_dir: Optional[str] = None) -> str:
    return log_dir or f"{self._workspace_path.rstrip('/')}/.sage/bg"


def supports_background(self) -> bool:
    return True


async def start_background(
    self,
    command: str,
    workdir: Optional[str] = None,
    env_vars: Optional[Dict[str, str]] = None,
    log_dir: Optional[str] = None,
) -> Dict[str, Any]:
    raw = await self._exec_runner(
        [
            "bg-start",
            "--command-b64",
            self._b64(command),
            "--workdir",
            workdir or self._workspace_path,
            "--bg-dir",
            self._bg_dir(log_dir),
        ]
    )
    return json.loads(raw)


async def read_background_output(self, task_id: str, max_bytes: int = 8192) -> str:
    raw = await self._exec_runner(
        ["bg-read", "--task-id", task_id, "--bg-dir", self._bg_dir(), "--max-bytes", str(max_bytes)]
    )
    return str(json.loads(raw).get("text", ""))


async def read_background_output_range(
    self, task_id: str, offset: int = 0, max_bytes: int = 1 << 20
) -> Tuple[str, int]:
    raw = await self._exec_runner(
        [
            "bg-read-range",
            "--task-id",
            task_id,
            "--bg-dir",
            self._bg_dir(),
            "--offset",
            str(offset),
            "--max-bytes",
            str(max_bytes),
        ]
    )
    payload = json.loads(raw)
    return str(payload.get("text", "")), int(payload.get("offset", offset))


async def get_background_output_size(self, task_id: str) -> Optional[int]:
    raw = await self._exec_runner(["bg-size", "--task-id", task_id, "--bg-dir", self._bg_dir()])
    return int(json.loads(raw).get("size", 0))


async def is_background_alive(self, task_id: str) -> bool:
    raw = await self._exec_runner(["bg-state", "--task-id", task_id, "--bg-dir", self._bg_dir()])
    return bool(json.loads(raw).get("alive"))


async def get_background_exit_code(self, task_id: str) -> Optional[int]:
    raw = await self._exec_runner(["bg-state", "--task-id", task_id, "--bg-dir", self._bg_dir()])
    value = json.loads(raw).get("exit_code")
    return None if value is None else int(value)


async def kill_background(self, task_id: str, force: bool = False) -> bool:
    args = ["bg-kill", "--task-id", task_id, "--bg-dir", self._bg_dir()]
    if force:
        args.append("--force")
    raw = await self._exec_runner(args)
    return bool(json.loads(raw).get("ok"))


async def cleanup_background(self, task_id: str) -> None:
    return None
```

Ensure `Tuple` is imported from `typing`.

- [ ] **Step 4: Run provider tests and verify they pass**

Run:

```bash
pytest tests/sagents/utils/sandbox/test_kubernetes_provider.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit background provider task**

Run:

```bash
git add sagents/utils/sandbox/providers/remote/kubernetes.py tests/sagents/utils/sandbox/test_kubernetes_provider.py
git commit -m "feat: add kubernetes sandbox background commands"
```

## Task 10: Tar-Based Copy From Host

**Files:**
- Modify: `sagents/utils/sandbox/providers/remote/kubernetes.py`
- Modify: `tests/sagents/utils/sandbox/test_kubernetes_provider.py`

- [ ] **Step 1: Add failing copy tests**

Append to `tests/sagents/utils/sandbox/test_kubernetes_provider.py`:

```python
def test_copy_from_host_builds_tar_for_file(monkeypatch, tmp_path):
    provider = make_provider()
    source = tmp_path / "a.txt"
    source.write_text("hello", encoding="utf-8")
    captured = {}

    async def fake_exec(args, stdin_data=None, timeout=30):
        captured["args"] = args
        captured["stdin_data"] = stdin_data
        return json.dumps({"ok": True})

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    copied = asyncio.run(provider.copy_from_host(str(source), "/sage-workspace/copied"))

    assert copied is True
    assert captured["args"][:3] == ["tar-extract", "--dest", "/sage-workspace/copied"]
    assert captured["stdin_data"].startswith(b"")


def test_copy_from_host_missing_returns_false(tmp_path):
    provider = make_provider()

    copied = asyncio.run(provider.copy_from_host(str(tmp_path / "missing"), "/sage-workspace/missing"))

    assert copied is False
```

- [ ] **Step 2: Run copy tests and verify they fail**

Run:

```bash
pytest tests/sagents/utils/sandbox/test_kubernetes_provider.py::test_copy_from_host_builds_tar_for_file tests/sagents/utils/sandbox/test_kubernetes_provider.py::test_copy_from_host_missing_returns_false -v
```

Expected: FAIL because `copy_from_host()` still uses base class upload behavior or lacks tar extraction.

- [ ] **Step 3: Add runner tar-extract command**

Add a `tar-extract` parser to `runner.py`:

```python
def tar_extract(args: argparse.Namespace) -> int:
    import tarfile

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    tar_bytes = base64.b64decode(sys.stdin.read().encode("ascii"))
    import io

    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        for member in archive:
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise RuntimeError(f"Unsafe tar member path: {member.name}")
            archive.extract(member, path=dest)
    sys.stdout.write(json.dumps({"ok": True}) + "\n")
    return 0
```

Add to `build_parser()`:

```python
    tar_parser = sub.add_parser("tar-extract")
    tar_parser.add_argument("--dest", required=True)
    tar_parser.set_defaults(func=tar_extract)
```

- [ ] **Step 4: Implement provider tar creation and copy_from_host**

In `kubernetes.py`, add imports:

```python
import fnmatch
import io
import os
import tarfile
```

Add helper:

```python
def _make_tar_bytes(
    self,
    host_source_path: str,
    ignore_patterns: Optional[List[str]] = None,
) -> bytes:
    ignore_patterns = ignore_patterns or []
    source = os.path.abspath(host_source_path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        if os.path.isdir(source):
            for root, dirs, files in os.walk(source):
                dirs[:] = [
                    name
                    for name in dirs
                    if not any(fnmatch.fnmatch(name, pattern) for pattern in ignore_patterns)
                ]
                for file_name in files:
                    if any(fnmatch.fnmatch(file_name, pattern) for pattern in ignore_patterns):
                        continue
                    full_path = os.path.join(root, file_name)
                    archive.add(full_path, arcname=os.path.relpath(full_path, source))
        else:
            archive.add(source, arcname=os.path.basename(source))
    return buf.getvalue()
```

Override `copy_from_host()`:

```python
async def copy_from_host(
    self,
    host_source_path: str,
    sandbox_dest_path: str,
    ignore_patterns: Optional[List[str]] = None,
) -> bool:
    if not os.path.exists(host_source_path):
        return False
    tar_bytes = await asyncio.to_thread(self._make_tar_bytes, host_source_path, ignore_patterns)
    await self._exec_runner(["tar-extract", "--dest", sandbox_dest_path], stdin_data=tar_bytes)
    return True
```

Ensure `asyncio` is imported.

- [ ] **Step 5: Run copy tests and provider tests**

Run:

```bash
pytest tests/sagents/utils/sandbox/test_kubernetes_provider.py tests/sagents/utils/sandbox/runtime/test_runner.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit tar copy task**

Run:

```bash
git add sagents/utils/sandbox/providers/remote/kubernetes.py sagents/utils/sandbox/runtime/runner.py tests/sagents/utils/sandbox/test_kubernetes_provider.py
git commit -m "feat: add tar copy for kubernetes sandbox"
```

## Task 11: Documentation And Final Verification

**Files:**
- Modify: `docs/en/architecture/ARCHITECTURE_SAGENTS_SANDBOX_OBS.md`
- Create: `docs/en/architecture/KUBERNETES_SANDBOX.md`
- Test: existing targeted pytest suites

- [ ] **Step 1: Add Kubernetes sandbox documentation**

Create `docs/en/architecture/KUBERNETES_SANDBOX.md`:

```markdown
# Kubernetes Sandbox

The Kubernetes sandbox provider runs Sage sandbox workloads as Kubernetes Pods. Sage Server must run inside the target cluster and must have a ServiceAccount that can read, create, delete, exec into, and watch sandbox Pods in the configured namespace.

## Provider Config

```python
SandboxConfig(
    mode=SandboxType.REMOTE,
    remote_provider="kubernetes",
    sandbox_id="sandbox-123",
    sandbox_agent_workspace="/sage-workspace",
    remote_image="sage/sandbox-runtime:latest",
    remote_provider_config={
        "namespace": "sage",
        "service_account_name": "sage-sandbox-runner",
        "session_id": "session-123",
        "pvc": {
            "claim_name": "sage-sandbox-workspaces",
            "mount_path": "/sage-workspace",
            "sub_path_template": "sessions/{session_id}",
        },
        "resources": {
            "requests": {"cpu": "500m", "memory": "512Mi"},
            "limits": {"cpu": "2", "memory": "2Gi"},
        },
    },
)
```

## Runtime Image

The configured image must include:

- `sage-shell-runner`
- `sage-sandbox-metric-agent`
- `tar`
- Python 3.11 or newer

The project-maintained image is defined in `deploy/images/Dockerfile.sandbox`.

## Lifecycle

The provider reuses a Ready Pod with the same `sandbox_id`. It deletes and recreates terminal Pods. `cleanup()` leaves the Pod and PVC in place. `kill()` deletes the Pod and keeps the PVC.

## Metrics

The metric agent writes JSON shell execution events to stdout by default. Raw command logging is disabled unless explicitly enabled.
```

- [ ] **Step 2: Link architecture doc**

Add this bullet under the sandbox section in `docs/en/architecture/ARCHITECTURE_SAGENTS_SANDBOX_OBS.md`:

```markdown
- **Kubernetes remote sandbox**: see [Kubernetes Sandbox](KUBERNETES_SANDBOX.md) for in-cluster provider configuration, PVC workspace binding, the sandbox runtime image, shell runner behavior, and metric events.
```

- [ ] **Step 3: Run targeted tests**

Run:

```bash
pytest \
  tests/sagents/utils/sandbox/runtime/test_protocol.py \
  tests/sagents/utils/sandbox/runtime/test_metrics.py \
  tests/sagents/utils/sandbox/runtime/test_runner.py \
  tests/sagents/utils/sandbox/test_kubernetes_provider.py \
  tests/sagents/tool/impl/test_execute_command_tool.py \
  tests/sagents/tool/impl/test_execute_shell_integration.py \
  -v
```

Expected: PASS. If `test_execute_shell_integration.py` is too slow in the environment, run the first four suites plus `tests/sagents/tool/impl/test_execute_command_tool.py` and record the skipped slow integration in the final handoff.

- [ ] **Step 4: Run static import check**

Run:

```bash
python -m compileall sagents/utils/sandbox/runtime sagents/utils/sandbox/providers/remote/kubernetes.py
```

Expected: exits 0.

- [ ] **Step 5: Check git diff**

Run:

```bash
git status --short
git diff --stat
```

Expected: only Kubernetes sandbox implementation, tests, Dockerfile, and docs are changed.

- [ ] **Step 6: Commit documentation and verification task**

Run:

```bash
git add docs/en/architecture/KUBERNETES_SANDBOX.md docs/en/architecture/ARCHITECTURE_SAGENTS_SANDBOX_OBS.md
git commit -m "docs: document kubernetes sandbox provider"
```

## Final Handoff Checklist

- [ ] Confirm all committed tasks are on the same branch.
- [ ] Confirm `.superpowers/` is not staged.
- [ ] Report exact test commands and results.
- [ ] Report any environment-dependent tests that were skipped.
- [ ] Summarize the new provider config contract and runtime image requirement.
