---
layout: default
title: Kubernetes 沙箱对接指南
parent: 架构
nav_order: 9
description: "如何在集群内接入 Sage Kubernetes remote sandbox，包括镜像、RBAC、PVC、配置、验证与排障"
lang: zh
ref: kubernetes-sandbox-integration
---

{% include lang_switcher.html %}

# Kubernetes 沙箱对接指南

本文面向需要把 Sage remote sandbox 接到 Kubernetes 集群的部署者。目标是让 Sage Server 在集群内创建和复用 sandbox Pod，并通过 Kubernetes exec 调用 `sage-shell-runner` 执行命令、读写文件、管理后台任务。

## 1. 前置条件

Kubernetes sandbox 当前按“集群内运行”设计：

- Sage Server 必须运行在目标 Kubernetes 集群内。
- Sage Server 使用 Pod 内 ServiceAccount 调 Kubernetes API，不读取 kubeconfig。
- 集群中需要提前准备一个 PersistentVolumeClaim，provider 不会自动创建 PVC。
- sandbox 镜像需要包含 Sage runtime 代码和两个入口命令：
  - `sage-sandbox-metric-agent`
  - `sage-shell-runner`
- ServiceAccount 需要能在目标 namespace 内读取、创建、删除 Pod，并能 exec 到 Pod。

## 2. 构建 sandbox runtime 镜像

项目维护的基础镜像定义在：

```bash
deploy/images/Dockerfile.sandbox
```

示例构建并推送：

```bash
docker build -f deploy/images/Dockerfile.sandbox -t registry.example.com/sage/sandbox-runtime:latest .
docker push registry.example.com/sage/sandbox-runtime:latest
```

这个镜像的主进程是 `sage-sandbox-metric-agent`。Sage Server 不直接把用户命令作为容器启动命令，而是通过 Kubernetes exec 进入 Pod 内调用：

```bash
sage-shell-runner run ...
sage-shell-runner file-read ...
sage-shell-runner bg-start ...
sage-shell-runner tar-extract ...
```

## 3. 准备 namespace、ServiceAccount、RBAC 和 PVC

下面是一份最小示例。按你的存储类、namespace、镜像拉取策略调整即可。

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

如果你的存储只支持 `ReadWriteOnce`，也可以先跑单副本 Sage Server 和单节点 sandbox 验证；生产多副本通常建议使用 RWX 存储，避免 Pod 漂移后 workspace 不可挂载。

## 4. 配置 Sage 使用 Kubernetes provider

核心配置是 `SandboxConfig`：

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

字段说明：

| 字段 | 说明 |
| --- | --- |
| `mode` | 必须是 `SandboxType.REMOTE` |
| `remote_provider` | 必须是 `"kubernetes"` |
| `sandbox_id` | sandbox Pod 的稳定身份；同一个 `sandbox_id` 会复用 Ready Pod |
| `sandbox_agent_workspace` | 工具层看到的沙箱工作区路径，默认建议 `/sage-workspace` |
| `remote_image` | sandbox runtime 镜像 |
| `remote_timeout` | Pod ready、删除等待等 provider 操作超时，单位秒 |
| `remote_provider_config.namespace` | sandbox Pod 所在 namespace |
| `remote_provider_config.service_account_name` | sandbox Pod 自身使用的 ServiceAccount；不配置则使用 namespace 默认值 |
| `remote_provider_config.session_id` | 用于渲染 PVC subPath，例如 `sessions/{session_id}` |
| `remote_provider_config.pvc.claim_name` | 已存在的 PVC 名称，必填 |
| `remote_provider_config.pvc.mount_path` | PVC 在 sandbox Pod 内的挂载路径 |
| `remote_provider_config.pvc.sub_path_template` | 可选；支持 `{session_id}` 占位 |
| `remote_provider_config.resources` | 透传到容器 resources |
| `remote_provider_config.pod_labels` | 自定义 Pod labels；保留 label 会被 provider 覆盖 |
| `remote_provider_config.pod_annotations` | 自定义 Pod annotations；保留 annotation 会被 provider 覆盖 |
| `remote_provider_config.pod_security_context` | Pod securityContext；默认 root |
| `remote_provider_config.container_security_context` | 容器 securityContext；默认空 |
| `remote_provider_config.command_logging` | 是否记录 redacted command；默认不记录原始命令 |
| `remote_provider_config.pod_command` / `pod_args` | 覆盖 sandbox 容器启动命令；一般不要改 |

## 5. 在 Sage Server 中接入

如果你的入口直接构造 `SandboxConfig`，按上一节传入即可。若使用环境变量驱动现有 session 初始化，至少需要让运行时选择 remote + kubernetes，并在创建 `SandboxConfig` 时把 provider 专有配置填入 `remote_provider_config`。

推荐的服务端集成方式是：在创建 session 时显式生成稳定的 `sandbox_id` 和 `session_id`，并把同一个 `session_id` 放进 PVC subPath。

```python
session_id = request.session_id

config = SandboxConfig(
    mode=SandboxType.REMOTE,
    remote_provider="kubernetes",
    sandbox_id=f"sandbox-{session_id}",
    sandbox_agent_workspace="/sage-workspace",
    remote_image=settings.SAGE_SANDBOX_IMAGE,
    remote_provider_config={
        "namespace": settings.SAGE_SANDBOX_NAMESPACE,
        "service_account_name": settings.SAGE_SANDBOX_SERVICE_ACCOUNT,
        "session_id": session_id,
        "pvc": {
            "claim_name": settings.SAGE_SANDBOX_PVC,
            "mount_path": "/sage-workspace",
            "sub_path_template": "sessions/{session_id}",
        },
    },
)
```

这样同一会话会落到同一个 PVC 子目录：`sessions/<session_id>`。

## 6. 生命周期语义

Kubernetes provider 的生命周期行为如下：

- `initialize()`：
  - 如果同名 Pod 不存在，则创建。
  - 如果同名 Pod 已 Ready 且 sandbox 身份匹配，则复用。
  - 如果同名 Pod 是 `Succeeded` / `Failed`，则删除后重建。
  - 如果同名 Pod 身份不匹配，则报错，避免误连别人的 Pod。
- `cleanup()`：只释放 provider 本地状态，不删除 Pod 和 PVC。
- `kill()`：删除 Pod，但保留 PVC 数据。

这意味着 workspace 数据是否保留主要由 PVC 和 subPath 决定。

## 7. 文件复制与 exec 注意事项

`copy_from_host()` 使用 tar over Kubernetes exec：

1. Sage Server 在本地把源文件或目录打成 tar。
2. 通过 exec stdin 写入 `sage-shell-runner tar-extract`。
3. runner 在 sandbox 内解包到目标路径。

实现不会依赖 Kubernetes websocket 的 stdin EOF；provider 会把 tar 大小通过 `--size-bytes` 传给 runner，runner 读取固定长度后开始解包。这样可以兼容不支持单独关闭 stdin stream 的 Kubernetes Python client。

安全边界：

- 源目录中的 symlink 会被跳过。
- tar 中的绝对路径和 `..` 路径会被拒绝。
- tar link member 会被拒绝。
- 单文件复制遵循“复制到目标文件路径”语义：

```python
await sandbox.copy_from_host("/tmp/a.txt", "/sage-workspace/target.txt")
# 结果是 /sage-workspace/target.txt
```

目录复制遵循“复制目录内容到目标目录”语义：

```python
await sandbox.copy_from_host("/tmp/project", "/sage-workspace/project")
# 结果是 /sage-workspace/project/<project 内部内容>
```

## 8. 冒烟验证

部署 Sage Server 后，可以用以下检查顺序定位问题。

先看 sandbox Pod 是否创建：

```bash
kubectl -n sage get pod -l app=sage-sandbox
kubectl -n sage describe pod <sandbox-pod-name>
```

进入 Sage 侧触发一条命令后，Pod 日志应包含 metric agent 输出：

```bash
kubectl -n sage logs <sandbox-pod-name>
```

手动验证 runner 是否存在：

```bash
kubectl -n sage exec <sandbox-pod-name> -- sage-shell-runner run \
  --command-b64 "$(printf 'pwd && whoami' | base64 -w0)" \
  --workdir /sage-workspace \
  --timeout 10 \
  --env-json-b64 "$(printf '{}' | base64 -w0)" \
  --sandbox-id smoke-test
```

预期输出是一行 JSON，包含 `success`、`stdout`、`stderr`、`exit_code` 等字段。

## 9. 指标与命令日志

sandbox 容器主进程 `sage-sandbox-metric-agent` 默认把 shell execution event 以 JSON line 写到 stdout。事件包含：

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

默认不记录原始命令。只有显式配置 `command_logging.enabled = True` 时，runner 才会输出脱敏后的命令字段。生产环境建议保持默认关闭，除非你已经确认日志链路的权限和保留策略。

## 10. 常见问题

### `remote_provider_config.pvc.claim_name is required`

没有配置 PVC。Kubernetes provider 要求使用已有 PVC，不会自动创建。

### `remote_provider_config.session_id is required for pvc.sub_path_template`

`sub_path_template` 中包含 `{session_id}`，但没有传 `remote_provider_config.session_id`。传入稳定 session id，或去掉 subPath 模板。

### Pod 一直不是 Ready

检查：

```bash
kubectl -n sage describe pod <sandbox-pod-name>
kubectl -n sage logs <sandbox-pod-name>
```

常见原因是镜像拉取失败、PVC 无法挂载、镜像内没有 `sage-sandbox-metric-agent`，或容器启动命令被覆盖。

### `sage-shell-runner` 找不到

sandbox runtime 镜像没有正确构建，或 PATH 没包含 runner 安装位置。优先确认 `deploy/images/Dockerfile.sandbox` 构建出的镜像能执行：

```bash
sage-sandbox-metric-agent --check
sage-shell-runner --help
```

### 403 Forbidden

ServiceAccount 权限不足。确认 Sage Server Pod 使用的 ServiceAccount 有目标 namespace 内 `pods` 的 `get/create/delete` 权限，以及 `pods/exec` 的 `create` 权限。

### 文件复制卡住或超时

确认 Sage Server 和 sandbox 镜像都包含支持 `--size-bytes` 的当前版本代码。旧 runner 只会从 stdin 读到 EOF，在部分 Kubernetes Python client 版本上可能卡住。

## 11. 最小接入清单

- [ ] Sage Server 部署在 Kubernetes 集群内。
- [ ] Sage Server Pod 使用有权限的 ServiceAccount。
- [ ] sandbox runtime 镜像已构建并推送到集群可拉取的 registry。
- [ ] namespace 中已有 PVC。
- [ ] `SandboxConfig.mode = SandboxType.REMOTE`。
- [ ] `SandboxConfig.remote_provider = "kubernetes"`。
- [ ] `remote_image` 指向 sandbox runtime 镜像。
- [ ] `remote_provider_config.namespace` 指向目标 namespace。
- [ ] `remote_provider_config.pvc.claim_name` 指向已有 PVC。
- [ ] 如果 `sub_path_template` 使用 `{session_id}`，同时传入 `remote_provider_config.session_id`。
- [ ] 通过 `kubectl get pod -l app=sage-sandbox` 能看到 sandbox Pod。
- [ ] 通过 Sage 工具执行 `pwd` / `echo hello` 能拿到 JSON 成功结果。
