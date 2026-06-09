from __future__ import annotations

import argparse
import base64
import io
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tarfile
import time
import uuid

from .metrics import command_hash, redact_command
from .protocol import CommandExecutionEvent, CommandRunResponse, encode_json_line


TIMEOUT_EXIT_CODE = 124
TASK_ID_PATTERN = re.compile(r"^shtask_[A-Za-z0-9_-]+$")


class RunnerError(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def _decode_text(value: str | None, default: str = "") -> str:
    if not value:
        return default
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def _decode_env(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    payload = json.loads(_decode_text(value))
    return {str(key): str(item) for key, item in payload.items()}


def _ensure_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def _emit_metric_event(
    args: argparse.Namespace,
    command: str,
    response: CommandRunResponse,
    stdout_bytes: int,
    stderr_bytes: int,
) -> None:
    if not args.sandbox_id or not args.session_id:
        return
    event = CommandExecutionEvent(
        event="shell_execution_finished",
        sandbox_id=args.sandbox_id,
        session_id=args.session_id,
        command_id=args.command_id or uuid.uuid4().hex,
        command_hash=command_hash(command),
        command_length=len(command),
        workdir=args.workdir,
        exit_code=response.exit_code,
        duration_ms=response.duration_ms,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        timeout=response.timeout,
        command_redacted=redact_command(command) if args.log_command else None,
    )
    print(encode_json_line(event).rstrip("\n"), file=sys.stderr, flush=True)


def run_command(args: argparse.Namespace) -> int:
    command = _decode_text(args.command_b64)
    env = os.environ.copy()
    env.update(_decode_env(args.env_json_b64))
    start = time.monotonic()

    process = subprocess.Popen(
        command,
        shell=True,
        cwd=args.workdir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=args.timeout)
        duration_ms = int((time.monotonic() - start) * 1000)
        response = CommandRunResponse(
            success=process.returncode == 0,
            stdout=_ensure_text(stdout),
            stderr=_ensure_text(stderr),
            exit_code=process.returncode,
            duration_ms=duration_ms,
            timeout=False,
        )
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        stdout, stderr = process.communicate()
        duration_ms = int((time.monotonic() - start) * 1000)
        response = CommandRunResponse(
            success=False,
            stdout=_ensure_text(stdout),
            stderr=_ensure_text(stderr),
            exit_code=TIMEOUT_EXIT_CODE,
            duration_ms=duration_ms,
            timeout=True,
            error_type="timeout",
            error_message=f"Command timed out after {args.timeout} seconds",
        )

    _emit_metric_event(args, command, response, len(stdout or b""), len(stderr or b""))
    sys.stdout.write(encode_json_line(response))
    return 0


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _file_info(path: Path) -> dict[str, object]:
    stat_result = path.lstat()
    try:
        is_file = path.is_file()
        is_dir = path.is_dir()
    except OSError:
        is_file = False
        is_dir = False
    return {
        "path": str(path),
        "name": path.name,
        "is_file": is_file,
        "is_dir": is_dir,
        "size": stat_result.st_size,
        "modified_time": stat_result.st_mtime,
    }


def file_read(args: argparse.Namespace) -> int:
    path = Path(args.path)
    content = path.read_text(encoding=args.encoding)
    sys.stdout.write(json.dumps({"content": content}, ensure_ascii=False) + "\n")
    return 0


def file_write(args: argparse.Namespace) -> int:
    path = Path(args.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = base64.b64decode(args.content_b64.encode("ascii"))
    mode = "ab" if args.mode == "append" else "wb"
    with path.open(mode) as file:
        file.write(content)
    sys.stdout.write(json.dumps({"ok": True}) + "\n")
    return 0


def file_list(args: argparse.Namespace) -> int:
    path = Path(args.path)
    entries = []
    if path.is_dir():
        for child in path.iterdir():
            if child.name.startswith(".") and not args.include_hidden:
                continue
            try:
                entries.append(_file_info(child))
            except OSError:
                continue
    entries.sort(key=lambda item: (not item["is_dir"], str(item["name"])))
    sys.stdout.write(json.dumps({"entries": entries}, ensure_ascii=False) + "\n")
    return 0


def file_stat(args: argparse.Namespace) -> int:
    path = Path(args.path)
    sys.stdout.write(json.dumps(_file_info(path), ensure_ascii=False) + "\n")
    return 0


def file_delete(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    sys.stdout.write(json.dumps({"ok": True}) + "\n")
    return 0


def file_mkdir(args: argparse.Namespace) -> int:
    Path(args.path).mkdir(parents=True, exist_ok=True)
    sys.stdout.write(json.dumps({"ok": True}) + "\n")
    return 0


def _safe_tar_target(dest: Path, member_name: str) -> Path:
    path = Path(member_name)
    if path.is_absolute() or ".." in path.parts:
        raise RunnerError("invalid_tar_path", f"Unsafe tar member path: {member_name}")

    target = (dest / path).resolve(strict=False)
    try:
        target.relative_to(dest)
    except ValueError as exc:
        raise RunnerError("invalid_tar_path", f"Unsafe tar member path: {member_name}") from exc
    return target


def tar_extract(args: argparse.Namespace) -> int:
    dest = Path(args.dest).resolve(strict=False)
    dest.mkdir(parents=True, exist_ok=True)
    size_bytes = getattr(args, "size_bytes", None)
    data = (
        sys.stdin.buffer.read(size_bytes)
        if size_bytes is not None
        else sys.stdin.buffer.read()
    )
    if size_bytes is not None and len(data) != size_bytes:
        raise RunnerError(
            "invalid_tar",
            f"Expected {size_bytes} tar bytes, received {len(data)}",
        )

    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as exc:
        raise RunnerError("invalid_tar", "Could not read tar archive") from exc

    with archive:
        for member in archive.getmembers():
            target = _safe_tar_target(dest, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym() or member.islnk():
                raise RunnerError(
                    "unsupported_tar_member",
                    f"Tar links are not supported: {member.name}",
                )
            if not member.isfile():
                raise RunnerError(
                    "unsupported_tar_member",
                    f"Tar member type is not supported: {member.name}",
                )

            source = archive.extractfile(member)
            if source is None:
                raise RunnerError(
                    "invalid_tar",
                    f"Could not read tar member: {member.name}",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)

    return _write_json({"ok": True})


def _bg_dir(args: argparse.Namespace) -> Path:
    path = Path(args.bg_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _task_paths(bg_dir: Path, task_id: str) -> tuple[Path, Path, Path]:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise RunnerError("invalid_task_id", "Background task ID is invalid")
    return (
        bg_dir / f"{task_id}.pid",
        bg_dir / f"{task_id}.log",
        bg_dir / f"{task_id}.exit",
    )


def _read_pid(pid_path: Path) -> int | None:
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_is_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False

    stat_path = Path("/proc") / str(pid) / "stat"
    if stat_path.exists():
        try:
            state = stat_path.read_text(encoding="utf-8").split()[2]
        except (IndexError, OSError):
            return False
        if state == "Z":
            return False
    return True


def _process_group_is_alive(pgid: int) -> bool:
    proc = Path("/proc")
    if proc.is_dir():
        for child in proc.iterdir():
            if not child.name.isdigit():
                continue
            pid = int(child.name)
            try:
                if os.getpgid(pid) == pgid and _pid_is_alive(pid):
                    return True
            except OSError:
                continue
        return False

    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def _read_exit_code(exit_path: Path) -> int | None:
    try:
        return int(exit_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_json(payload: dict[str, object]) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


def bg_start(args: argparse.Namespace) -> int:
    bg_dir = _bg_dir(args)
    task_id = "shtask_" + uuid.uuid4().hex[:12]
    pid_path, log_path, exit_path = _task_paths(bg_dir, task_id)
    command = _decode_text(args.command_b64)
    log_path.touch()

    launcher = """
import os
import pathlib
import signal
import subprocess
import sys
import time


def reset_signals():
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    stat_path = pathlib.Path("/proc") / str(pid) / "stat"
    if stat_path.exists():
        try:
            return stat_path.read_text(encoding="utf-8").split()[2] != "Z"
        except (IndexError, OSError):
            return False
    return True


def group_has_live_members(pgid):
    proc = pathlib.Path("/proc")
    current_pid = os.getpid()
    if proc.is_dir():
        for child in proc.iterdir():
            if not child.name.isdigit():
                continue
            pid = int(child.name)
            if pid == current_pid:
                continue
            try:
                if os.getpgid(pid) == pgid and pid_alive(pid):
                    return True
            except OSError:
                continue
        return False
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


cmd = sys.argv[1]
cwd = sys.argv[2]
log = sys.argv[3]
exitp = sys.argv[4]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(log).parent.mkdir(parents=True, exist_ok=True)
with open(log, "ab", buffering=0) as file:
    process = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        stdout=file,
        stderr=subprocess.STDOUT,
        preexec_fn=reset_signals,
    )
    return_code = process.wait()
    pgid = os.getpgrp()
    while group_has_live_members(pgid):
        time.sleep(0.05)
pathlib.Path(exitp).write_text(str(return_code), encoding="utf-8")
"""
    process = subprocess.Popen(
        [sys.executable, "-c", launcher, command, args.workdir, str(log_path), str(exit_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_path.write_text(str(process.pid), encoding="utf-8")
    return _write_json(
        {
            "task_id": task_id,
            "pid": process.pid,
            "log_path": str(log_path),
            "exit_path": str(exit_path),
        }
    )


def bg_state(args: argparse.Namespace) -> int:
    pid_path, _, exit_path = _task_paths(_bg_dir(args), args.task_id)
    exit_code = _read_exit_code(exit_path)
    return _write_json(
        {
            "alive": exit_code is None and _pid_is_alive(_read_pid(pid_path)),
            "exit_code": exit_code,
        }
    )


def bg_read(args: argparse.Namespace) -> int:
    _, log_path, _ = _task_paths(_bg_dir(args), args.task_id)
    data = log_path.read_bytes() if log_path.exists() else b""
    tail = b"" if args.max_bytes <= 0 else data[-args.max_bytes :]
    return _write_json({"text": tail.decode("utf-8", errors="replace"), "size": len(data)})


def bg_read_range(args: argparse.Namespace) -> int:
    _, log_path, _ = _task_paths(_bg_dir(args), args.task_id)
    data = log_path.read_bytes() if log_path.exists() else b""
    start = min(max(args.offset, 0), len(data))
    end = min(start + max(args.max_bytes, 0), len(data))
    chunk = data[start:end]
    return _write_json(
        {
            "text": chunk.decode("utf-8", errors="replace"),
            "offset": end,
            "size": len(data),
        }
    )


def bg_size(args: argparse.Namespace) -> int:
    _, log_path, _ = _task_paths(_bg_dir(args), args.task_id)
    return _write_json({"size": log_path.stat().st_size if log_path.exists() else 0})


def bg_kill(args: argparse.Namespace) -> int:
    pid_path, _, exit_path = _task_paths(_bg_dir(args), args.task_id)
    pid = _read_pid(pid_path)
    signal_number = signal.SIGKILL if args.force else signal.SIGTERM
    ok = False
    if pid is not None:
        try:
            os.killpg(pid, signal_number)
            ok = True
        except ProcessLookupError:
            ok = False
        except OSError:
            try:
                os.kill(pid, signal_number)
                ok = True
            except OSError:
                ok = False
    if ok and args.force:
        deadline = time.monotonic() + 1
        while _process_group_is_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
    if ok and args.force and not _process_group_is_alive(pid) and not exit_path.exists():
        exit_path.write_text(str(128 + int(signal_number)), encoding="utf-8")
    return _write_json({"ok": ok})


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

    extract = sub.add_parser("tar-extract")
    extract.add_argument("--dest", required=True)
    extract.add_argument("--size-bytes", type=int)
    extract.set_defaults(func=tar_extract)

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RunnerError as error:
        sys.stdout.write(
            json.dumps(
                {"error_type": error.error_type, "error_message": error.message},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
