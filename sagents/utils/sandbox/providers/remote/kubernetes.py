"""Kubernetes 远程沙箱实现."""

import asyncio
import ast
import base64
import fnmatch
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import tarfile
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

from .base import RemoteSandboxProvider
from ...interface import CommandResult, ExecutionResult, FileInfo
from ...config import MountPath
from sagents.utils.logger import logger

try:
    from kubernetes import client as k8s_client  # pyright: ignore[reportMissingImports]
    from kubernetes import config as k8s_config  # pyright: ignore[reportMissingImports]
    from kubernetes import stream as k8s_stream  # pyright: ignore[reportMissingImports]
except ImportError:
    k8s_client = None
    k8s_config = None
    k8s_stream = None


class KubernetesSandboxProvider(RemoteSandboxProvider):
    """Kubernetes 远程沙箱实现"""

    def __init__(
        self,
        sandbox_id: str,
        namespace: str = "default",
        image: str = "python:3.11-slim",
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
        pod_command: Optional[List[str]] = None,
        pod_args: Optional[List[str]] = None,
    ):
        super().__init__(
            sandbox_id=sandbox_id,
            workspace_mount=workspace_mount,
            mount_paths=mount_paths,
            virtual_workspace=virtual_workspace,
            timeout=timeout,
        )
        self.namespace = namespace
        self.image = image
        self.resources = resources or {}
        self.service_account_name = service_account_name
        self.session_id = session_id
        self.pvc = pvc or {}
        self.pod_labels = pod_labels or {}
        self.pod_annotations = pod_annotations or {}
        self.pod_security_context = pod_security_context or {
            "runAsUser": 0,
            "runAsGroup": 0,
        }
        self.container_security_context = container_security_context or {}
        self.command_logging = command_logging or {"enabled": False, "redact": True}
        self.pod_command = (
            pod_command if pod_command is not None else ["sage-sandbox-metric-agent"]
        )
        self.pod_args = pod_args if pod_args is not None else []
        self._pod_name = self._pod_name_for_sandbox()
        self._k8s_client = None
        self._background_task_bg_dirs: Dict[str, str] = {}

    def _load_kubernetes_config(self) -> None:
        if k8s_config is None or k8s_client is None:
            raise ImportError(
                "kubernetes package is required. Install with: pip install kubernetes"
            )

        k8s_config.load_incluster_config()
        self._k8s_client = k8s_client.CoreV1Api()

    def _pod_name_for_sandbox(self) -> str:
        prefix = "sage-sandbox-"
        safe = self._sanitize_dns_fragment(self._sandbox_id)
        if len(prefix + safe) <= 63:
            return f"{prefix}{safe}"

        digest = hashlib.sha256(self._sandbox_id.encode("utf-8")).hexdigest()[:10]
        max_safe_length = 63 - len(prefix) - len(digest) - 1
        truncated = safe[:max_safe_length].strip("-") or "sandbox"
        return f"{prefix}{truncated}-{digest}"

    def _sanitize_dns_fragment(self, value: str) -> str:
        safe = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
        return safe or "sandbox"

    def _label_value_for_sandbox(self) -> str:
        safe = self._sanitize_dns_fragment(self._sandbox_id)
        if len(safe) <= 63:
            return safe

        digest = hashlib.sha256(self._sandbox_id.encode("utf-8")).hexdigest()[:10]
        max_safe_length = 63 - len(digest) - 1
        truncated = safe[:max_safe_length].strip("-") or "sandbox"
        return f"{truncated}-{digest}"

    def _render_sub_path(self) -> Optional[str]:
        template = self.pvc.get("sub_path_template")
        if not template:
            return None
        if "{session_id}" in template and not self.session_id:
            raise ValueError(
                "remote_provider_config.session_id is required for pvc.sub_path_template"
            )
        return template.format(session_id=self.session_id)

    def _validate_pod_config(self) -> None:
        if not self.namespace:
            raise ValueError("remote_provider_config.namespace is required")
        if not self.image:
            raise ValueError("remote_image is required for kubernetes sandbox")
        if not self.pvc or not self.pvc.get("claim_name"):
            raise ValueError("remote_provider_config.pvc.claim_name is required")

    def _build_pod_manifest(self) -> Dict[str, Any]:
        self._validate_pod_config()

        mount_path = self.pvc.get("mount_path") or self._workspace_path
        volume_mount: Dict[str, Any] = {"name": "workspace", "mountPath": mount_path}
        sub_path = self._render_sub_path()
        if sub_path:
            volume_mount["subPath"] = sub_path

        container: Dict[str, Any] = {
            "name": "sandbox",
            "image": self.image,
            "imagePullPolicy": "IfNotPresent",
            "command": self.pod_command,
            "args": self.pod_args,
            "volumeMounts": [volume_mount],
            "resources": self.resources,
            "securityContext": self.container_security_context,
        }

        labels = {
            **self.pod_labels,
            "app": "sage-sandbox",
            "sandbox-id": self._label_value_for_sandbox(),
        }
        annotations = {
            **self.pod_annotations,
            "sage.dev/sandbox-id": self._sandbox_id,
        }

        manifest: Dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": self._pod_name,
                "namespace": self.namespace,
                "labels": labels,
                "annotations": annotations,
            },
            "spec": {
                "restartPolicy": "Never",
                "securityContext": self.pod_security_context,
                "containers": [container],
                "volumes": [
                    {
                        "name": "workspace",
                        "persistentVolumeClaim": {
                            "claimName": self.pvc["claim_name"],
                        },
                    }
                ],
            },
        }

        if self.service_account_name:
            manifest["spec"]["serviceAccountName"] = self.service_account_name

        return manifest

    def _is_not_found(self, value: Any) -> bool:
        return getattr(value, "status", None) == 404

    def _b64(self, text: str) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    def _parse_runner_json(
        self,
        raw: str,
        required_keys: set[str],
        *additional_key_sets: set[str],
    ) -> Dict[str, Any]:
        key_sets = (required_keys, *additional_key_sets)
        for line in reversed(raw.strip().splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and any(
                keys.issubset(payload.keys()) for keys in key_sets
            ):
                return payload
        raise RuntimeError(f"Invalid sage-shell-runner response: {raw[:500]}")

    def _require_runner_ok(self, raw: str) -> None:
        payload = self._parse_runner_json(raw, {"ok"})
        if payload.get("ok") is not True:
            raise RuntimeError(f"sage-shell-runner returned unsuccessful response: {raw[:500]}")

    def _runner_response_to_text(self, value: Any) -> str:
        data = getattr(value, "data", value)
        if isinstance(data, tuple) and len(data) == 1:
            return self._runner_response_to_text(data[0])
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        if isinstance(data, str):
            stripped = data.strip()
            if stripped.startswith(("{'", "['")):
                try:
                    parsed = ast.literal_eval(stripped)
                except (ValueError, SyntaxError):
                    return data
                if isinstance(parsed, (dict, list)):
                    return json.dumps(parsed, ensure_ascii=False) + "\n"
            return data
        if isinstance(data, (dict, list)):
            return json.dumps(data, ensure_ascii=False) + "\n"
        return str(data)

    def _is_ignored_tar_path(
        self,
        name: str,
        rel_path: str,
        ignore_patterns: List[str],
    ) -> bool:
        return any(
            fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel_path, pattern)
            for pattern in ignore_patterns
        )

    def _make_tar_bytes(
        self,
        host_source_path: str,
        ignore_patterns: Optional[List[str]] = None,
        arcname: Optional[str] = None,
    ) -> bytes:
        ignore_patterns = ignore_patterns or []
        source_path = os.path.abspath(host_source_path)
        buffer = io.BytesIO()

        with tarfile.open(fileobj=buffer, mode="w") as tar:
            if os.path.isfile(source_path) and not os.path.islink(source_path):
                basename = arcname or os.path.basename(source_path)
                if not self._is_ignored_tar_path(basename, basename, ignore_patterns):
                    tar.add(source_path, arcname=basename, recursive=False)
                return buffer.getvalue()

            if not os.path.isdir(source_path):
                return buffer.getvalue()

            for root, dirs, files in os.walk(source_path):
                rel_root = os.path.relpath(root, source_path)
                if rel_root == ".":
                    rel_root = ""

                kept_dirs = []
                for directory in dirs:
                    full_dir = os.path.join(root, directory)
                    rel_dir = os.path.join(rel_root, directory) if rel_root else directory
                    rel_dir = rel_dir.replace(os.sep, "/")
                    if os.path.islink(full_dir):
                        continue
                    if self._is_ignored_tar_path(directory, rel_dir, ignore_patterns):
                        continue
                    kept_dirs.append(directory)
                    tar.add(full_dir, arcname=rel_dir, recursive=False)
                dirs[:] = kept_dirs

                for file_name in files:
                    full_file = os.path.join(root, file_name)
                    rel_file = os.path.join(rel_root, file_name) if rel_root else file_name
                    rel_file = rel_file.replace(os.sep, "/")
                    if os.path.islink(full_file):
                        continue
                    if self._is_ignored_tar_path(file_name, rel_file, ignore_patterns):
                        continue
                    tar.add(full_file, arcname=rel_file, recursive=False)

        return buffer.getvalue()

    def _close_runner_stdin(self, ws: Any) -> None:
        close_channel = getattr(ws, "close_channel", None)
        if close_channel is not None:
            close_channel(0)
            return

        close_stdin = getattr(ws, "close_stdin", None)
        if close_stdin is not None:
            close_stdin()

    async def _exec_runner(
        self,
        args: List[str],
        stdin_data: Optional[bytes] = None,
        timeout: int = 30,
    ) -> str:
        if not self._is_initialized:
            await self.initialize()
        if k8s_stream is None:
            raise ImportError(
                "kubernetes package is required. Install with: pip install kubernetes"
            )

        command = ["sage-shell-runner", *args]

        if stdin_data is None:
            response = await asyncio.to_thread(
                k8s_stream.stream,
                self._k8s_client.connect_get_namespaced_pod_exec,  # pyright: ignore[reportOptionalMemberAccess]
                self._pod_name,
                self.namespace,
                command=command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _request_timeout=timeout,
            )
            return self._runner_response_to_text(response)

        def _stream_with_stdin() -> str:
            ws = k8s_stream.stream(
                self._k8s_client.connect_get_namespaced_pod_exec,  # pyright: ignore[reportOptionalMemberAccess]
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
            stdout_chunks: List[str] = []
            stderr_chunks: List[str] = []
            deadline = time.monotonic() + timeout
            try:
                ws.write_stdin(stdin_data)
                self._close_runner_stdin(ws)
                while ws.is_open():
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"sage-shell-runner exec timed out after {timeout} seconds"
                        )
                    ws.update(timeout=1)
                    if ws.peek_stdout():
                        stdout_chunks.append(ws.read_stdout())
                    if ws.peek_stderr():
                        stderr_chunks.append(ws.read_stderr())
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"sage-shell-runner exec timed out after {timeout} seconds"
                        )
            finally:
                ws.close()
            if stderr_chunks and not stdout_chunks:
                raise RuntimeError(
                    "Kubernetes runner exec failed: " + "".join(stderr_chunks)
                )
            return "".join(stdout_chunks)

        return await asyncio.to_thread(_stream_with_stdin)

    def _pod_phase(self, pod: Any) -> Optional[str]:
        return getattr(getattr(pod, "status", None), "phase", None)

    def _pod_is_terminal(self, pod: Any) -> bool:
        return self._pod_phase(pod) in {"Succeeded", "Failed"}

    def _pod_label_matches(self, pod: Any) -> bool:
        labels = getattr(getattr(pod, "metadata", None), "labels", None) or {}
        annotations = getattr(getattr(pod, "metadata", None), "annotations", None) or {}
        existing_raw_id = annotations.get("sage.dev/sandbox-id")
        if existing_raw_id is not None:
            return existing_raw_id == self._sandbox_id

        existing_label_id = labels.get("sandbox-id")
        if existing_label_id is not None:
            return existing_label_id in {
                self._label_value_for_sandbox(),
                self._sandbox_id,
            }

        return False

    def _pod_is_ready(self, pod: Any) -> bool:
        if self._pod_phase(pod) != "Running":
            return False
        status = getattr(pod, "status", None)
        for condition in getattr(status, "conditions", []) or []:
            if (
                getattr(condition, "type", None) == "Ready"
                and getattr(condition, "status", None) == "True"
            ):
                return True
        return False

    def _read_pod(self) -> Any:
        try:
            return self._k8s_client.read_namespaced_pod(  # pyright: ignore[reportOptionalMemberAccess]
                name=self._pod_name,
                namespace=self.namespace,
            )
        except Exception as exc:
            if self._is_not_found(exc):
                return None
            raise

    async def _read_pod_async(self) -> Any:
        try:
            pod = await asyncio.to_thread(
                self._k8s_client.read_namespaced_pod,  # pyright: ignore[reportOptionalMemberAccess]
                name=self._pod_name,
                namespace=self.namespace,
            )
        except Exception as exc:
            if self._is_not_found(exc):
                return None
            raise
        if self._is_not_found(pod):
            return None
        return pod

    async def _create_pod_async(self) -> None:
        try:
            result = await asyncio.to_thread(
                self._k8s_client.create_namespaced_pod,  # pyright: ignore[reportOptionalMemberAccess]
                namespace=self.namespace,
                body=self._build_pod_manifest(),
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 409:
                await self._handle_create_conflict()
                return
            raise
        if getattr(result, "status", None) == 409:
            await self._handle_create_conflict()

    async def _handle_create_conflict(self) -> None:
        pod = await self._read_pod_async()
        if pod is None:
            await asyncio.to_thread(
                self._k8s_client.create_namespaced_pod,  # pyright: ignore[reportOptionalMemberAccess]
                namespace=self.namespace,
                body=self._build_pod_manifest(),
            )
            return
        if not self._pod_label_matches(pod):
            raise RuntimeError(
                f"Kubernetes sandbox pod {self._pod_name} has mismatched "
                "sandbox identity after create conflict"
            )

    async def _delete_pod_async(self) -> None:
        try:
            result = await asyncio.to_thread(
                self._k8s_client.delete_namespaced_pod,  # pyright: ignore[reportOptionalMemberAccess]
                name=self._pod_name,
                namespace=self.namespace,
            )
        except Exception as exc:
            if self._is_not_found(exc):
                return
            raise
        if self._is_not_found(result):
            return

    async def _wait_until_deleted(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.timeout.total_seconds()

        while True:
            pod = await self._read_pod_async()
            if pod is None:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"Kubernetes sandbox pod {self._pod_name} was not deleted "
                    "before timeout"
                )
            await asyncio.sleep(1)

    async def _wait_until_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.timeout.total_seconds()
        last_phase = "unknown"

        while True:
            pod = await self._read_pod_async()
            if pod is not None and self._pod_is_ready(pod):
                return
            if pod is not None:
                last_phase = self._pod_phase(pod) or "unknown"
                if self._pod_is_terminal(pod):
                    raise RuntimeError(
                        f"Kubernetes sandbox pod {self._pod_name} entered "
                        f"terminal phase {last_phase}"
                    )
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"Kubernetes sandbox pod {self._pod_name} was not Ready "
                    f"before timeout; last phase={last_phase}"
                )
            await asyncio.sleep(1)

    async def initialize(self) -> None:
        """在 K8s 中创建 Pod"""
        self._validate_pod_config()

        if self._k8s_client is None:
            self._load_kubernetes_config()

        existing_pod = await self._read_pod_async()

        if existing_pod is None:
            await self._create_pod_async()
        elif not self._pod_label_matches(existing_pod):
            raise RuntimeError(
                f"Kubernetes sandbox pod {self._pod_name} has mismatched "
                "sandbox identity: sandbox-id label/annotation"
            )
        elif self._pod_is_terminal(existing_pod):
            await self._delete_pod_async()
            await self._wait_until_deleted()
            await self._create_pod_async()
        elif self._pod_is_ready(existing_pod):
            self._is_initialized = True
            logger.info(f"KubernetesSandboxProvider: 复用 Pod {self._pod_name}")
            return

        await self._wait_until_ready()
        self._is_initialized = True

        logger.info(f"KubernetesSandboxProvider: Pod {self._pod_name} Ready")

    async def execute_command(
        self,
        command: str,
        workdir: Optional[str] = None,
        timeout: int = 30,
        env_vars: Optional[Dict[str, str]] = None,
        background: bool = False,
    ) -> CommandResult:
        """在 K8s Pod 中执行命令"""
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
        payload = self._parse_runner_json(
            raw,
            {"success", "stdout", "stderr", "exit_code"},
        )
        return CommandResult(
            success=bool(payload.get("success")),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
            return_code=int(payload.get("exit_code", 1)),
            execution_time=float(
                payload.get("duration_ms", int((time.monotonic() - started) * 1000))
            )
            / 1000.0,
        )

    async def execute_python(
        self,
        code: str,
        requirements: Optional[List[str]] = None,
        workdir: Optional[str] = None,
        timeout: int = 60,
    ) -> ExecutionResult:
        """执行 Python 代码"""
        code_b64 = self._b64(code)
        command = "python -c " + shlex.quote(
            "import base64; exec(base64.b64decode("
            + repr(code_b64)
            + ").decode('utf-8'))"
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
        """执行 JavaScript 代码"""
        code_b64 = self._b64(code)
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

    async def read_file(self, path: str, encoding: str = "utf-8") -> str:
        """读取文件"""
        raw = await self._exec_runner(
            ["file-read", "--path", path, "--encoding", encoding]
        )
        return str(self._parse_runner_json(raw, {"content"})["content"])

    async def write_file(
        self,
        path: str,
        content: str,
        encoding: str = "utf-8",
        mode: str = "overwrite",
    ) -> None:
        """写入文件"""
        data = base64.b64encode(content.encode(encoding)).decode("ascii")
        raw = await self._exec_runner(
            ["file-write", "--path", path, "--mode", mode, "--content-b64", data]
        )
        self._require_runner_ok(raw)

    async def file_exists(self, path: str) -> bool:
        """检查文件是否存在"""
        try:
            raw = await self._exec_runner(["file-stat", "--path", path])
            payload = self._parse_runner_json(
                raw,
                {"path", "is_file", "is_dir", "size", "modified_time"},
                {"error_type"},
            )
        except FileNotFoundError:
            return False
        if payload.get("error_type") in {"file_not_found", "not_found"}:
            return False
        return True

    async def list_directory(
        self,
        path: str,
        include_hidden: bool = False,
    ) -> List[FileInfo]:
        """列出目录内容"""
        args = ["file-list", "--path", path]
        if include_hidden:
            args.append("--include-hidden")
        raw = await self._exec_runner(args)
        entries = self._parse_runner_json(raw, {"entries"}).get("entries", [])
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
        """返回文件修改时间。"""
        try:
            raw = await self._exec_runner(["file-stat", "--path", path])
            payload = self._parse_runner_json(
                raw,
                {"path", "is_file", "is_dir", "size", "modified_time"},
                {"error_type"},
            )
            if payload.get("error_type") in {"file_not_found", "not_found"}:
                return 0
            return float(payload.get("modified_time", 0))
        except FileNotFoundError:
            return 0

    async def ensure_directory(self, path: str) -> None:
        """确保目录存在"""
        raw = await self._exec_runner(["file-mkdir", "--path", path])
        self._require_runner_ok(raw)

    async def delete_file(self, path: str) -> None:
        """删除文件"""
        raw = await self._exec_runner(["file-delete", "--path", path])
        self._require_runner_ok(raw)

    async def copy_from_host(
        self,
        host_source_path: str,
        sandbox_dest_path: str,
        ignore_patterns: Optional[List[str]] = None,
    ) -> bool:
        """Copy a host file or directory into the Kubernetes sandbox via tar stdin."""
        if not await asyncio.to_thread(os.path.exists, host_source_path):
            return False

        path_is_file = await asyncio.to_thread(os.path.isfile, host_source_path)
        extract_dest_path = sandbox_dest_path
        arcname = None
        if path_is_file:
            normalized_dest = posixpath.normpath(sandbox_dest_path)
            arcname = posixpath.basename(normalized_dest) or os.path.basename(
                host_source_path
            )
            extract_dest_path = posixpath.dirname(normalized_dest) or "."

        tar_bytes = await asyncio.to_thread(
            self._make_tar_bytes,
            host_source_path,
            ignore_patterns,
            arcname,
        )
        raw = await self._exec_runner(
            [
                "tar-extract",
                "--dest",
                extract_dest_path,
                "--size-bytes",
                str(len(tar_bytes)),
            ],
            stdin_data=tar_bytes,
        )
        self._require_runner_ok(raw)
        return True

    def _bg_dir(self, log_dir: Optional[str] = None) -> str:
        return log_dir or f"{self._workspace_path}/.sage/bg"

    def _bg_dir_for_task(self, task_id: str) -> str:
        return self._background_task_bg_dirs.get(task_id, self._bg_dir())

    def _command_with_env_prefix(
        self,
        command: str,
        env_vars: Optional[Dict[str, str]],
    ) -> str:
        if not env_vars:
            return command

        assignments = []
        for key, value in env_vars.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"Invalid environment variable name: {key}")
            assignments.append(f"{key}={shlex.quote(str(value))}")
        return " ".join([*assignments, command])

    def supports_background(self) -> bool:
        return True

    async def start_background(
        self,
        command: str,
        workdir: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        log_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start a runner-managed background command."""
        bg_dir = self._bg_dir(log_dir)
        raw = await self._exec_runner(
            [
                "bg-start",
                "--command-b64",
                self._b64(self._command_with_env_prefix(command, env_vars)),
                "--workdir",
                workdir or self._workspace_path,
                "--bg-dir",
                bg_dir,
            ]
        )
        payload = self._parse_runner_json(
            raw,
            {"task_id", "pid", "log_path", "exit_path"},
        )
        self._background_task_bg_dirs[str(payload["task_id"])] = bg_dir
        return payload

    async def read_background_output(self, task_id: str, max_bytes: int = 8192) -> str:
        raw = await self._exec_runner(
            [
                "bg-read",
                "--task-id",
                task_id,
                "--bg-dir",
                self._bg_dir_for_task(task_id),
                "--max-bytes",
                str(max_bytes),
            ]
        )
        return str(self._parse_runner_json(raw, {"text"})["text"])

    async def read_background_output_range(
        self, task_id: str, offset: int = 0, max_bytes: int = 1 << 20
    ) -> tuple[str, int]:
        raw = await self._exec_runner(
            [
                "bg-read-range",
                "--task-id",
                task_id,
                "--bg-dir",
                self._bg_dir_for_task(task_id),
                "--offset",
                str(offset),
                "--max-bytes",
                str(max_bytes),
            ]
        )
        payload = self._parse_runner_json(raw, {"text", "offset"})
        return str(payload["text"]), int(payload["offset"])

    async def get_background_output_size(self, task_id: str) -> Optional[int]:
        raw = await self._exec_runner(
            [
                "bg-size",
                "--task-id",
                task_id,
                "--bg-dir",
                self._bg_dir_for_task(task_id),
            ]
        )
        return int(self._parse_runner_json(raw, {"size"})["size"])

    async def is_background_alive(self, task_id: str) -> bool:
        raw = await self._exec_runner(
            [
                "bg-state",
                "--task-id",
                task_id,
                "--bg-dir",
                self._bg_dir_for_task(task_id),
            ]
        )
        return bool(self._parse_runner_json(raw, {"alive", "exit_code"})["alive"])

    async def get_background_exit_code(self, task_id: str) -> Optional[int]:
        raw = await self._exec_runner(
            [
                "bg-state",
                "--task-id",
                task_id,
                "--bg-dir",
                self._bg_dir_for_task(task_id),
            ]
        )
        payload = self._parse_runner_json(raw, {"alive", "exit_code"})
        exit_code = payload.get("exit_code")
        return None if exit_code is None else int(exit_code)

    async def kill_background(self, task_id: str, force: bool = False) -> bool:
        args = [
            "bg-kill",
            "--task-id",
            task_id,
            "--bg-dir",
            self._bg_dir_for_task(task_id),
        ]
        if force:
            args.append("--force")
        raw = await self._exec_runner(args)
        return bool(self._parse_runner_json(raw, {"ok"})["ok"])

    async def cleanup_background(self, task_id: str) -> None:
        self._background_task_bg_dirs.pop(task_id, None)
        return None

    async def cleanup(self) -> None:
        """释放本地状态，不删除 Kubernetes 资源。"""
        self._is_initialized = False

    async def kill(self) -> None:
        """强制删除沙箱"""
        if self._k8s_client is None:
            self._load_kubernetes_config()

        if self._pod_name:
            await self._delete_pod_async()
            logger.info(f"KubernetesSandboxProvider: 删除 Pod {self._pod_name}")

        self._is_initialized = False
