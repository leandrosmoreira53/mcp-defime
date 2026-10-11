import json
from pathlib import Path

import pytest

from defime_mcp.client import PoolClient
from defime_mcp.config import Settings
from defime_mcp.http import HttpClient
from defime_mcp.ratelimit import HostLimiter

FIX = Path(__file__).parent / "fixtures"
POOL = "0x9666ba19430380456bff5ac9d35a7ee2a6c6bc895bcc7ab92d3b64c8ec5cb611"
SLUG = f"robinhood_{POOL}"
DS_URL = f"https://api.dexscreener.com/latest/dex/pairs/robinhood/{POOL}"
GT_URL = f"https://api.geckoterminal.com/api/v2/networks/robinhood/pools/{POOL}"
GT_OHLCV_DAY = f"{GT_URL}/ohlcv/day"
GT_LIST = "https://api.geckoterminal.com/api/v2/networks/robinhood/pools"
GT_SEARCH = "https://api.geckoterminal.com/api/v2/search/pools"
DS_SEARCH = "https://api.dexscreener.com/latest/dex/search"


def fx(name: str):
    return json.loads((FIX / name).read_text())


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(HttpClient, "_backoff", staticmethod(lambda attempt: 0))
    monkeypatch.setattr(HostLimiter, "KNOWN", {})


def make_settings(tmp_path, **kw) -> Settings:
    base = dict(cache_path=tmp_path / "c.db", rate_limit_rps=1000, max_retries=2, max_retry_after=0.01,
                onchain_enabled=False, scope="all")  # on-chain e escopo: test_onchain.py, test_scope.py
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
async def client(settings):
    c = PoolClient(settings)
    yield c
    await c.aclose()


@pytest.fixture
def mock_pool(respx_mock):
    respx_mock.get(DS_URL).respond(json=fx("dexscreener_pair.json"))
    respx_mock.get(GT_URL).respond(json=fx("gt_pool.json"))
    return respx_mock
