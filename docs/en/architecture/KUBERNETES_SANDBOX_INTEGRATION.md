---
layout: default
title: Kubernetes Sandbox Integration Guide
parent: Architecture
nav_order: 9
description: "How to integrate Sage Kubernetes remote sandbox, including images, RBAC, PVCs, Sage configuration, smoke tests, and troubleshooting"
lang: en
ref: kubernetes-sandbox-integration
---

{% include lang_switcher.html %}

# Kubernetes Sandbox Integration Guide

This guide is for deploying Sage remote sandbox on Kubernetes. Sage Server runs inside the target cluster, creates or reuses sandbox Pods, and calls `sage-shell-runner` through Kubernetes exec for commands, file operations, and background tasks.

## 1. Prerequisites

- Sage Server must run inside the target Kubernetes cluster.
- Sage Server uses its in-Pod ServiceAccount; kubeconfig is not loaded.
- A PersistentVolumeClaim must already exist. The provider does not create PVCs.
- The sandbox image must include:
  - `sage-sandbox-metric-agent`
  - `sage-shell-runner`
- The Sage Server ServiceAccount needs permission to read, create, delete, and exec into sandbox Pods in the target namespace.

## 2. Build The Runtime Image

The project-maintained image is defined at:

```bash
deploy/images/Dockerfile.sandbox
```

Example:

```bash
docker build -f deploy/images/Dockerfile.sandbox -t registry.example.com/sage/sandbox-runtime:latest .
docker push registry.example.com/sage/sandbox-runtime:latest
```

The container main process is `sage-sandbox-metric-agent`. Sage Server uses Kubernetes exec to run `sage-shell-runner` inside that container.

## 3. Create Namespace, RBAC, And PVC

Minimal example:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: sage
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: sage-server
  namespace: sage
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: sage-sandbox-manager
  namespace: sage
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "create", "delete"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: sage-sandbox-manager
  namespace: sage
subjects:
  - kind: ServiceAccount
    name: sage-server
    namespace: sage
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: sage-sandbox-manager
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: sage-sandbox-workspaces
  namespace: sage
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 100Gi
  storageClassName: your-rwx-storage-class
```

RWX storage is recommended for production, especially when Sage Server or sandbox Pods may move across nodes.

## 4. Configure Sage

```python
from sagents.utils.sandbox.config import SandboxConfig
from sagents.utils.sandbox.factory import SandboxProviderFactory
from sagents.utils.sandbox.interface import SandboxType

config = SandboxConfig(
    mode=SandboxType.REMOTE,
    remote_provider="kubernetes",
    sandbox_id="sandbox-123",
    sandbox_agent_workspace="/sage-workspace",
    remote_image="registry.example.com/sage/sandbox-runtime:latest",
    remote_timeout=1800,
    remote_provider_config={
        "namespace": "sage",
        "service_account_name": "sage-server",
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
        "command_logging": {
            "enabled": False,
            "redact": True,
        },
    },
)

sandbox = await SandboxProviderFactory.create(config)
```

Important fields:

| Field | Description |
| --- | --- |
| `remote_provider` | Must be `"kubernetes"` |
| `sandbox_id` | Stable sandbox identity; Ready Pods with the same identity are reused |
| `sandbox_agent_workspace` | Sandbox-visible workspace path, usually `/sage-workspace` |
| `remote_image` | Sandbox runtime image |
| `remote_provider_config.namespace` | Namespace for sandbox Pods |
| `remote_provider_config.service_account_name` | ServiceAccount used by the sandbox Pod |
| `remote_provider_config.session_id` | Used to render PVC subPath templates |
| `remote_provider_config.pvc.claim_name` | Existing PVC name, required |
| `remote_provider_config.pvc.sub_path_template` | Optional; supports `{session_id}` |
| `remote_provider_config.resources` | Container resources |
| `remote_provider_config.command_logging` | Redacted command logging; disabled by default |

## 5. Lifecycle

- `initialize()` creates a Pod, reuses a Ready matching Pod, or recreates terminal Pods.
- `cleanup()` releases provider-local state and leaves the Pod/PVC in place.
- `kill()` deletes the Pod and keeps PVC data.

## 6. File Copy And Exec

`copy_from_host()` sends a tar archive over Kubernetes exec stdin and runs `sage-shell-runner tar-extract` in the sandbox. The provider passes `--size-bytes` so the runner reads a fixed number of bytes instead of waiting for stdin EOF. This is compatible with Kubernetes Python clients that do not support closing stdin independently.

The runner rejects absolute tar paths, `..` traversal, symlinks, and hardlinks.

## 7. Smoke Test

Check sandbox Pod creation:

```bash
kubectl -n sage get pod -l app=sage-sandbox
kubectl -n sage describe pod <sandbox-pod-name>
```

Verify the runner:

```bash
kubectl -n sage exec <sandbox-pod-name> -- sage-shell-runner run \
  --command-b64 "$(printf 'pwd && whoami' | base64 -w0)" \
  --workdir /sage-workspace \
  --timeout 10 \
  --env-json-b64 "$(printf '{}' | base64 -w0)" \
  --sandbox-id smoke-test
```

Expected output is one JSON line containing `success`, `stdout`, `stderr`, and `exit_code`.

## 8. Troubleshooting

- Missing PVC: configure `remote_provider_config.pvc.claim_name`.
- Missing `session_id`: required when `sub_path_template` contains `{session_id}`.
- Pod not Ready: check image pull, PVC mount, and whether `sage-sandbox-metric-agent` exists.
- 403 Forbidden: check `pods` and `pods/exec` RBAC.
- File copy timeout: make sure Sage Server and the sandbox image both include the `tar-extract --size-bytes` runtime.
