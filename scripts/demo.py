"""Usa o PoolClient direto (sem MCP). Uso: python scripts/demo.py [pool] """
import asyncio
import sys

from defime_mcp import PoolClient

POOL = sys.argv[1] if len(sys.argv) > 1 else "robinhood_0x9666ba19430380456bff5ac9d35a7ee2a6c6bc895bcc7ab92d3b64c8ec5cb611"


async def main() -> None:
    async with PoolClient() as c:
        ref = c.ref(POOL)
        snap = await c.get_pool(ref)
        print(snap.model_dump_json(indent=2))
        hist = await c.get_pool_history(ref, "7d", limit=12)
        print(hist.model_dump_json(indent=2))


asyncio.run(main())
