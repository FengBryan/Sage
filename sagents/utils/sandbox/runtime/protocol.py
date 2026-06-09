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
