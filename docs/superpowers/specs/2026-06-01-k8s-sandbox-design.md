# Kubernetes Sandbox Integration Design

Date: 2026-06-01

## Scope

This design covers a production-ready baseline Kubernetes sandbox provider for Sage. The provider will run only inside a Kubernetes cluster, use the in-cluster Kubernetes client, reuse sandbox Pods by `sandbox_id`, bind an existing PVC for workspace persistence, and execute commands through a project-maintained sandbox runtime image.

The design does not include automatic PVC creation, RBAC creation, ServiceAccount creation, NetworkPolicy management, TTL controllers, or a multi-tenant governance layer. Those are deployment responsibilities or future platform work.

## Goals

- Implement a Kubernetes-backed `ISandboxHandle` that preserves the existing tool-layer contract.
- Use in-cluster Kubernetes configuration only. Local kubeconfig fallback is out of scope.
- Reuse an existing Pod when the same `sandbox_id` is initialized and the Pod is Ready.
- Bind an existing PVC as the sandbox workspace, with configurable `subPath` templates using an explicit `session_id`.
- Maintain a sandbox base image in this repository.
- Run a metric agent as the sandbox container's main process.
- Execute shell commands through an in-image `sage-shell-runner` entrypoint.
- Support reliable stdout, stderr, exit code, timeout, duration, environment variables, and working directory.
- Support text/file APIs, tar-based copy, and full background shell APIs.
- Emit shell execution metrics through a reporter abstraction, with JSON logs as the first implementation.

## Non-Goals

- No Pod-internal HTTP or gRPC command server.
- No external kubeconfig or out-of-cluster control plane support.
- No automatic PVC provisioning.
- No default hardening of sandbox container privileges in the first version.
- No first-version OTLP push or Prometheus scrape endpoint, although the reporter boundary should allow them later.
- No changes to local or passthrough sandbox behavior.

## Architecture

The integration has two layers: the Sage control plane and the sandbox image runtime.

The Sage control plane lives in `KubernetesSandboxProvider`. It loads in-cluster Kubernetes configuration, validates Kubernetes-specific provider config, manages sandbox Pod lifecycle, waits for Pod readiness, and calls Kubernetes exec against the sandbox container. The tool layer continues to depend only on `ISandboxHandle`.

The sandbox image runtime is maintained by this project and includes two executables:

- `sage-sandbox-metric-agent`: the container main process. It keeps the container alive, receives or collects shell execution events, and sends them to configured reporters.
- `sage-shell-runner`: the command and file operation entrypoint invoked by Kubernetes exec. It runs subprocesses, handles timeouts, captures stdout/stderr/exit code/duration, manages background tasks, and reports execution events to the metric agent.

Execution path:

```text
Tool -> ISandboxHandle -> KubernetesSandboxProvider
  -> Kubernetes API exec
    -> sage-shell-runner inside sandbox container
      -> subprocess / background registry / file helper
      -> metric agent reporter
```

## Kubernetes Provider Configuration

Kubernetes-specific settings live under `SandboxConfig.remote_provider_config` so common sandbox config remains provider-neutral.

Example:

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
        "pod_labels": {},
        "pod_annotations": {},
        "pod_security_context": {},
        "container_security_context": {},
        "command_logging": {
            "enabled": False,
            "redact": True,
        },
    },
)
```

Required fields:

- `namespace`
- `session_id`
- `pvc.claim_name`
- `pvc.mount_path`

`pvc.sub_path_template` is optional. If it references `{session_id}`, `session_id` must be present. The provider must not silently substitute `sandbox_id` for `session_id`.

`service_account_name` is optional. When provided, it is written to the Pod spec. If the ServiceAccount does not exist or the Sage Server ServiceAccount lacks permission to use it, the Kubernetes API error is surfaced clearly.

## Pod And PVC Lifecycle

The provider binds an existing PVC; it never creates or deletes PVCs.

Initialization behavior:

1. Validate Kubernetes provider config.
2. Load in-cluster Kubernetes config.
3. Build a deterministic Pod name from `sandbox_id`, sanitized as a Kubernetes DNS label.
4. Read the Pod by name in the configured namespace.
5. If the Pod exists and is Ready, reuse it.
6. If the Pod exists but is Failed, Succeeded, deleting, or otherwise unrecoverable, delete it and recreate it.
7. If the Pod does not exist, create it.
8. Wait until the Pod is Ready or initialization times out.

The Pod mounts the configured PVC at the workspace path. If `sub_path_template` is configured, it is rendered from explicit provider config variables, starting with `session_id`.

`cleanup()` releases local provider state only and does not delete the Pod or PVC. `kill()` deletes the Pod but does not delete the PVC. Long-term TTL cleanup is left to deployment-level CronJobs or controllers.

The first version runs as root by default to reduce PVC write permission and package installation failures. `pod_security_context` and `container_security_context` are accepted as optional passthrough config, but no restrictive defaults are applied.

## Sandbox Runtime Image

Add a project-maintained sandbox image, for example under `deploy/images/Dockerfile.sandbox`, plus runtime source files in a small package owned by this repository.

The image must include:

- Python runtime needed by `sage-shell-runner` and `sage-sandbox-metric-agent`.
- POSIX shell tools used by command execution.
- `tar` for upload/download streams.
- `sage-shell-runner` on `PATH`.
- `sage-sandbox-metric-agent` as the default container command.

The image can be overridden through `SandboxConfig.remote_image`, but the provider assumes the selected image implements the Sage sandbox runtime contract.

## Command Execution

The provider does not exec arbitrary user commands directly. It execs `sage-shell-runner`.

Synchronous command shape:

```text
sage-shell-runner run \
  --command-b64 <base64-command> \
  --workdir /sage-workspace \
  --timeout 30 \
  --env-json-b64 <base64-json-env>
```

The runner decodes the command and environment, starts a subprocess, captures stdout/stderr, enforces timeout, and prints a structured JSON response to stdout for the provider to parse.

The provider converts runner output to `CommandResult`:

- `success`: `exit_code == 0`
- `stdout`: captured command stdout
- `stderr`: captured command stderr, plus runner-level error detail when appropriate
- `return_code`: command exit code or a conventional nonzero value for timeout/runner failure
- `execution_time`: duration in seconds

Command failure is not a provider exception. Infrastructure failures are exceptions: Pod unavailable, Kubernetes exec failure, invalid runner protocol, or malformed runner JSON.

`execute_python()` and `execute_javascript()` use `execute_command()` with safe command construction through the runner. They should avoid shell quoting vulnerabilities by passing code through base64 or a temporary file mechanism.

## Metrics

The metric agent uses a reporter abstraction. First version reporter:

- `JsonLogReporter`: writes structured JSON events to stdout.

Future reporters:

- OTLP push.
- Prometheus metrics endpoint.

By default, raw command text is not logged. Events include:

- `event`
- `sandbox_id`
- `session_id`
- `command_id`
- `command_hash`
- `command_length`
- `workdir`
- `exit_code`
- `duration_ms`
- `stdout_bytes`
- `stderr_bytes`
- `timeout`

Raw command logging is configurable and disabled by default. When enabled, the runner records a redacted command field. Redaction covers common `token`, `key`, `secret`, and `password` forms and can be expanded later.

Runner-to-agent communication should use a local Unix socket in the first implementation. If the agent is unavailable, the runner should degrade to direct JSON logging so metrics failure does not break shell execution.

## File Operations

File operations use a mix of tar streaming and runner subcommands.

Tar streaming is used for:

- `copy_from_host()` for files and directories.
- Large or binary-oriented uploads.
- Future `sync_directory_from_sandbox()` downloads.

Runner subcommands are used for structured file APIs:

```text
sage-shell-runner file-read --path <path>
sage-shell-runner file-write --path <path> --mode overwrite|append --content-b64 <content>
sage-shell-runner file-list --path <path> --include-hidden
sage-shell-runner file-stat --path <path>
sage-shell-runner file-delete --path <path>
sage-shell-runner file-mkdir --path <path>
```

`list_directory()` returns JSON and is converted to `FileInfo`; it must not parse `ls` output. File paths are validated to avoid path traversal during tar extraction and to keep operations inside allowed virtual paths when applicable.

## Background Shell

The Kubernetes provider implements the full `ISandboxHandle` background API in the first version.

- `start_background()` invokes `sage-shell-runner bg-start`.
- The runner creates a `task_id`, starts the process, and writes stdout/stderr to `/sage-workspace/.sage/bg/<task_id>.log`.
- The runner writes the exit code to `/sage-workspace/.sage/bg/<task_id>.exit`.
- `read_background_output()` returns a tail of the log.
- `read_background_output_range()` reads from a byte offset and returns `(text, new_offset)`.
- `get_background_output_size()` returns log byte size.
- `is_background_alive()` checks the sandbox-side pid or runner registry.
- `get_background_exit_code()` reads the exit file when present.
- `kill_background()` sends SIGTERM and escalates to SIGKILL when needed.
- `cleanup_background()` removes registry state while preserving logs long enough for completed task reads.

This preserves the existing two-stage shell UX in `execute_command_tool`.

## Error Handling

Configuration errors raise clear `ValueError`s before creating Kubernetes resources. Examples include missing namespace, missing PVC claim name, missing session id, or an unrenderable subPath template.

Kubernetes API errors preserve status, reason, and message. The provider should wrap them only to add operation context such as "create sandbox pod" or "wait for sandbox pod readiness".

Runner command errors return structured command results. Runner protocol errors, invalid JSON, missing runtime binaries, Pod readiness failures, and Kubernetes exec failures raise provider exceptions.

## Tests

Provider unit tests mock the Kubernetes client and cover:

- In-cluster config loading.
- Pod name sanitization.
- Pod reuse when Ready.
- Pod delete/recreate when terminal or unrecoverable.
- PVC mount and subPath rendering.
- Required config validation.
- ServiceAccount passthrough.
- Resource requests and limits.
- Kubernetes API error wrapping.
- Runner JSON conversion to `CommandResult`.

Runner unit tests cover:

- Command success and failure.
- stdout/stderr separation.
- exit code propagation.
- timeout behavior.
- working directory and env handling.
- command hashing and optional redacted command logging.
- file subcommands.
- background start/read/range/size/alive/exit/kill cleanup behavior.

Tar/copy tests cover:

- file upload.
- directory upload.
- ignore patterns.
- path traversal rejection.

Integration tests can run against a real cluster or kind and are skipped by default unless an explicit environment variable enables them.

## Implementation Order

1. Add the sandbox runtime image and minimal runner/metric agent package.
2. Implement Kubernetes provider config parsing and validation.
3. Implement Pod reuse/recreate lifecycle, PVC mount, subPath rendering, and readiness wait.
4. Route `execute_command()` through `sage-shell-runner run`.
5. Implement `execute_python()` and `execute_javascript()` on top of runner command execution.
6. Implement structured file subcommands and tar-based `copy_from_host()`.
7. Implement the full background shell API.
8. Add unit tests and optional integration test hooks.
9. Document Kubernetes deployment prerequisites: namespace, ServiceAccount permissions, existing PVC, image, and provider config.

