"""Large catalogs traverse Hermes and the real SDK, with a bounded SSE parser."""

import asyncio
import json

import pytest


@pytest.mark.parametrize("size,accepted", [(128, True), (2 * 1024 * 1024, True), (8 * 1024 * 1024 + 1024, False)])
def test_catalog_size_boundary(monkeypatch, size, accepted):
    from tools import mcp_tool as core
    from tools import mcp_tool_transport as transport

    assert core._ensure_mcp_sdk()
    from mcp import ClientSession
    from mcp.shared.exceptions import MCPError

    httpx = core.sdk_httpx()

    def handle(request):
        if request.method != "POST":
            return httpx.Response(405)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-catalog", "version": "1"},
            }
        else:
            assert message["method"] == "tools/list"
            result = {"tools": [{
                "name": "large_catalog_tool", "description": "x" * size,
                "inputSchema": {"type": "object", "properties": {}},
            }]}
        event = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="event: message\ndata: " + json.dumps(event) + "\n\n")

    # Replace only the network boundary; Hermes still constructs its owned
    # client, body cap and SDK streams with the production parser option.
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    monkeypatch.setattr(transport, "_mcp_proxy_mounts", lambda *args: None)

    async def discover():
        server = core.MCPServerTask("synthetic-catalog")
        async with server._streamable_http_transport(
            "https://catalog.invalid/mcp", {}, 5.0, True, None, None, False, set()
        ) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                try:
                    catalog = await session.list_tools()
                except MCPError as exc:
                    assert not accepted
                    assert "exceeded the 8388608 byte limit" in str(exc)
                else:
                    assert accepted
                    assert len(catalog.tools) == 1
                    assert len(catalog.tools[0].description) == size

    asyncio.run(asyncio.wait_for(discover(), 15))
