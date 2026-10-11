"""Chama as APIs reais. Rodar manualmente: pytest -m live -s"""

import pytest

from defime_mcp.client import PoolClient
from defime_mcp.config import Settings

from .conftest import SLUG

pytestmark = pytest.mark.live


async def test_live_pool_and_cross_check(tmp_path):
    async with PoolClient(Settings(cache_path=tmp_path / "c.db")) as c:
        snap = await c.get_pool(c.ref(SLUG))
        print(snap.model_dump_json(indent=2))
        assert snap.metrics.tvl_usd and snap.fee_pct
        # tolerância documentada em docs/API.md: fontes divergem até ~20% no TVL
        if snap.cross_check.tvl_divergence_pct is not None:
            assert snap.cross_check.tvl_divergence_pct < 25
        again = await c.get_pool(c.ref(SLUG))
        assert again.cache.hit


async def test_live_history(tmp_path):
    async with PoolClient(Settings(cache_path=tmp_path / "c.db")) as c:
        h = await c.get_pool_history(c.ref(SLUG), "7d", limit=10)
        assert h.points
