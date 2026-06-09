# Workspace Read Cache With Pluggable Object Storage

## Purpose

Client workspace file reads currently go through backend HTTP endpoints that resolve files from local host paths and return them directly. This design adds an optional, pluggable read-through cache layer backed by configured object storage for client-facing workspace downloads and streams.

The cache is only an optimization. The authoritative source is always the workspace filesystem resolved by Sage. Writes, uploads, deletes, and listings keep their current behavior and do not update or delete cached objects.

## Scope

In scope:

- Agent workspace download and stream endpoints.
- Session workspace download and stream endpoints.
- A pluggable read-cache boundary that can support VM or remote sandbox file sources later.
- GCS and S3/RustFS object-cache providers behind one cache-store factory.

Out of scope:

- Agent or sandbox tool reads such as `FileSystemTool.file_read`.
- Workspace list, upload, delete, and directory zip caching.
- Object cache deletion on file deletion.
- Database-backed cache manifests.
- Direct signed URL responses to clients.

## Current Context

The desktop and server clients call HTTP file APIs such as:

- `GET /api/agent/{agent_id}/file_workspace/download?file_path=...`
- `GET /api/agent/{agent_id}/file_workspace/stream?file_path=...`

The server currently resolves paths under an agent workspace and returns files with `FileResponse` or `StreamingResponse`. Path safety is enforced by normalizing the requested path and requiring it to remain under the workspace root.

The new cache layer preserves that external contract. Clients still call the same HTTP APIs, and the backend continues to enforce authorization and path containment.

## API Surface

The cache layer applies only to content reads:

- `GET /api/agent/{agent_id}/file_workspace/download?file_path=...`
- `GET /api/agent/{agent_id}/file_workspace/stream?file_path=...`
- `GET /api/sessions/{session_id}/file_workspace/download?file_path=...`
- `GET /api/sessions/{session_id}/file_workspace/stream?file_path=...`

Agent-scope reads use the agent workspace as the authoritative source.

Session-scope reads use the session workspace resolved by `SessionManager` as the authoritative source. The whole session workspace is exposed for session file reads, with the same containment rule: `file_path` must resolve under the session workspace root.

Workspace listing, upload, and delete endpoints continue to read or mutate the filesystem directly. If session listing is added or already exists, it should list the session workspace directly and not use the cache.

## Architecture

Add a `WorkspaceReadCache` service between the routers and the final file response creation.

It depends on two small interfaces:

### FileSource

`FileSource` represents the authoritative filesystem source. First implementation:

- `HostWorkspaceFileSource`

Future implementations can represent VM or remote sandbox files without changing the cache policy.

Responsibilities:

- Resolve and validate a relative path under an allowed root.
- Return file stat data.
- Compute the configured content fingerprint when needed.
- Open an authoritative stream for download or range reads.

### ObjectCacheStore

`ObjectCacheStore` represents the object-cache backend. First implementation:

- `GCSObjectCacheStore`
- `S3ObjectCacheStore`

Responsibilities:

- `head(key)` returns object metadata.
- `open_stream(key, range=None)` returns cached object content.
- `put_stream(key, stream, metadata, content_type)` writes object content and metadata.
- `update_metadata(key, metadata)` refreshes metadata without changing object bytes when the provider supports it. If the provider cannot update metadata in place, it may rewrite the existing object or report that metadata repair is unsupported.

Routers and `agent_service` do not instantiate provider clients directly. They ask a small factory to create the configured `ObjectCacheStore`, and the factory routes to GCS or S3/RustFS based on `workspace_read_cache_provider`.

## Cache Keys

Cache keys must isolate deployment, scope, and path:

```text
{prefix}/{deployment_id}/agent/{agent_id}/{relative_path_hash}/{basename}
{prefix}/{deployment_id}/session/{session_id}/{relative_path_hash}/{basename}
```

Default prefix:

```text
workspace-cache
```

`relative_path_hash` is derived from the normalized workspace-relative path. `basename` is included only for readability. It is not used as a security boundary.

## Fingerprints

The cache uses a two-stage fingerprint.

Cheap fingerprint:

- file size
- filesystem `mtime_ns`

Strong fingerprint:

- For files `<= 3MB`: full SHA-256 of the file content.
- For files `> 3MB`: sampled SHA-256.

Sampled SHA-256 input includes:

- file size
- `mtime_ns`
- sample range descriptors
- the bytes from:
  - head 512KB
  - middle 512KB
  - tail 512KB

This keeps the common read path fast while still giving a stronger validation path when cheap metadata changes or cache metadata is incomplete.

## Object Metadata

Cached objects store validation metadata directly on the object:

- `sage-size`
- `sage-mtime-ns`
- `sage-hash-kind`
- `sage-content-hash`
- `sage-source-id`
- `sage-relative-path`

`sage-hash-kind` is either:

- `full_sha256`
- `sample_sha256`

No database manifest is required.

## Read Flow

1. Client requests a workspace file through an agent or session download/stream endpoint.
2. The router resolves the authoritative source:
   - agent workspace for agent endpoints
   - session workspace for session endpoints
3. The `FileSource` validates `file_path` under the source root and returns stat data.
4. `WorkspaceReadCache` builds the object-store key.
5. `WorkspaceReadCache` performs an object-store `head`.
6. If object metadata has matching `size + mtime_ns`, the backend proxies the cached object stream to the client.
7. If metadata is missing or the cheap fingerprint differs, the cache computes the source strong fingerprint.
8. If the strong fingerprint matches object metadata, the backend may return cached content and schedule metadata repair if needed.
9. If the strong fingerprint differs, object read fails, or object storage is unavailable, the backend returns the authoritative source content.
10. After returning authoritative source content for a stale or missing cache entry, the backend schedules a background sync that uploads the source content to object storage with fresh metadata.

The response to the client is never blocked on object upload completion.

## Streaming And Range Requests

Agent and session download endpoints can proxy either:

- Object-store stream on cache hit
- authoritative file stream on miss or fallback

Stream endpoints must preserve HTTP Range behavior:

- On cache hit, pass the requested byte range to the object read and return `206` with correct `Content-Range`.
- On fallback, use the authoritative source's range stream.

If a provider cannot support efficient range reads, it may fallback to source streaming rather than loading a full object into memory.

## Configuration

The cache is disabled by default. Missing or invalid cache configuration must preserve existing behavior.

Proposed configuration:

```text
workspace_read_cache_enabled=true|false
workspace_read_cache_provider=gcs|s3
workspace_read_cache_gcs_bucket=...
workspace_read_cache_gcs_prefix=workspace-cache
workspace_read_cache_gcs_credentials_json=...
workspace_read_cache_sample_threshold_bytes=3145728
workspace_read_cache_sample_chunk_bytes=524288
workspace_read_cache_deployment_id=...
```

Credentials may also be resolved via `GOOGLE_APPLICATION_CREDENTIALS`.

When `workspace_read_cache_provider=s3`, the cache uses the existing S3/RustFS settings:

```text
s3_endpoint=...
s3_access_key=...
s3_secret_key=...
s3_secure=true|false
s3_bucket_name=...
```

`deployment_id` prevents collisions when multiple Sage deployments share one bucket. Production deployments should set it explicitly.

## Error Handling

- Authoritative source errors preserve current API behavior.
- Path traversal or paths outside the workspace remain rejected.
- Object-store `head`, `get`, or `put` failures do not fail the request.
- If cache metadata matches but cached object streaming fails, fallback to the authoritative source and schedule cache repair.
- Background sync failures are logged and not retried inline. A later read can try again.
- Metadata repair failures are treated like sync failures: they are logged and do not affect the current response.

## Concurrency

Multiple concurrent stale reads for the same cache key should not all upload the same object.

Use an in-process lock or task registry keyed by cache key:

- The first request schedules the upload.
- Later requests can observe that sync is already pending and skip scheduling.

This is best-effort within one server process. Cross-process duplicate uploads are acceptable in the first implementation because object replacement is idempotent for the same source version.

## Security

The cache layer never trusts client paths. It only receives paths after the same workspace containment checks used today.

Object keys are derived from normalized relative paths and scope identifiers, not raw absolute host paths. Object metadata may include the relative path for diagnostics, but it must not include credentials or arbitrary host-only secrets.

Clients do not receive signed object-store URLs in the first implementation. All cached content is proxied through the backend so existing auth and access control remain in force.

## Testing

Unit tests:

- Fingerprint selection:
  - `<=3MB` uses full SHA-256.
  - `>3MB` uses sampled SHA-256.
- Cheap fingerprint hit returns cache without computing strong hash.
- Metadata missing or cheap mismatch computes strong fingerprint.
- Object-store head failure falls back to source.
- Object-store get failure falls back to source and schedules repair.
- Object-store put failure does not affect response.
- Cache-store factory routes `gcs` and `s3` providers to the correct backend.
- S3 provider reuses existing S3/RustFS configuration.
- Upload de-duplication only schedules one in-process sync for a cache key.

API tests:

- Cache disabled preserves current agent download behavior.
- Agent download returns cached bytes when metadata matches.
- Agent download returns source bytes and schedules sync when metadata differs.
- Agent stream preserves Range responses on cache hit and fallback.
- Session download reads from session workspace, not agent workspace.
- Session stream preserves Range responses.
- Path traversal remains rejected for agent and session APIs.

Regression tests:

- Workspace list, upload, and delete behavior remains unchanged.
- Agent tool reads are not affected by this cache.

## Open Extension Points

Future work can add:

- `SandboxFileSource` for VM or remote sandbox workspaces.
- Object lifecycle cleanup policy for stale cached objects.
- Signed URL response mode for deployments that want client-to-object-storage transfer.
- Cache metrics for hit, miss, fallback, upload success, and upload failure.
