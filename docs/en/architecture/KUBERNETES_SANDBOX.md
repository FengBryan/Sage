# Kubernetes Sandbox

The Kubernetes sandbox provider runs Sage sandbox workloads as Kubernetes Pods. Sage Server must run inside the target cluster and use a ServiceAccount that can read, create, delete, and exec into sandbox Pods in the configured namespace.

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

`remote_provider_config` is passed through to the Kubernetes provider. The provider reads in-cluster Kubernetes credentials only; it does not load a kubeconfig file. `pvc.claim_name` must reference an existing PersistentVolumeClaim. `pvc.sub_path_template` may use `{session_id}` when `session_id` is present in the provider config.

## Runtime Image

The configured image must include:

- `sage-shell-runner`
- `sage-sandbox-metric-agent`
- `tar`
- Python 3.11 or newer

The project-maintained image is defined in `deploy/images/Dockerfile.sandbox`. Its main process is `sage-sandbox-metric-agent`; Sage Server uses Kubernetes exec to run `sage-shell-runner` inside the same container.

## Lifecycle

The provider reuses a Ready Pod with the same `sandbox_id`. It deletes and recreates terminal Pods. `cleanup()` leaves the Pod and PVC in place. `kill()` deletes the Pod and keeps the PVC.

## Commands And Files

Commands execute through `sage-shell-runner run`. File reads, writes, listing, directory creation, and background task management use runner subcommands. `copy_from_host()` streams a tar archive over Kubernetes exec stdin and extracts it inside the sandbox; symlinks and unsafe tar paths are rejected by the runner.

## Metrics

The metric agent writes JSON shell execution events to stdout by default. Raw command logging is disabled unless explicitly enabled, and command metrics include stable hashes and byte counts for command output.
