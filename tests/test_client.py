import httpx
import pytest

from defime_mcp.client import PoolClient
from defime_mcp.exceptions import FeatureUnavailable, InvalidPoolId, PoolNotFound, RateLimited, UpstreamError

from .conftest import (DS_SEARCH, DS_URL, GT_LIST, GT_OHLCV_DAY, GT_SEARCH, GT_URL, POOL, SLUG, fx,
                       make_settings)


async def test_get_pool_success(client, mock_pool):
    snap = await client.get_pool(client.ref(SLUG))
    assert snap.sources == ["dexscreener", "geckoterminal"]
    assert snap.name == "TE / ETH" and snap.fee_pct == 4.88 and snap.dex_version == "v4"
    assert snap.metrics.tvl_usd == 2315.82  # DexScreener primário
    assert snap.metrics.apr_24h_pct == pytest.approx(5228.66 * 0.0488 / 2315.82 * 365 * 100)
    assert snap.cross_check.tvl_usd == {"dexscreener": 2315.82, "geckoterminal": 2659.3808}
    assert snap.cross_check.tvl_divergence_pct == pytest.approx(14.84, abs=0.01)
    assert {d.field for d in snap.derived_fields} == {"metrics.fees_24h_usd", "metrics.apr_24h_pct"}
    # on-chain desligado neste teste: a taxa do LP não é suposta, fica ausente e declarada
    assert snap.onchain is None
    assert set(snap.missing_fields) == {"metrics.fees_24h_lp_usd", "metrics.apr_24h_lp_pct", "lp_fee_pct"}
    assert snap.cache.hit is False


async def test_cache_hit_then_expire(tmp_path, mock_pool):
    c = PoolClient(make_settings(tmp_path, ttl_snapshot=60))
    ref = c.ref(SLUG)
    await c.get_pool(ref)
    second = await c.get_pool(ref)
    assert second.cache.hit is True and mock_pool.calls.call_count == 2  # 1 DS + 1 GT
    await c.aclose()

    c2 = PoolClient(make_settings(tmp_path, ttl_snapshot=0))
    third = await c2.get_pool(ref)
    assert third.cache.hit is False and mock_pool.calls.call_count == 4
    await c2.aclose()


async def test_stale_if_error(tmp_path, respx_mock):
    ds = respx_mock.get(DS_URL).respond(json=fx("dexscreener_pair.json"))
    gt = respx_mock.get(GT_URL).respond(json=fx("gt_pool.json"))
    c = PoolClient(make_settings(tmp_path, ttl_snapshot=0))
    await c.get_pool(c.ref(SLUG))
    ds.respond(503)
    gt.respond(503)
    snap = await c.get_pool(c.ref(SLUG))
    assert snap.cache.stale is True and snap.cache.hit is True
    assert any("indisponível" in n for n in snap.notes)
    await c.aclose()


async def test_one_source_down(client, respx_mock):
    respx_mock.get(DS_URL).respond(500)
    respx_mock.get(GT_URL).respond(json=fx("gt_pool.json"))
    snap = await client.get_pool(client.ref(SLUG))
    assert snap.sources == ["geckoterminal"] and snap.metrics.tvl_usd == 2659.3808
    assert any("dexscreener: indisponível" in n for n in snap.notes)


async def test_pool_not_found(client, respx_mock):
    respx_mock.get(DS_URL).respond(json={"schemaVersion": "1.0.0", "pairs": None})
    respx_mock.get(GT_URL).respond(404, json={"errors": [{"status": "404"}]})
    with pytest.raises(PoolNotFound):
        await client.get_pool(client.ref(SLUG))


async def test_timeout_all_sources(client, respx_mock):
    respx_mock.get(DS_URL).mock(side_effect=httpx.ReadTimeout("t"))
    respx_mock.get(GT_URL).mock(side_effect=httpx.ConnectTimeout("t"))
    with pytest.raises(UpstreamError, match="sem resposta"):
        await client.get_pool(client.ref(SLUG))


async def test_429_retry_after_then_success(client, respx_mock):
    route = respx_mock.get(GT_URL)
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json=fx("gt_pool.json"))]
    respx_mock.get(DS_URL).respond(json=fx("dexscreener_pair.json"))
    snap = await client.get_pool(client.ref(SLUG))
    assert route.call_count == 2 and snap.fee_pct == 4.88


async def test_429_exhausted(client, respx_mock):
    respx_mock.get("https://api.geckoterminal.com/api/v2/search/pools").respond(429)
    respx_mock.get(DS_SEARCH).respond(429)
    with pytest.raises(RateLimited):
        await client.search_pools("TE")


async def test_5xx_retry(client, respx_mock):
    route = respx_mock.get(DS_URL)
    route.side_effect = [httpx.Response(502), httpx.Response(200, json=fx("dexscreener_pair.json"))]
    respx_mock.get(GT_URL).respond(json=fx("gt_pool.json"))
    await client.get_pool(client.ref(SLUG))
    assert route.call_count == 2


async def test_schema_changed_source_is_reported(client, respx_mock):
    respx_mock.get(DS_URL).respond(json={"pairs": [{"pairAddress": POOL}]})  # faltam tokens
    respx_mock.get(GT_URL).respond(json=fx("gt_pool.json"))
    snap = await client.get_pool(client.ref(SLUG))
    assert snap.sources == ["geckoterminal"] and any("formato" in n for n in snap.notes)


async def test_history_30d(client, mock_pool):
    mock_pool.get(GT_OHLCV_DAY).respond(json=fx("gt_ohlcv_day.json"))
    h = await client.get_pool_history(client.ref(SLUG), "30d", limit=30)
    # 9 candles cobrindo 16 dias (04/09 a 19/09): o GeckoTerminal omite os 7 dias sem swap
    assert h.granularity == "day" and len(h.points) == 16
    assert sum(p.no_swaps for p in h.points) == 7
    assert all(p.volume_usd == 0 for p in h.points if p.no_swaps)
    ts = [p.timestamp for p in h.points]
    assert ts == sorted(ts) and all(b - a == 86400 for a, b in zip(ts, ts[1:]))
    last = h.points[-1]
    assert last.volume_usd == pytest.approx(5151.270359155122)
    assert last.apr_pct == pytest.approx(5151.270359155122 * 0.0488 / 2315.82 * 365 * 100)
    assert h.summary["max_apr_pct"] == pytest.approx(last.apr_pct)
    # a média é sobre os 16 dias, não sobre os 9 com candle (antes saía 16/9 = 1,78x maior)
    total = sum(p.volume_usd for p in h.points)
    assert h.summary["avg_apr_pct"] == pytest.approx(total * 0.0488 / 2315.82 * 365 * 100 / 16)
    assert h.lp_fee_pct is None and h.summary["avg_apr_lp_pct"] is None


async def test_history_does_not_fill_before_pool_birth(client, mock_pool):
    mock_pool.get(GT_OHLCV_DAY).respond(json=fx("gt_ohlcv_day.json"))
    h = await client.get_pool_history(client.ref(SLUG), "30d", limit=200)
    assert h.points[0].timestamp == 1788480000  # dia de criação (04/09), não 30 dias antes


async def test_history_limit_and_invalid(client, mock_pool):
    mock_pool.get(GT_OHLCV_DAY).respond(json=fx("gt_ohlcv_day.json"))
    h = await client.get_pool_history(client.ref(SLUG), "30d", limit=3)
    assert len(h.points) == 3 and any("últimos 3" in n for n in h.notes)
    with pytest.raises(InvalidPoolId):
        await client.get_pool_history(client.ref(SLUG), "2y")


async def test_list_pools_sort_filter(client, respx_mock):
    respx_mock.get(GT_LIST).respond(json=fx("gt_pools_list.json"))
    res = await client.list_pools("robinhood", sort_by="tvl", limit=10)
    assert [i.name for i in res.items][:2] == ["BIG / USDG", "NOFEE / USDG"]
    res2 = await client.list_pools("robinhood", sort_by="apr", min_tvl_usd=2000)
    assert res2.items[0].name == "te / WETH" and all((i.tvl_usd or 0) >= 2000 for i in res2.items)
    nofee = next(i for i in res.items if i.name == "NOFEE / USDG")
    assert nofee.fee_pct is None and nofee.apr_24h_pct is None  # nada inventado


async def test_list_category_needs_scope(client):
    with pytest.raises(FeatureUnavailable):
        await client.list_pools(category="rwa")


async def test_search(client, respx_mock):
    respx_mock.get(GT_SEARCH).respond(json=fx("gt_search.json"))
    res = await client.search_pools("TE", "robinhood")
    assert res.count == 1 and res.items[0].pool_id == POOL and res.items[0].fee_pct == 4.88


async def test_search_fallback_dexscreener(client, respx_mock):
    respx_mock.get(GT_SEARCH).respond(500)
    respx_mock.get(DS_SEARCH).respond(json=fx("dexscreener_pair.json"))
    res = await client.search_pools("TE", "robinhood")
    assert res.sources == ["dexscreener"] and res.items[0].name == "TE / ETH"

