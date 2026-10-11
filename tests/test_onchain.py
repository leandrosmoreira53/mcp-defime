"""Fonte on-chain: decodificação conferida contra respostas REAIS do RPC (scripts/record_onchain.py)."""

import httpx
import pytest

from defime_mcp.client import PoolClient
from defime_mcp.exceptions import NotFoundHTTP, UpstreamError
from defime_mcp.http import HttpClient
from defime_mcp.ids import parse_pool_ref
from defime_mcp.sources.onchain import (SEL, OnChain, _signed, keccak256, pool_state_slot, selector, v3_lp_share,
                                    v4_fees)

from .conftest import SLUG, fx, make_settings

RPC = "https://rpc.test"
RPC2 = "https://rpc2.test"
PLTR = "robinhood_0x851680416a4f4e1c463d45171d61acddbc8554c0"


def replay(respx_mock, fixture: str, url: str = RPC):
    """Devolve as respostas gravadas na mesma ordem e confere que o pedido é o gravado (sem o id)."""
    exchanges = list(fx(fixture)["exchanges"])

    def side_effect(request: httpx.Request):
        import json
        ex = exchanges.pop(0)
        sent = json.loads(request.content)
        assert [(c["method"], c["params"]) for c in sent] == [(c["method"], c["params"]) for c in ex["request"]]
        return httpx.Response(200, json=ex["response"])

    return respx_mock.post(url).mock(side_effect=side_effect)


def test_selectors_are_keccak_of_signatures():
    sigs = {"token0": "token0()", "token1": "token1()", "fee": "fee()", "tickSpacing": "tickSpacing()",
            "slot0": "slot0()", "liquidity": "liquidity()", "decimals": "decimals()", "symbol": "symbol()",
            "extsload": "extsload(bytes32)", "poolKeys": "poolKeys(bytes25)"}
    assert {k: selector(v) for k, v in sigs.items()} == SEL


def test_v3_lp_share_reads_fee_protocol():
    assert v3_lp_share(102) == pytest.approx((5 / 6, 1 / 6))   # 0x66: 1/6 nos dois sentidos
    assert v3_lp_share(0) == (1.0, 0.0)                          # sem corte
    assert v3_lp_share(0x46) == pytest.approx((0.75, 0.25))      # sentidos diferentes → o maior corte


def test_v4_fees_protocol_charged_on_input():
    # UPS/USDG medida na chain: lpFee 3000, protocolFee 500 → evento grava 3499 pips
    total, lp, pf = v4_fees(500 | (500 << 12), 3000)
    assert total == pytest.approx(0.34985) and lp == pytest.approx(0.29985) and pf == pytest.approx(0.05)
    assert v4_fees(0, 3000) == (0.3, 0.3, 0.0)


def test_signed_int24():
    assert _signed(0xFFFDA8, 24) == -600 and _signed(62889, 24) == 62889


def test_pool_state_slot_is_mapping_6():
    pid = "0x" + "11" * 32
    assert pool_state_slot(pid) == int.from_bytes(keccak256(bytes.fromhex("11" * 32) + (6).to_bytes(32, "big")), "big")


async def test_v3_pltr_lp_gets_five_sixths(respx_mock, settings):
    replay(respx_mock, "onchain_v3_pltr_usdg.json")
    http = HttpClient(settings)
    st = await OnChain(http, [RPC]).read(parse_pool_ref(PLTR))
    await http.aclose()
    assert st.protocol == "uniswap_v3" and st.fee_tier_pct == 0.3
    assert st.lp_fee_pct == pytest.approx(0.25) and st.protocol_fee_share == pytest.approx(1 / 6)
    assert st.token0.symbol == "USDG" and st.token0.decimals == 6 and st.token1.symbol == "PLTR"
    assert st.tick_spacing == 60
    assert 50 < st.price_token0_per_token1 < 1000   # USDG por PLTR, ordem de grandeza de ação
    gravado = fx("onchain_v3_pltr_usdg.json")["decoded"]
    assert st.model_dump(mode="json", exclude={"rpc"}) == {k: v for k, v in gravado.items() if k != "rpc"}


async def test_v4_te_eth_pool_key_reproduces_pool_id(respx_mock, settings):
    replay(respx_mock, "onchain_v4_te_eth.json")
    http = HttpClient(settings)
    st = await OnChain(http, [RPC]).read(parse_pool_ref(SLUG))
    await http.aclose()
    assert st.protocol == "uniswap_v4" and st.fee_tier_pct == 4.88
    assert st.protocol_fee_pct == pytest.approx(0.1) and st.lp_fee_pct == pytest.approx(4.88 * 0.999)
    assert st.token0.symbol == "ETH" and st.token1.symbol == "TE"
    assert not any("não reproduz" in n for n in st.notes)
    assert st.hooks == "0x" + "0" * 40 and st.dynamic_fee is False


async def test_v4_uninitialized_is_not_found(respx_mock, settings):
    zero = "0x" + "0" * 64
    respx_mock.post(RPC).mock(side_effect=[
        httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 0, "result": "0x10"}]),
        httpx.Response(200, json=[{"jsonrpc": "2.0", "id": i, "result": zero} for i in range(3)]),
    ])
    http = HttpClient(settings)
    with pytest.raises(NotFoundHTTP):  # "não existe" não é falha de RPC: não tenta o próximo
        await OnChain(http, [RPC]).read(parse_pool_ref("robinhood_0x" + "ab" * 32))
    await http.aclose()


async def test_rpc_fallback_to_second_url(respx_mock, settings):
    respx_mock.post(RPC).respond(503)
    replay(respx_mock, "onchain_v3_pltr_usdg.json", url=RPC2)
    http = HttpClient(settings)
    st = await OnChain(http, [RPC, RPC2]).read(parse_pool_ref(PLTR))
    await http.aclose()
    assert st.rpc == RPC2 and st.lp_fee_pct == pytest.approx(0.25)


async def test_client_publishes_lp_apr_and_keeps_nominal(tmp_path, mock_pool):
    replay(mock_pool, "onchain_v4_te_eth.json")
    c = PoolClient(make_settings(tmp_path, onchain_enabled=True, robinhood_rpc_urls=(RPC,)))
    snap = await c.get_pool(c.ref(SLUG))
    await c.aclose()
    assert snap.sources == ["dexscreener", "geckoterminal", "onchain"]
    assert snap.fee_pct == 4.88 and snap.lp_fee_pct == pytest.approx(4.87512)
    m = snap.metrics
    assert m.apr_24h_lp_pct == pytest.approx(m.apr_24h_pct * 4.87512 / 4.88)
    assert m.fees_24h_lp_usd == pytest.approx(m.volume_24h_usd * 4.87512 / 100)
    assert snap.onchain["block"] == fx("onchain_v4_te_eth.json")["decoded"]["block"]
    # o preço pelo sqrtPrice, casado por endereço com a orientação do agregador, bate com o DexScreener
    precos = snap.cross_check.price_quote_per_base
    assert precos["onchain"] == pytest.approx(precos["dexscreener"], rel=1e-3)
    assert "lp_fee_pct" not in snap.missing_fields


def test_price_orientation_never_guesses():
    from defime_mcp.client import _chain_price_quote_per_base
    from defime_mcp.sources.onchain import OnchainState, OnchainToken
    st = OnchainState(rpc=RPC, block=1, protocol="uniswap_v3", token0=OnchainToken(address="0xa"),
                      token1=OnchainToken(address="0xb"), price_token1_per_token0=2.0, price_token0_per_token1=0.5)
    assert _chain_price_quote_per_base(st, "0xa", "0xb") == 2.0
    assert _chain_price_quote_per_base(st, "0xb", "0xa") == 0.5
    assert _chain_price_quote_per_base(st, "0xa", "0xc") is None  # endereço que não casa → None


async def test_client_onchain_down_is_a_note_not_an_error(tmp_path, mock_pool):
    mock_pool.post(RPC).respond(503)
    c = PoolClient(make_settings(tmp_path, onchain_enabled=True, robinhood_rpc_urls=(RPC,)))
    snap = await c.get_pool(c.ref(SLUG))
    await c.aclose()
    assert snap.lp_fee_pct is None and "lp_fee_pct" in snap.missing_fields
    assert any(n.startswith("onchain: indisponível") for n in snap.notes)
    assert snap.metrics.apr_24h_lp_pct is None and snap.metrics.apr_24h_pct is not None


async def test_v3_reader_rejects_non_uniswap_slot0(respx_mock, settings):
    """Medido em 19/09: pools que respondem fee() mas têm slot0 de 1 palavra (outro AMM) saíam com LP 0%."""
    w = "0x" + format(3000, "064x")
    respx_mock.post(RPC).mock(side_effect=[
        httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 0, "result": "0x10"}]),
        httpx.Response(200, json=[{"jsonrpc": "2.0", "id": i, "result": w} for i in range(6)]),
    ])
    http = HttpClient(settings)
    with pytest.raises(NotFoundHTTP, match="não é pool Uniswap V3"):
        await OnChain(http, [RPC]).read(parse_pool_ref(PLTR))
    await http.aclose()


async def test_v4_dynamic_fee_with_hook_is_null_not_zero(respx_mock, settings):
    """lpFee 0 no slot0 + flag 0x800000 + hook: a taxa é do hook, não zero (SPY/NVDA/TSLA V4 em 19/09)."""
    pid = "0x" + "cd" * 32
    slot0 = (1 << 96) | (0 << 160)  # sqrtPrice != 0, tick 0, protocolFee 0, lpFee 0
    key = [0x5FC5360D0400A0FD4F2AF552ADD042D716F1D168, 0x117CC2133C37B721F49DE2A7A74833232B3B4C0C,
           0x800000, 60, 0xA0E8FBFF13E24AF2B5E61A72800E08A161BDE080]
    respx_mock.post(RPC).mock(side_effect=[
        httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 0, "result": "0x10"}]),
        httpx.Response(200, json=[
            {"jsonrpc": "2.0", "id": 0, "result": "0x" + format(slot0, "064x")},
            {"jsonrpc": "2.0", "id": 1, "result": "0x" + format(10 ** 18, "064x")},
            {"jsonrpc": "2.0", "id": 2, "result": "0x" + "".join(format(k, "064x") for k in key)},
        ]),
    ])
    http = HttpClient(settings)
    st = await OnChain(http, [RPC]).read(parse_pool_ref("robinhood_" + pid))
    await http.aclose()
    assert st.dynamic_fee is True and st.lp_fee_pct is None and st.fee_tier_pct is None
    assert any("definida pelo hook" in n for n in st.notes)
