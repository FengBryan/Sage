import asyncio
from dataclasses import dataclass

from starlette.requests import Request

from app.server.routers import agent as server_agent_router


@dataclass
class FakePlan:
    source: str = "source"
    filename: str = "a.txt"
    media_type: str = "text/plain"
    size: int = 6
    mtime_ns: int = 1

    async def iter_bytes(self):
        yield b"source"


def _request(path="/", user_id="user-a", role="user"):
    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": path.split("?", 1)[1].encode() if "?" in path else b"",
            "headers": [],
        }
    )
    req.state.user_claims = {"userid": user_id, "role": role}
    return req


def test_server_download_uses_read_plan(monkeypatch):
    calls = {}

    async def fake_prepare(agent_id, user_id, file_path, **kwargs):
        calls.update({"agent_id": agent_id, "user_id": user_id, "file_path": file_path})
        return FakePlan()

    monkeypatch.setattr(
        server_agent_router.agent_service,
        "prepare_server_agent_read_plan",
        fake_prepare,
    )
    request = _request("/?file_path=a.txt")

    response = asyncio.run(server_agent_router.download_file("agent-a", request))

    assert response.media_type == "text/plain"
    assert calls == {
        "agent_id": "agent-a",
        "user_id": "user-a",
        "file_path": "a.txt",
    }


def test_server_session_download_route_uses_session_plan(monkeypatch):
    from app.server.routers import session_workspace

    calls = {}

    async def fake_prepare(session_id, file_path, **kwargs):
        calls.update({"session_id": session_id, "file_path": file_path})
        return FakePlan()

    monkeypatch.setattr(
        session_workspace.agent_service,
        "prepare_session_read_plan",
        fake_prepare,
    )

    response = asyncio.run(
        session_workspace.download_session_file(
            "session-a",
            _request("/?file_path=messages.json"),
        )
    )

    assert response.media_type == "text/plain"
    assert calls == {"session_id": "session-a", "file_path": "messages.json"}
