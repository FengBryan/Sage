import asyncio
import base64
import io
import json
import shlex
import tarfile
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from sagents.utils.sandbox.config import SandboxConfig
from sagents.utils.sandbox.factory import SandboxProviderFactory
from sagents.utils.sandbox.interface import SandboxType
from sagents.utils.sandbox.providers.remote.kubernetes import KubernetesSandboxProvider


def make_provider(**overrides):
    config = {
        "sandbox_id": "sandbox_ABC",
        "namespace": "sage",
        "image": "sage/sandbox-runtime:latest",
        "virtual_workspace": "/sage-workspace",
        "timeout": timedelta(seconds=30),
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi"},
        },
        "pvc": {
            "claim_name": "sage-sandbox-workspaces",
            "mount_path": "/sage-workspace",
            "sub_path_template": "sessions/{session_id}",
        },
        "session_id": "session-123",
        "pod_labels": {"sage.dev/session": "session-123"},
        "pod_annotations": {"sage.dev/owner": "tests"},
    }
    config.update(overrides)
    return KubernetesSandboxProvider(**config)


def ready_pod(name="sage-sandbox-sandbox-abc"):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels={"sandbox-id": "sandbox-abc"},
            annotations={"sage.dev/sandbox-id": "sandbox_ABC"},
        ),
        status=SimpleNamespace(
            phase="Running",
            conditions=[
                SimpleNamespace(type="Ready", status="True"),
            ],
        ),
    )


def terminal_pod(phase="Failed", name="sage-sandbox-sandbox-abc"):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels={"sandbox-id": "sandbox-abc"},
            annotations={"sage.dev/sandbox-id": "sandbox_ABC"},
        ),
        status=SimpleNamespace(phase=phase, conditions=[]),
    )


def not_found_error():
    return SimpleNamespace(status=404, reason="Not Found")


def conflict_error():
    return SimpleNamespace(status=409, reason="Already Exists")


def tar_entries(tar_bytes):
    result = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as tar:
        for member in tar.getmembers():
            if member.isfile():
                extracted = tar.extractfile(member)
                result[member.name] = extracted.read() if extracted is not None else b""
            else:
                result[member.name] = None
    return result


def terminating_pod(name="sage-sandbox-sandbox-abc"):
    pod = terminal_pod("Failed", name)
    pod.metadata.deletionTimestamp = "2026-06-01T00:00:00Z"
    return pod


def test_load_kubernetes_config_uses_incluster_only_and_initializes_core_v1(
    monkeypatch,
):
    import sagents.utils.sandbox.providers.remote.kubernetes as module

    loaded = []
    kube_config = SimpleNamespace(
        load_incluster_config=lambda: loaded.append("incluster"),
        load_kube_config=lambda: loaded.append("kubeconfig"),
    )
    core_v1 = Mock(name="core-v1")

    monkeypatch.setattr(module, "k8s_config", kube_config)
    monkeypatch.setattr(module, "k8s_client", SimpleNamespace(CoreV1Api=lambda: core_v1))

    provider = make_provider()
    provider._load_kubernetes_config()

    assert loaded == ["incluster"]
    assert provider._k8s_client is core_v1


def test_sub_path_template_requires_session_id():
    with pytest.raises(ValueError, match="session_id"):
        make_provider(session_id=None)._render_sub_path()


def test_build_pod_manifest_uses_pvc_subpath_security_resources_labels_and_annotations():
    provider = make_provider(service_account_name="sage-sandbox-runner")

    manifest = provider._build_pod_manifest()
    container = manifest["spec"]["containers"][0]

    assert manifest["metadata"]["name"] == "sage-sandbox-sandbox-abc"
    assert manifest["metadata"]["namespace"] == "sage"
    assert manifest["metadata"]["labels"] == {
        "app": "sage-sandbox",
        "sandbox-id": "sandbox-abc",
        "sage.dev/session": "session-123",
    }
    assert manifest["metadata"]["annotations"] == {
        "sage.dev/owner": "tests",
        "sage.dev/sandbox-id": "sandbox_ABC",
    }
    assert manifest["spec"]["serviceAccountName"] == "sage-sandbox-runner"
    assert manifest["spec"]["securityContext"] == {"runAsUser": 0, "runAsGroup": 0}
    assert manifest["spec"]["restartPolicy"] == "Never"
    assert container["image"] == "sage/sandbox-runtime:latest"
    assert container["command"] == ["sage-sandbox-metric-agent"]
    assert container["args"] == []
    assert container["resources"]["requests"]["cpu"] == "100m"
    assert container["securityContext"] == {}
    assert container["volumeMounts"][0] == {
        "name": "workspace",
        "mountPath": "/sage-workspace",
        "subPath": "sessions/session-123",
    }
    assert manifest["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == (
        "sage-sandbox-workspaces"
    )


def test_pod_manifest_allows_command_and_args_override():
    provider = make_provider(
        pod_command=["/bin/sh", "-c"],
        pod_args=["sleep infinity"],
    )

    container = provider._build_pod_manifest()["spec"]["containers"][0]

    assert container["command"] == ["/bin/sh", "-c"]
    assert container["args"] == ["sleep infinity"]


def test_long_sandbox_id_uses_valid_label_and_raw_annotation():
    raw_sandbox_id = "Session__ABC.123_very_LONG_" * 4
    provider = make_provider(sandbox_id=raw_sandbox_id)

    metadata = provider._build_pod_manifest()["metadata"]

    assert len(metadata["labels"]["sandbox-id"]) <= 63
    assert metadata["labels"]["sandbox-id"] == provider._label_value_for_sandbox()
    assert metadata["annotations"]["sage.dev/sandbox-id"] == raw_sandbox_id


def test_reserved_labels_override_user_pod_labels():
    provider = make_provider(
        pod_labels={
            "app": "not-sage",
            "sandbox-id": "not-this-sandbox",
            "custom": "value",
        }
    )

    labels = provider._build_pod_manifest()["metadata"]["labels"]

    assert labels["app"] == "sage-sandbox"
    assert labels["sandbox-id"] == "sandbox-abc"
    assert labels["custom"] == "value"


def test_pod_name_is_dns_safe_and_deterministic():
    provider = make_provider(sandbox_id="Session__ABC.123_very_LONG_" * 4)

    first = provider._pod_name_for_sandbox()
    second = provider._pod_name_for_sandbox()

    assert first == second
    assert first.startswith("sage-sandbox-session-abc-123-very-long")
    assert first == first.lower()
    assert "_" not in first
    assert "." not in first
    assert len(first) <= 63


def test_long_pod_names_include_hash_suffix_to_avoid_collisions():
    prefix = "Session__ABC.123_very_LONG_" * 4
    first = make_provider(sandbox_id=prefix + "one")._pod_name_for_sandbox()
    second = make_provider(sandbox_id=prefix + "two")._pod_name_for_sandbox()

    assert first != second
    assert first.startswith("sage-sandbox-session-abc-123-very-long")
    assert second.startswith("sage-sandbox-session-abc-123-very-long")
    assert len(first) <= 63
    assert len(second) <= 63


def test_initialize_reuses_ready_pod(monkeypatch):
    provider = make_provider()
    client = Mock()
    client.read_namespaced_pod.return_value = ready_pod(provider._pod_name)
    monkeypatch.setattr(provider, "_load_kubernetes_config", lambda: None)
    provider._k8s_client = client

    asyncio.run(provider.initialize())

    client.read_namespaced_pod.assert_called_once_with(
        name=provider._pod_name,
        namespace="sage",
    )
    client.create_namespaced_pod.assert_not_called()
    assert provider._is_initialized is True


def test_initialize_accepts_legacy_raw_sandbox_id_label_without_annotation(
    monkeypatch,
):
    provider = make_provider()
    client = Mock()
    pod = ready_pod(provider._pod_name)
    pod.metadata.labels = {"sandbox-id": "sandbox_ABC"}
    pod.metadata.annotations = {}
    client.read_namespaced_pod.return_value = pod
    monkeypatch.setattr(provider, "_load_kubernetes_config", lambda: None)
    provider._k8s_client = client

    asyncio.run(provider.initialize())

    client.create_namespaced_pod.assert_not_called()
    assert provider._is_initialized is True


def test_initialize_rejects_ready_pod_with_mismatched_sandbox_label(monkeypatch):
    provider = make_provider()
    client = Mock()
    pod = ready_pod(provider._pod_name)
    pod.metadata.labels = {"sandbox-id": "someone-else"}
    pod.metadata.annotations = {"sage.dev/sandbox-id": "someone-else"}
    client.read_namespaced_pod.return_value = pod
    monkeypatch.setattr(provider, "_load_kubernetes_config", lambda: None)
    provider._k8s_client = client

    with pytest.raises(RuntimeError, match="sandbox-id label"):
        asyncio.run(provider.initialize())

    client.create_namespaced_pod.assert_not_called()


def test_initialize_rejects_ready_pod_with_missing_identity(monkeypatch):
    provider = make_provider()
    client = Mock()
    pod = ready_pod(provider._pod_name)
    pod.metadata.labels = {"app": "sage-sandbox"}
    pod.metadata.annotations = {}
    client.read_namespaced_pod.return_value = pod
    monkeypatch.setattr(provider, "_load_kubernetes_config", lambda: None)
    provider._k8s_client = client

    with pytest.raises(RuntimeError, match="sandbox identity"):
        asyncio.run(provider.initialize())

    client.create_namespaced_pod.assert_not_called()


def test_initialize_creates_pod_when_not_found_and_waits_until_ready(monkeypatch):
    provider = make_provider()
    client = Mock()
    client.read_namespaced_pod.side_effect = [
        not_found_error(),
        ready_pod(provider._pod_name),
    ]
    client.create_namespaced_pod.return_value = ready_pod(provider._pod_name)
    monkeypatch.setattr(provider, "_load_kubernetes_config", lambda: None)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    provider._k8s_client = client

    asyncio.run(provider.initialize())

    client.create_namespaced_pod.assert_called_once()
    created = client.create_namespaced_pod.call_args.kwargs["body"]
    assert created["metadata"]["name"] == provider._pod_name
    assert provider._is_initialized is True


def test_initialize_reuses_matching_pod_after_create_conflict(monkeypatch):
    provider = make_provider()
    client = Mock()
    client.read_namespaced_pod.side_effect = [
        not_found_error(),
        ready_pod(provider._pod_name),
        ready_pod(provider._pod_name),
    ]
    client.create_namespaced_pod.return_value = conflict_error()
    wait_until_deleted = AsyncMock()
    monkeypatch.setattr(provider, "_load_kubernetes_config", lambda: None)
    monkeypatch.setattr(provider, "_wait_until_deleted", wait_until_deleted)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    provider._k8s_client = client

    asyncio.run(provider.initialize())

    client.create_namespaced_pod.assert_called_once()
    wait_until_deleted.assert_not_called()
    assert provider._is_initialized is True


def test_initialize_create_conflict_with_mismatched_pod_raises(monkeypatch):
    provider = make_provider()
    client = Mock()
    mismatched = ready_pod(provider._pod_name)
    mismatched.metadata.annotations = {"sage.dev/sandbox-id": "someone-else"}
    mismatched.metadata.labels = {"sandbox-id": "someone-else"}
    client.read_namespaced_pod.side_effect = [not_found_error(), mismatched]
    client.create_namespaced_pod.return_value = conflict_error()
    monkeypatch.setattr(provider, "_load_kubernetes_config", lambda: None)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    provider._k8s_client = client

    with pytest.raises(RuntimeError, match="sandbox identity"):
        asyncio.run(provider.initialize())

    client.create_namespaced_pod.assert_called_once()


def test_initialize_deletes_and_recreates_terminal_existing_pod(monkeypatch):
    provider = make_provider()
    client = Mock()
    client.read_namespaced_pod.side_effect = [
        terminal_pod("Failed", provider._pod_name),
        not_found_error(),
        ready_pod(provider._pod_name),
    ]
    monkeypatch.setattr(provider, "_load_kubernetes_config", lambda: None)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    provider._k8s_client = client

    asyncio.run(provider.initialize())

    client.delete_namespaced_pod.assert_called_once_with(
        name=provider._pod_name,
        namespace="sage",
    )
    client.create_namespaced_pod.assert_called_once()
    assert provider._is_initialized is True


def test_initialize_waits_for_terminal_pod_delete_before_recreate(monkeypatch):
    provider = make_provider()
    client = Mock()
    client.read_namespaced_pod.side_effect = [
        terminal_pod("Failed", provider._pod_name),
        terminating_pod(provider._pod_name),
        not_found_error(),
        ready_pod(provider._pod_name),
    ]
    monkeypatch.setattr(provider, "_load_kubernetes_config", lambda: None)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    provider._k8s_client = client

    asyncio.run(provider.initialize())

    client.delete_namespaced_pod.assert_called_once()
    client.create_namespaced_pod.assert_called_once()
    assert client.read_namespaced_pod.call_count == 4
    assert provider._is_initialized is True


def test_async_lifecycle_wraps_kubernetes_calls_with_to_thread(monkeypatch):
    provider = make_provider()
    client = Mock()
    client.read_namespaced_pod.side_effect = [
        not_found_error(),
        ready_pod(provider._pod_name),
    ]
    provider._k8s_client = client
    monkeypatch.setattr(provider, "_load_kubernetes_config", lambda: None)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    wrapped = []

    async def fake_to_thread(func, *args, **kwargs):
        wrapped.append(getattr(func, "__name__", getattr(func, "_mock_name", "")))
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    asyncio.run(provider.initialize())

    assert "read_namespaced_pod" in wrapped
    assert "create_namespaced_pod" in wrapped


def test_execute_command_wraps_stream_call_with_to_thread(monkeypatch):
    import sagents.utils.sandbox.providers.remote.kubernetes as module

    provider = make_provider()
    provider._is_initialized = True
    provider._k8s_client = SimpleNamespace(connect_get_namespaced_pod_exec=Mock())
    stream_fn = Mock(
        name="stream",
        return_value=json.dumps(
            {
                "success": True,
                "stdout": "hello\n",
                "stderr": "",
                "exit_code": 0,
                "duration_ms": 25,
                "timeout": False,
            }
        )
        + "\n",
    )
    monkeypatch.setattr(module, "k8s_stream", SimpleNamespace(stream=stream_fn))
    wrapped = []

    async def fake_to_thread(func, *args, **kwargs):
        wrapped.append(getattr(func, "__name__", getattr(func, "_mock_name", "")))
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    result = asyncio.run(provider.execute_command("echo hello"))

    assert result.stdout == "hello\n"
    assert stream_fn.call_args.kwargs["command"][:2] == ["sage-shell-runner", "run"]
    assert "stream" in wrapped


def test_runner_response_converts_to_command_result(monkeypatch):
    provider = make_provider()

    async def fake_exec(args, stdin_data=None, timeout=30):
        return (
            "metric line\n"
            + json.dumps(
                {
                    "success": False,
                    "stdout": "out",
                    "stderr": "err",
                    "exit_code": 7,
                    "duration_ms": 1250,
                    "timeout": False,
                }
            )
        )

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    result = asyncio.run(
        provider.execute_command("echo hi", workdir="/sage-workspace", timeout=3)
    )

    assert result.success is False
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.return_code == 7
    assert result.execution_time == 1.25


def test_exec_runner_normalizes_preloaded_stream_dict_response(monkeypatch):
    import sagents.utils.sandbox.providers.remote.kubernetes as module

    provider = make_provider()
    provider._is_initialized = True
    provider._k8s_client = SimpleNamespace(connect_get_namespaced_pod_exec=Mock())
    stream_fn = Mock(
        name="stream",
        return_value=(
            {
                "task_id": "shtask_mock",
                "pid": 4321,
                "log_path": "/sage-workspace/bg/shtask_mock.log",
                "exit_path": "/sage-workspace/bg/shtask_mock.exit",
            },
        ),
    )
    monkeypatch.setattr(module, "k8s_stream", SimpleNamespace(stream=stream_fn))

    result = asyncio.run(
        provider.start_background(
            "printf hello",
            log_dir="/sage-workspace/bg",
        )
    )

    assert result["task_id"] == "shtask_mock"
    assert result["pid"] == 4321


@pytest.mark.parametrize(
    "stream_response",
    [
        {
            "success": True,
            "stdout": "dict\n",
            "stderr": "",
            "exit_code": 0,
            "duration_ms": 11,
            "timeout": False,
        },
        (
            "{'success': True, 'stdout': 'literal\\n', 'stderr': '', "
            "'exit_code': 0, 'duration_ms': 12, 'timeout': False}"
        ),
        SimpleNamespace(
            data={
                "success": True,
                "stdout": "data attr\n",
                "stderr": "",
                "exit_code": 0,
                "duration_ms": 13,
                "timeout": False,
            }
        ),
    ],
)
def test_execute_command_normalizes_preloaded_stream_response_shapes(
    monkeypatch,
    stream_response,
):
    import sagents.utils.sandbox.providers.remote.kubernetes as module

    provider = make_provider()
    provider._is_initialized = True
    provider._k8s_client = SimpleNamespace(connect_get_namespaced_pod_exec=Mock())
    monkeypatch.setattr(
        module,
        "k8s_stream",
        SimpleNamespace(stream=Mock(name="stream", return_value=stream_response)),
    )

    result = asyncio.run(provider.execute_command("printf hello"))

    assert result.success is True
    assert result.stdout in {"dict\n", "literal\n", "data attr\n"}
    assert result.return_code == 0


def test_file_read_normalizes_preloaded_stream_data_attr_response(monkeypatch):
    import sagents.utils.sandbox.providers.remote.kubernetes as module

    provider = make_provider()
    provider._is_initialized = True
    provider._k8s_client = SimpleNamespace(connect_get_namespaced_pod_exec=Mock())
    monkeypatch.setattr(
        module,
        "k8s_stream",
        SimpleNamespace(
            stream=Mock(
                name="stream",
                return_value=SimpleNamespace(data={"content": "hello from data"}),
            )
        ),
    )

    assert asyncio.run(provider.read_file("/sage-workspace/a.txt")) == "hello from data"


def test_execute_command_ignores_trailing_metric_json_event(monkeypatch):
    provider = make_provider()
    command_response = {
        "success": True,
        "stdout": "command output",
        "stderr": "",
        "exit_code": 0,
        "duration_ms": 250,
        "timeout": False,
    }
    metric_event = {
        "event": "shell_execution_finished",
        "sandbox_id": "sandbox_ABC",
        "session_id": "session-123",
        "exit_code": 0,
        "duration_ms": 251,
    }

    async def fake_exec(args, stdin_data=None, timeout=30):
        return "\n".join([json.dumps(command_response), json.dumps(metric_event)])

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    result = asyncio.run(provider.execute_command("echo hi"))

    assert result.success is True
    assert result.stdout == "command output"
    assert result.stderr == ""
    assert result.return_code == 0
    assert result.execution_time == 0.25


def test_execute_command_passes_runner_arguments_as_base64(monkeypatch):
    provider = make_provider(command_logging={"enabled": True})
    captured = {}

    async def fake_exec(args, stdin_data=None, timeout=30):
        captured["args"] = args
        captured["timeout"] = timeout
        return json.dumps(
            {
                "success": True,
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "duration_ms": 10,
                "timeout": False,
            }
        )

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    asyncio.run(
        provider.execute_command(
            "echo $TOKEN",
            workdir="/tmp/work",
            timeout=9,
            env_vars={"TOKEN": "secret"},
        )
    )

    args = captured["args"]
    assert args[:2] == ["run", "--command-b64"]
    assert "echo $TOKEN" not in args
    assert base64.b64decode(args[args.index("--command-b64") + 1]).decode() == (
        "echo $TOKEN"
    )
    env_json = base64.b64decode(args[args.index("--env-json-b64") + 1]).decode()
    assert json.loads(env_json) == {"TOKEN": "secret"}
    assert "--session-id" in args
    assert "--log-command" in args
    assert captured["timeout"] == 14


def test_invalid_runner_json_raises_with_sample_output(monkeypatch):
    provider = make_provider()

    async def fake_exec(args, stdin_data=None, timeout=30):
        return "not-json\n"

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    with pytest.raises(RuntimeError, match="Invalid sage-shell-runner response: not-json"):
        asyncio.run(provider.execute_command("echo hi"))


def test_file_read_write_stat_and_directory_calls_runner(monkeypatch):
    provider = make_provider()
    calls = []

    async def fake_exec(args, stdin_data=None, timeout=30):
        calls.append(args)
        if args[0] == "file-read":
            return json.dumps({"content": "hello"})
        if args[0] == "file-stat":
            return json.dumps(
                {
                    "path": "/sage-workspace/a.txt",
                    "is_file": True,
                    "is_dir": False,
                    "size": 5,
                    "modified_time": 10.5,
                }
            )
        return json.dumps({"ok": True})

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    assert asyncio.run(provider.read_file("/sage-workspace/a.txt")) == "hello"
    asyncio.run(provider.write_file("/sage-workspace/a.txt", "secret"))
    assert asyncio.run(provider.file_exists("/sage-workspace/a.txt")) is True
    asyncio.run(provider.ensure_directory("/sage-workspace/dir"))
    asyncio.run(provider.delete_file("/sage-workspace/a.txt"))

    assert calls[0] == [
        "file-read",
        "--path",
        "/sage-workspace/a.txt",
        "--encoding",
        "utf-8",
    ]
    assert calls[1][:5] == [
        "file-write",
        "--path",
        "/sage-workspace/a.txt",
        "--mode",
        "overwrite",
    ]
    assert base64.b64decode(calls[1][-1]).decode() == "secret"
    assert calls[2] == ["file-stat", "--path", "/sage-workspace/a.txt"]
    assert calls[3] == ["file-mkdir", "--path", "/sage-workspace/dir"]
    assert calls[4] == ["file-delete", "--path", "/sage-workspace/a.txt"]


def test_file_mutations_require_ok_response(monkeypatch):
    provider = make_provider()
    calls = []

    async def fake_exec(args, stdin_data=None, timeout=30):
        calls.append(args)
        return json.dumps({"ok": True})

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    asyncio.run(provider.write_file("/sage-workspace/a.txt", "secret"))
    asyncio.run(provider.ensure_directory("/sage-workspace/dir"))
    asyncio.run(provider.delete_file("/sage-workspace/a.txt"))

    assert calls[0][0] == "file-write"
    assert calls[1] == ["file-mkdir", "--path", "/sage-workspace/dir"]
    assert calls[2] == ["file-delete", "--path", "/sage-workspace/a.txt"]


@pytest.mark.parametrize("raw", [json.dumps({"ok": False}), "not-json"])
def test_file_mutations_raise_on_failed_or_malformed_runner_response(
    monkeypatch,
    raw,
):
    provider = make_provider()

    async def fake_exec(args, stdin_data=None, timeout=30):
        return raw

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    with pytest.raises(RuntimeError, match="sage-shell-runner"):
        asyncio.run(provider.write_file("/sage-workspace/a.txt", "secret"))

    with pytest.raises(RuntimeError, match="sage-shell-runner"):
        asyncio.run(provider.ensure_directory("/sage-workspace/dir"))

    with pytest.raises(RuntimeError, match="sage-shell-runner"):
        asyncio.run(provider.delete_file("/sage-workspace/a.txt"))


def test_make_tar_bytes_archives_file_with_basename(tmp_path):
    provider = make_provider()
    source = tmp_path / "payload.bin"
    source.write_bytes(b"\x00\xffbinary")

    tar_bytes = provider._make_tar_bytes(str(source))

    assert tar_entries(tar_bytes) == {"payload.bin": b"\x00\xffbinary"}


def test_copy_from_host_file_calls_tar_extract_to_exact_destination(monkeypatch, tmp_path):
    provider = make_provider()
    source = tmp_path / "payload.txt"
    source.write_text("hello", encoding="utf-8")
    captured = {}

    async def fake_exec(args, stdin_data=None, timeout=30):
        captured["args"] = args
        captured["stdin_data"] = stdin_data
        captured["timeout"] = timeout
        return json.dumps({"ok": True})

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    copied = asyncio.run(
        provider.copy_from_host(str(source), "/sage-workspace/target.txt")
    )

    assert copied is True
    assert captured["args"] == [
        "tar-extract",
        "--dest",
        "/sage-workspace",
        "--size-bytes",
        str(len(captured["stdin_data"])),
    ]
    assert captured["timeout"] == 30
    assert tar_entries(captured["stdin_data"]) == {"target.txt": b"hello"}


def test_copy_from_host_directory_calls_tar_extract_with_size(monkeypatch, tmp_path):
    provider = make_provider()
    source = tmp_path / "project"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    nested = source / "nested"
    nested.mkdir()
    (nested / "b.txt").write_text("b", encoding="utf-8")
    captured = {}

    async def fake_exec(args, stdin_data=None, timeout=30):
        captured["args"] = args
        captured["stdin_data"] = stdin_data
        return json.dumps({"ok": True})

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    copied = asyncio.run(
        provider.copy_from_host(str(source), "/sage-workspace/project")
    )

    assert copied is True
    assert captured["args"] == [
        "tar-extract",
        "--dest",
        "/sage-workspace/project",
        "--size-bytes",
        str(len(captured["stdin_data"])),
    ]
    assert tar_entries(captured["stdin_data"]) == {
        "a.txt": b"a",
        "nested": None,
        "nested/b.txt": b"b",
    }


def test_copy_from_host_missing_source_returns_false_without_exec(monkeypatch, tmp_path):
    provider = make_provider()
    exec_runner = AsyncMock()
    monkeypatch.setattr(provider, "_exec_runner", exec_runner)

    copied = asyncio.run(provider.copy_from_host(str(tmp_path / "missing"), "/dest"))

    assert copied is False
    exec_runner.assert_not_called()


def test_make_tar_bytes_honors_ignore_patterns_for_files_and_dirs(tmp_path):
    provider = make_provider()
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("keep", encoding="utf-8")
    (source / "skip.pyc").write_text("skip", encoding="utf-8")
    ignored_dir = source / "__pycache__"
    ignored_dir.mkdir()
    (ignored_dir / "cached.pyc").write_text("cached", encoding="utf-8")
    nested = source / "nested"
    nested.mkdir()
    (nested / "keep.bin").write_bytes(b"\x00\x01")

    tar_bytes = provider._make_tar_bytes(
        str(source),
        ignore_patterns=["*.pyc", "__pycache__"],
    )

    entries = tar_entries(tar_bytes)
    assert entries == {
        "keep.txt": b"keep",
        "nested": None,
        "nested/keep.bin": b"\x00\x01",
    }


def test_make_tar_bytes_skips_symlinked_files_and_directories(tmp_path):
    provider = make_provider()
    source = tmp_path / "source"
    source.mkdir()
    real_dir = source / "real"
    real_dir.mkdir()
    (real_dir / "inside.txt").write_text("inside", encoding="utf-8")
    (source / "real.txt").write_text("real", encoding="utf-8")
    (source / "file-link").symlink_to(source / "real.txt")
    (source / "dir-link").symlink_to(real_dir, target_is_directory=True)

    tar_bytes = provider._make_tar_bytes(str(source))

    assert tar_entries(tar_bytes) == {
        "real": None,
        "real/inside.txt": b"inside",
        "real.txt": b"real",
    }


def test_exec_runner_stdin_reads_response_without_close_stdin(monkeypatch):
    import sagents.utils.sandbox.providers.remote.kubernetes as module

    class WebSocketWithoutCloseStdin:
        def __init__(self):
            self.closed = False
            self.stdin_closed = False
            self.stdin = None

        def write_stdin(self, data):
            self.stdin = data

        def close_channel(self, channel):
            assert channel == 0
            self.stdin_closed = True

        def is_open(self):
            return not self.closed

        def update(self, timeout=1):
            pass

        def peek_stdout(self):
            return self.stdin_closed

        def read_stdout(self):
            self.closed = True
            return json.dumps({"ok": True}) + "\n"

        def peek_stderr(self):
            return False

        def close(self):
            self.closed = True

    ws = WebSocketWithoutCloseStdin()
    provider = make_provider()
    provider._is_initialized = True
    provider._k8s_client = SimpleNamespace(connect_get_namespaced_pod_exec=Mock())
    monkeypatch.setattr(
        module,
        "k8s_stream",
        SimpleNamespace(stream=Mock(name="stream", return_value=ws)),
    )

    result = asyncio.run(provider._exec_runner(["file-write"], stdin_data=b"payload"))

    assert result == json.dumps({"ok": True}) + "\n"
    assert ws.stdin == b"payload"
    assert ws.stdin_closed is True


def test_exec_runner_stdin_writes_bytes_without_text_conversion(monkeypatch):
    import sagents.utils.sandbox.providers.remote.kubernetes as module

    class CapturingWebSocket:
        def __init__(self):
            self.closed = False
            self.stdin = None

        def write_stdin(self, data):
            self.stdin = data

        def close_stdin(self):
            pass

        def is_open(self):
            return not self.closed

        def update(self, timeout=1):
            pass

        def peek_stdout(self):
            return True

        def read_stdout(self):
            self.closed = True
            return json.dumps({"ok": True}) + "\n"

        def peek_stderr(self):
            return False

        def close(self):
            self.closed = True

    ws = CapturingWebSocket()
    provider = make_provider()
    provider._is_initialized = True
    provider._k8s_client = SimpleNamespace(connect_get_namespaced_pod_exec=Mock())
    monkeypatch.setattr(
        module,
        "k8s_stream",
        SimpleNamespace(stream=Mock(name="stream", return_value=ws)),
    )

    asyncio.run(
        provider._exec_runner(["file-write"], stdin_data=b"\x00\xffbinary")
    )

    assert ws.stdin == b"\x00\xffbinary"


def test_exec_runner_stdin_times_out(monkeypatch):
    import sagents.utils.sandbox.providers.remote.kubernetes as module

    class SlowWebSocket:
        def __init__(self):
            self.closed = False
            self.open_checks = 0

        def write_stdin(self, data):
            pass

        def close_stdin(self):
            pass

        def is_open(self):
            self.open_checks += 1
            return self.open_checks == 1 and not self.closed

        def update(self, timeout=1):
            pass

        def peek_stdout(self):
            return False

        def peek_stderr(self):
            return False

        def close(self):
            self.closed = True

    ws = SlowWebSocket()
    provider = make_provider()
    provider._is_initialized = True
    provider._k8s_client = SimpleNamespace(connect_get_namespaced_pod_exec=Mock())
    monkeypatch.setattr(
        module,
        "k8s_stream",
        SimpleNamespace(stream=Mock(name="stream", return_value=ws)),
    )
    with pytest.raises(TimeoutError, match="sage-shell-runner"):
        asyncio.run(provider._exec_runner(["file-write"], stdin_data=b"payload", timeout=0))


def test_file_exists_returns_false_only_for_missing_runner_stat(monkeypatch):
    provider = make_provider()

    async def fake_missing(args, stdin_data=None, timeout=30):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(provider, "_exec_runner", fake_missing)

    assert asyncio.run(provider.file_exists("/missing.txt")) is False

    async def fake_infra_error(args, stdin_data=None, timeout=30):
        raise RuntimeError("api unavailable")

    monkeypatch.setattr(provider, "_exec_runner", fake_infra_error)

    with pytest.raises(RuntimeError, match="api unavailable"):
        asyncio.run(provider.file_exists("/missing.txt"))


def test_file_exists_propagates_runner_executable_missing(monkeypatch):
    provider = make_provider()

    async def fake_exec(args, stdin_data=None, timeout=30):
        raise RuntimeError("sage-shell-runner: not found")

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    with pytest.raises(RuntimeError, match="sage-shell-runner: not found"):
        asyncio.run(provider.file_exists("/missing.txt"))


def test_file_exists_returns_false_for_structured_missing_file(monkeypatch):
    provider = make_provider()

    async def fake_exec(args, stdin_data=None, timeout=30):
        return json.dumps(
            {
                "error_type": "file_not_found",
                "error_message": "/missing.txt does not exist",
            }
        )

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    assert asyncio.run(provider.file_exists("/missing.txt")) is False


def test_file_list_converts_json_entries(monkeypatch):
    provider = make_provider()

    async def fake_exec(args, stdin_data=None, timeout=30):
        assert args == ["file-list", "--path", "/sage-workspace", "--include-hidden"]
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

    entries = asyncio.run(provider.list_directory("/sage-workspace", include_hidden=True))

    assert len(entries) == 1
    assert entries[0].path == "/sage-workspace/a.txt"
    assert entries[0].is_file is True
    assert entries[0].is_dir is False
    assert entries[0].size == 3
    assert entries[0].modified_time == 10.5


def test_background_support_and_default_bg_dir():
    provider = make_provider()

    assert provider.supports_background() is True
    assert provider._bg_dir() == "/sage-workspace/.sage/bg"
    assert provider._bg_dir("/custom/bg") == "/custom/bg"


def test_start_background_calls_runner_with_command_workdir_and_bg_dir(monkeypatch):
    provider = make_provider()
    captured = {}

    async def fake_exec(args, stdin_data=None, timeout=30):
        captured["args"] = args
        captured["timeout"] = timeout
        return json.dumps(
            {
                "task_id": "shtask_abc",
                "pid": 123,
                "log_path": "/custom/bg/shtask_abc.log",
                "exit_path": "/custom/bg/shtask_abc.exit",
            }
        )

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    result = asyncio.run(
        provider.start_background(
            "echo $TOKEN",
            workdir="/tmp/work",
            log_dir="/custom/bg",
        )
    )

    assert result == {
        "task_id": "shtask_abc",
        "pid": 123,
        "log_path": "/custom/bg/shtask_abc.log",
        "exit_path": "/custom/bg/shtask_abc.exit",
    }
    args = captured["args"]
    assert args[:2] == ["bg-start", "--command-b64"]
    assert "echo $TOKEN" not in args
    assert base64.b64decode(args[args.index("--command-b64") + 1]).decode() == (
        "echo $TOKEN"
    )
    assert args[args.index("--workdir") + 1] == "/tmp/work"
    assert args[args.index("--bg-dir") + 1] == "/custom/bg"
    assert captured["timeout"] == 30


def test_start_background_prefixes_env_vars_with_shell_quoted_assignments(monkeypatch):
    provider = make_provider()
    captured = {}

    async def fake_exec(args, stdin_data=None, timeout=30):
        captured["args"] = args
        return json.dumps(
            {
                "task_id": "shtask_env",
                "pid": 321,
                "log_path": "/sage-workspace/.sage/bg/shtask_env.log",
                "exit_path": "/sage-workspace/.sage/bg/shtask_env.exit",
            }
        )

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    asyncio.run(
        provider.start_background(
            "python -c 'print(1)'",
            env_vars={"FOO": "bar baz", "TOKEN": "x'y"},
        )
    )

    encoded = captured["args"][captured["args"].index("--command-b64") + 1]
    command = base64.b64decode(encoded).decode()
    quoted_token = shlex.quote("x'y")

    assert command == (
        f"FOO={shlex.quote('bar baz')} "
        f"TOKEN={quoted_token} "
        "python -c 'print(1)'"
    )


def test_start_background_preserves_command_when_env_vars_absent(monkeypatch):
    provider = make_provider()
    captured = {}

    async def fake_exec(args, stdin_data=None, timeout=30):
        captured["args"] = args
        return json.dumps(
            {
                "task_id": "shtask_plain",
                "pid": 654,
                "log_path": "/sage-workspace/.sage/bg/shtask_plain.log",
                "exit_path": "/sage-workspace/.sage/bg/shtask_plain.exit",
            }
        )

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    asyncio.run(provider.start_background("echo unchanged"))

    encoded = captured["args"][captured["args"].index("--command-b64") + 1]
    assert base64.b64decode(encoded).decode() == "echo unchanged"


def test_start_background_uses_workspace_defaults(monkeypatch):
    provider = make_provider()
    captured = {}

    async def fake_exec(args, stdin_data=None, timeout=30):
        captured["args"] = args
        return json.dumps(
            {
                "task_id": "shtask_default",
                "pid": 456,
                "log_path": "/sage-workspace/.sage/bg/shtask_default.log",
                "exit_path": "/sage-workspace/.sage/bg/shtask_default.exit",
            }
        )

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    asyncio.run(provider.start_background("pwd"))

    assert captured["args"][captured["args"].index("--workdir") + 1] == "/sage-workspace"
    assert captured["args"][captured["args"].index("--bg-dir") + 1] == (
        "/sage-workspace/.sage/bg"
    )


def test_background_followups_use_started_custom_bg_dir_and_cleanup_forgets_it(
    monkeypatch,
):
    provider = make_provider()
    calls = []

    async def fake_exec(args, stdin_data=None, timeout=30):
        calls.append(args)
        if args[0] == "bg-start":
            return json.dumps(
                {
                    "task_id": "shtask_custom",
                    "pid": 111,
                    "log_path": "/custom/bg/shtask_custom.log",
                    "exit_path": "/custom/bg/shtask_custom.exit",
                }
            )
        if args[0] == "bg-read":
            return json.dumps({"text": "tail", "size": 100})
        if args[0] == "bg-read-range":
            return json.dumps({"text": "chunk", "offset": 12, "size": 100})
        if args[0] == "bg-size":
            return json.dumps({"size": 100})
        if args[0] == "bg-state":
            return json.dumps({"alive": True, "exit_code": None})
        if args[0] == "bg-kill":
            return json.dumps({"ok": True})
        raise AssertionError(args)

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    asyncio.run(provider.start_background("sleep 60", log_dir="/custom/bg"))
    asyncio.run(provider.read_background_output("shtask_custom"))
    asyncio.run(provider.read_background_output_range("shtask_custom", offset=1))
    asyncio.run(provider.get_background_output_size("shtask_custom"))
    asyncio.run(provider.is_background_alive("shtask_custom"))
    asyncio.run(provider.get_background_exit_code("shtask_custom"))
    asyncio.run(provider.kill_background("shtask_custom"))
    asyncio.run(provider.cleanup_background("shtask_custom"))
    asyncio.run(provider.read_background_output("shtask_custom"))

    bg_dirs = [
        call[call.index("--bg-dir") + 1]
        for call in calls
        if "--bg-dir" in call
    ]
    assert bg_dirs == [
        "/custom/bg",
        "/custom/bg",
        "/custom/bg",
        "/custom/bg",
        "/custom/bg",
        "/custom/bg",
        "/custom/bg",
        "/sage-workspace/.sage/bg",
    ]


def test_background_read_range_size_state_and_kill_call_runner(monkeypatch):
    provider = make_provider()
    calls = []

    async def fake_exec(args, stdin_data=None, timeout=30):
        calls.append(args)
        if args[0] == "bg-read":
            return json.dumps({"text": "tail", "size": 100})
        if args[0] == "bg-read-range":
            return json.dumps({"text": "chunk", "offset": 12, "size": 100})
        if args[0] == "bg-size":
            return json.dumps({"size": 100})
        if args[0] == "bg-state":
            return json.dumps({"alive": False, "exit_code": 7})
        if args[0] == "bg-kill":
            return json.dumps({"ok": True})
        raise AssertionError(args)

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    assert asyncio.run(provider.read_background_output("shtask_abc", max_bytes=44)) == "tail"
    assert asyncio.run(
        provider.read_background_output_range("shtask_abc", offset=5, max_bytes=7)
    ) == ("chunk", 12)
    assert asyncio.run(provider.get_background_output_size("shtask_abc")) == 100
    assert asyncio.run(provider.is_background_alive("shtask_abc")) is False
    assert asyncio.run(provider.get_background_exit_code("shtask_abc")) == 7
    assert asyncio.run(provider.kill_background("shtask_abc", force=True)) is True
    asyncio.run(provider.cleanup_background("shtask_abc"))

    assert calls == [
        [
            "bg-read",
            "--task-id",
            "shtask_abc",
            "--bg-dir",
            "/sage-workspace/.sage/bg",
            "--max-bytes",
            "44",
        ],
        [
            "bg-read-range",
            "--task-id",
            "shtask_abc",
            "--bg-dir",
            "/sage-workspace/.sage/bg",
            "--offset",
            "5",
            "--max-bytes",
            "7",
        ],
        ["bg-size", "--task-id", "shtask_abc", "--bg-dir", "/sage-workspace/.sage/bg"],
        ["bg-state", "--task-id", "shtask_abc", "--bg-dir", "/sage-workspace/.sage/bg"],
        ["bg-state", "--task-id", "shtask_abc", "--bg-dir", "/sage-workspace/.sage/bg"],
        [
            "bg-kill",
            "--task-id",
            "shtask_abc",
            "--bg-dir",
            "/sage-workspace/.sage/bg",
            "--force",
        ],
    ]


def test_background_followups_normalize_preloaded_stream_data_responses(monkeypatch):
    import sagents.utils.sandbox.providers.remote.kubernetes as module

    provider = make_provider()
    provider._is_initialized = True
    provider._k8s_client = SimpleNamespace(connect_get_namespaced_pod_exec=Mock())
    bg_dir = "/sage-workspace/.sage/bg"
    stream_fn = Mock(
        name="stream",
        side_effect=[
            SimpleNamespace(data={"text": "tail"}),
            SimpleNamespace(data={"text": "delta", "offset": 5}),
            SimpleNamespace(data={"size": 9}),
            SimpleNamespace(data={"alive": True, "exit_code": None}),
            SimpleNamespace(data={"alive": False, "exit_code": 0}),
            SimpleNamespace(data={"ok": True}),
        ],
    )
    monkeypatch.setattr(module, "k8s_stream", SimpleNamespace(stream=stream_fn))

    assert asyncio.run(provider.read_background_output("shtask_abc")) == "tail"
    assert asyncio.run(
        provider.read_background_output_range("shtask_abc", offset=2, max_bytes=3)
    ) == ("delta", 5)
    assert asyncio.run(provider.get_background_output_size("shtask_abc")) == 9
    assert asyncio.run(provider.is_background_alive("shtask_abc")) is True
    assert asyncio.run(provider.get_background_exit_code("shtask_abc")) == 0
    assert asyncio.run(provider.kill_background("shtask_abc", force=True)) is True

    commands = [call.kwargs["command"] for call in stream_fn.call_args_list]
    assert commands[0] == [
        "sage-shell-runner",
        "bg-read",
        "--task-id",
        "shtask_abc",
        "--bg-dir",
        bg_dir,
        "--max-bytes",
        "8192",
    ]
    assert commands[-1] == [
        "sage-shell-runner",
        "bg-kill",
        "--task-id",
        "shtask_abc",
        "--bg-dir",
        bg_dir,
        "--force",
    ]


@pytest.mark.parametrize(
    ("method_name", "args", "raw"),
    [
        ("start_background", ("echo hi",), json.dumps({"task_id": "shtask_abc"})),
        ("read_background_output", ("shtask_abc",), json.dumps({"size": 10})),
        ("read_background_output_range", ("shtask_abc",), json.dumps({"text": "x"})),
        ("get_background_output_size", ("shtask_abc",), json.dumps({"text": "x"})),
        ("is_background_alive", ("shtask_abc",), json.dumps({"exit_code": 0})),
        ("get_background_exit_code", ("shtask_abc",), "not-json"),
        ("kill_background", ("shtask_abc",), json.dumps({"killed": True})),
    ],
)
def test_background_runner_malformed_responses_raise(
    monkeypatch,
    method_name,
    args,
    raw,
):
    provider = make_provider()

    async def fake_exec(exec_args, stdin_data=None, timeout=30):
        return raw

    monkeypatch.setattr(provider, "_exec_runner", fake_exec)

    with pytest.raises(RuntimeError, match="Invalid sage-shell-runner response"):
        asyncio.run(getattr(provider, method_name)(*args))


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


def test_execute_javascript_passes_code_as_base64(monkeypatch):
    provider = make_provider()
    captured = {}

    async def fake_execute_command(command, workdir=None, timeout=60, env_vars=None):
        captured["command"] = command
        return SimpleNamespace(success=True, stdout="ok", stderr="", execution_time=0.2)

    monkeypatch.setattr(provider, "execute_command", fake_execute_command)

    result = asyncio.run(provider.execute_javascript("console.log('secret code')"))

    assert result.success is True
    assert result.output == "ok"
    assert "secret code" not in captured["command"]
    assert "base64" in captured["command"]


def test_cleanup_releases_state_and_kill_deletes_pod_only():
    provider = make_provider()
    client = Mock()
    provider._k8s_client = client
    provider._pod_name = "sage-sandbox-sandbox-abc"
    provider._is_initialized = True

    asyncio.run(provider.cleanup())

    client.delete_namespaced_pod.assert_not_called()
    assert provider._is_initialized is False
    assert provider._pod_name == "sage-sandbox-sandbox-abc"

    asyncio.run(provider.kill())

    client.delete_namespaced_pod.assert_called_once_with(
        name="sage-sandbox-sandbox-abc",
        namespace="sage",
    )
    assert provider._is_initialized is False


def test_kill_ignores_not_found_and_is_idempotent():
    provider = make_provider()
    client = Mock()
    client.delete_namespaced_pod.side_effect = [not_found_error(), None]
    provider._k8s_client = client
    provider._pod_name = "sage-sandbox-sandbox-abc"
    provider._is_initialized = True

    asyncio.run(provider.kill())
    asyncio.run(provider.kill())

    assert client.delete_namespaced_pod.call_count == 2
    assert provider._is_initialized is False


def test_factory_passes_kubernetes_config_without_duplicate_kwargs(monkeypatch):
    class CapturingProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setitem(
        SandboxProviderFactory._remote_providers,
        "kubernetes",
        CapturingProvider,
    )
    config = SandboxConfig(
        mode=SandboxType.REMOTE,
        sandbox_id="sandbox_ABC",
        remote_provider="kubernetes",
        remote_image="sage/sandbox-runtime:latest",
        sandbox_agent_workspace=None,
        remote_provider_config={
            "namespace": "sage",
            "resources": {"limits": {"cpu": "500m"}},
            "service_account_name": "sage-sandbox-runner",
            "session_id": "session-123",
            "pvc": {"claim_name": "sage-sandbox-workspaces"},
            "pod_labels": {"extra": "label"},
            "pod_annotations": {"extra": "annotation"},
            "pod_security_context": {"runAsUser": 0, "runAsGroup": 0},
            "container_security_context": {"allowPrivilegeEscalation": False},
            "command_logging": {"enabled": True},
            "pod_command": ["/bin/sh", "-c"],
            "pod_args": ["sleep infinity"],
        },
    )

    provider = asyncio.run(SandboxProviderFactory.create(config))

    assert provider.kwargs["namespace"] == "sage"
    assert provider.kwargs["image"] == "sage/sandbox-runtime:latest"
    assert provider.kwargs["virtual_workspace"] == "/sage-workspace"
    assert provider.kwargs["resources"] == {"limits": {"cpu": "500m"}}
    assert provider.kwargs["service_account_name"] == "sage-sandbox-runner"
    assert provider.kwargs["session_id"] == "session-123"
    assert provider.kwargs["pvc"] == {"claim_name": "sage-sandbox-workspaces"}
    assert provider.kwargs["pod_command"] == ["/bin/sh", "-c"]
    assert provider.kwargs["pod_args"] == ["sleep infinity"]
