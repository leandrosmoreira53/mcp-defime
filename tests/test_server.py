import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from defime_mcp import server as srv
from defime_mcp.client import PoolClient

from .conftest import GT_OHLCV_DAY, SLUG, fx, make_settings


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "_client", PoolClient(make_settings(tmp_path)))
    return srv.build_server(make_settings(tmp_path))


def _payload(result):
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


async def test_tools_registered(mcp):
    names = {t.name for t in await mcp.list_tools()}
    assert names == {"defime_get_pool", "defime_get_pool_history", "defime_list_pools", "defime_search_pools",
                     "defime_compare_pools"}


async def test_defime_get_pool(mcp, mock_pool):
    data = _payload(await mcp.call_tool("defime_get_pool", {"pool": SLUG}))
    assert data["chain"] == "robinhood" and data["fee_pct"] == 4.88 and "retrieved_at" in data
    assert data["kind"] == "current" and data["sources"]


async def test_defime_history(mcp, mock_pool):
    mock_pool.get(GT_OHLCV_DAY).respond(json=fx("gt_ohlcv_day.json"))
    data = _payload(await mcp.call_tool("defime_get_pool_history", {"pool": SLUG, "timeframe": "30d", "limit": 5}))
    assert data["kind"] == "historical" and len(data["points"]) == 5


async def test_tool_error_is_friendly(mcp):
    with pytest.raises(ToolError, match="pool_id inválido"):
        await mcp.call_tool("defime_get_pool", {"pool": "0x123"})


async def test_bearer_middleware():
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["path"])

    mw = srv.BearerAuth(app, "x" * 32)
    sent = []

    async def send(msg):
        sent.append(msg)

    await mw({"type": "http", "path": "/mcp", "headers": []}, None, send)
    assert sent[0]["status"] == 401 and not calls
    await mw({"type": "http", "path": "/mcp", "headers": [(b"authorization", b"Bearer " + b"x" * 32)]}, None, send)
    await mw({"type": "http", "path": "/healthz", "headers": []}, None, send)
    assert calls == ["/mcp", "/healthz"]


async def test_lifespan_does_not_close_shared_client(mcp, mock_pool):
    """Regressão: com stateless_http o lifespan roda a CADA requisição. Ele fechava o cliente e a
    segunda chamada HTTP falhava com 'Cannot operate on a closed database' (medido em 19/09)."""
    for _ in range(2):
        async with mcp._mcp_server.lifespan(mcp._mcp_server):
            pass
        data = _payload(await mcp.call_tool("defime_get_pool", {"pool": SLUG}))
        assert data["chain"] == "robinhood"


async def test_defime_compare_pools(mcp, mock_pool):
    data = _payload(await mcp.call_tool("defime_compare_pools", {"pools": [SLUG, "robinhood_0x123"]}))
    assert data["kind"] == "comparison" and len(data["rows"]) == 2
    ok, bad = data["rows"]
    assert ok["name"] == "TE / ETH" and ok["turnover_24h"] == pytest.approx(5228.66 / 2315.82)
    assert bad["error"] and "inválido" in bad["error"]           # um pool ruim não derruba o outro
    assert data["leaders"]["tvl_usd"] == "TE / ETH"


async def test_defime_compare_pools_needs_two(mcp):
    with pytest.raises(ToolError, match="2 a 5"):
        await mcp.call_tool("defime_compare_pools", {"pools": [SLUG, SLUG]})
