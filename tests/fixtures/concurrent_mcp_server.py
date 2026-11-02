"""Disposable stdio-only server; no filesystem or external network effects."""
import asyncio
from mcp.server.fastmcp import FastMCP

server = FastMCP("DianStar concurrency fixture")
active = 0


@server.tool()
async def probe(delay: float = 0.01) -> dict:
    global active
    active += 1
    peak = active
    try:
        await asyncio.sleep(delay)
        return {"delay": delay, "overlap": peak}
    finally:
        active -= 1


if __name__ == "__main__":
    server.run(transport="stdio")
