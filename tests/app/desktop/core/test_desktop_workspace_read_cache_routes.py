import asyncio
from dataclasses import dataclass

from starlette.requests import Request

from app.desktop.core.routers import agent as desktop_agent_router


@dataclass
class FakePlan:
    source: str = "cache"
    filename: str = "video.mp4"
    media_type: str = "video/mp4"
    size: int = 10
    mtime_ns: int = 1

    async def iter_bytes(self):
        yield b"0123456789"


def _request(path="/"):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": path.split("?", 1)[1].encode() if "?" in path else b"",
            "headers": [],
        }
    )


def test_desktop_download_uses_read_plan(monkeypatch):
    calls = {}

    async def fake_prepare(agent_id, file_path, **kwargs):
        calls.update({"agent_id": agent_id, "file_path": file_path})
        return FakePlan(filename="a.txt", media_type="text/plain", size=6)

    monkeypatch.setattr(
        desktop_agent_router.agent_service,
        "prepare_desktop_agent_read_plan",
        fake_prepare,
    )

    response = asyncio.run(
        desktop_agent_router.download_file("agent-a", _request("/?file_path=a.txt"))
    )

    assert response.media_type == "text/plain"
    assert calls == {"agent_id": "agent-a", "file_path": "a.txt"}


def test_desktop_stream_passes_range_to_read_plan(monkeypatch):
    calls = []

    async def fake_prepare(agent_id, file_path, *, byte_range=None):
        calls.append(
            {
                "agent_id": agent_id,
                "file_path": file_path,
                "byte_range": byte_range,
            }
        )
        return FakePlan(size=10 if byte_range is None else 4)

    monkeypatch.setattr(
        desktop_agent_router.agent_service,
        "prepare_desktop_agent_read_plan",
        fake_prepare,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"file_path=video.mp4",
            "headers": [(b"range", b"bytes=2-5")],
        }
    )

    response = asyncio.run(desktop_agent_router.stream_file("agent-a", request))

    assert response.status_code == 206
    assert calls[-1]["byte_range"] == (2, 5)


def test_desktop_session_stream_route_uses_session_plan(monkeypatch):
    from app.desktop.core.routers import session_workspace

    calls = []

    async def fake_prepare(session_id, file_path, *, byte_range=None):
        calls.append(
            {
                "session_id": session_id,
                "file_path": file_path,
                "byte_range": byte_range,
            }
        )
        return FakePlan(size=10 if byte_range is None else 3)

    monkeypatch.setattr(
        session_workspace.agent_service,
        "prepare_session_read_plan",
        fake_prepare,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"file_path=messages.json",
            "headers": [(b"range", b"bytes=1-3")],
        }
    )

    response = asyncio.run(session_workspace.stream_session_file("session-a", request))

    assert response.status_code == 206
    assert calls[-1] == {
        "session_id": "session-a",
        "file_path": "messages.json",
        "byte_range": (1, 3),
    }
