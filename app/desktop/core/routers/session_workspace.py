import re

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from common.services import agent_service

session_workspace_router = APIRouter(prefix="/api/sessions", tags=["Session Workspace"])


def _streaming_response_from_plan(plan, *, status_code: int = 200, headers=None):
    response_headers = {
        "Content-Length": str(plan.size),
        "Content-Disposition": f'inline; filename="{plan.filename}"',
    }
    if headers:
        response_headers.update(headers)
    return StreamingResponse(
        plan.iter_bytes(),
        status_code=status_code,
        headers=response_headers,
        media_type=plan.media_type,
    )


@session_workspace_router.get("/{session_id}/file_workspace/download")
async def download_session_file(session_id: str, request: Request):
    file_path = request.query_params.get("file_path")
    plan = await agent_service.prepare_session_read_plan(
        session_id,
        file_path,  # pyright: ignore[reportArgumentType]
    )
    return _streaming_response_from_plan(plan)


@session_workspace_router.get("/{session_id}/file_workspace/stream")
async def stream_session_file(session_id: str, request: Request):
    file_path = request.query_params.get("file_path")
    initial_plan = await agent_service.prepare_session_read_plan(
        session_id,
        file_path,  # pyright: ignore[reportArgumentType]
    )
    file_size = initial_plan.size
    range_header = request.headers.get("range")
    if range_header:
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
            end = min(end, file_size - 1)
            plan = await agent_service.prepare_session_read_plan(
                session_id,
                file_path,  # pyright: ignore[reportArgumentType]
                byte_range=(start, end),
            )
            return _streaming_response_from_plan(
                plan,
                status_code=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                },
            )
    return _streaming_response_from_plan(
        initial_plan,
        headers={"Accept-Ranges": "bytes"},
    )
