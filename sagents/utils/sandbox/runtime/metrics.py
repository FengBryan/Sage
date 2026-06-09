from __future__ import annotations

import hashlib
import re
import sys
from typing import Callable, Protocol

from .protocol import CommandExecutionEvent, encode_json_line


class Reporter(Protocol):
    def report(self, event: CommandExecutionEvent) -> None:
        ...


_ASSIGNMENT_RE = re.compile(
    r"\b(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*=\s*"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s'\"]+)"
)
_SECRET_KEY_SEGMENTS = frozenset({"TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD"})
_NON_SECRET_KEYS = frozenset({"MONKEY"})


def command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", errors="ignore")).hexdigest()


def redact_command(command: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if not _is_secret_key(match.group("key")):
            return match.group(0)
        return f"{match.group('key')}=<redacted>"

    return _ASSIGNMENT_RE.sub(replace, command)


def _is_secret_key(key: str) -> bool:
    key_upper = key.upper()
    if key_upper in _NON_SECRET_KEYS:
        return False
    for segment in key_upper.split("_"):
        for secret in _SECRET_KEY_SEGMENTS:
            if segment == secret:
                return True
            if segment.startswith(secret) and segment[len(secret) :].isdigit():
                return True
    if key == key_upper:
        return any(key_upper.endswith(secret) for secret in _SECRET_KEY_SEGMENTS)
    return False


class JsonLogReporter:
    def __init__(self, write_line: Callable[[str], None] | None = None) -> None:
        self._write_line = write_line or self._default_write_line

    def report(self, event: CommandExecutionEvent) -> None:
        self._write_line(encode_json_line(event).rstrip("\n"))

    @staticmethod
    def _default_write_line(line: str) -> None:
        print(line, file=sys.stdout, flush=True)
