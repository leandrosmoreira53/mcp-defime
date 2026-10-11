import pytest

from defime_mcp.exceptions import UpstreamSchemaChanged
from defime_mcp.models import Metrics, PoolSnapshot, apr_pct, divergence_pct
from defime_mcp.sources.dexscreener import parse_pair
from defime_mcp.sources.geckoterminal import parse_pool

from .conftest import fx


def test_apr_formula_matches_reference_values():
    # valores de referência de um agregador para o pool de teste: 3986.53%
    assert apr_pct(5214.69, 4.88, 2329.95) == pytest.approx(3986.5258, rel=1e-6)
    # histórico diário de referência (fees 251.38 / tvl 2329.95 × 365)
    assert apr_pct(5151.270359155122, 4.88, 2329.95) == pytest.approx(3938.0427750497242, rel=1e-9)


def test_apr_missing_inputs():
    assert apr_pct(None, 4.88, 100) is None
    assert apr_pct(100, None, 100) is None
    assert apr_pct(100, 1, 0) is None


def test_divergence():
    assert divergence_pct({"a": 100, "b": 110}) == 10.0
    assert divergence_pct({"a": 100}) is None


def test_missing_fields():
    snap = PoolSnapshot(sources=["x"], chain="robinhood", pool_id="0x1", metrics=Metrics(tvl_usd=1.0)).compute_missing()
    assert "metrics.tvl_usd" not in snap.missing_fields
    assert "metrics.apr_24h_pct" in snap.missing_fields and "fee_pct" in snap.missing_fields


def test_parse_dexscreener():
    p = parse_pair(fx("dexscreener_pair.json")["pairs"][0], "robinhood")
    assert p.base_token.symbol == "TE" and p.dex_version == "v4" and p.tvl_usd == 2315.82
    assert p.base_token.address == "0xb1969f6604ca1ae7a2cd3f1827876e914594ca2d"


def test_parse_gecko_fee_and_tokens():
    d = fx("gt_pool.json")
    p = parse_pool(d["data"], d["included"])
    assert p.fee_pct == 4.88 and not p.fee_pct_derived and p.quote_token.symbol == "WETH" and p.name == "te / WETH"


def test_parse_gecko_fee_from_name():
    d = fx("gt_pool.json")
    del d["data"]["attributes"]["pool_fee_percentage"]
    p = parse_pool(d["data"], d["included"])
    assert p.fee_pct == 4.88 and p.fee_pct_derived


def test_schema_changed():
    with pytest.raises(UpstreamSchemaChanged):
        parse_pair({"pairAddress": "0x1"}, "robinhood")  # sem baseToken
    with pytest.raises(UpstreamSchemaChanged):
        parse_pool({"id": "robinhood_0x1", "attributes": {}})  # sem address
