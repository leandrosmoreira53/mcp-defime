"""Escopo RWA da Robinhood + cripto blue chip. Classificação por contrato, nunca por símbolo."""

import pytest

from defime_mcp.client import PoolClient
from defime_mcp.exceptions import InvalidPoolId, OutOfScope
from defime_mcp.http import HttpClient
from defime_mcp.sources.onchain import NATIVE, OnChain
from defime_mcp.universe import ROBINHOOD_STOCK_BEACON, Scope, TokenClass

from .conftest import DS_URL, GT_LIST, GT_SEARCH, GT_URL, SLUG, fx, make_settings
from .test_onchain import RPC, replay

USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
TE = "0xb1969f6604ca1ae7a2cd3f1827876e914594ca2d"
GT_TOKEN_POOLS = "https://api.geckoterminal.com/api/v2/networks/robinhood/tokens/{}/pools"


def scoped(tmp_path, **kw):
    return PoolClient(make_settings(tmp_path, scope="rwa_bluechip", onchain_enabled=True,
                                      robinhood_rpc_urls=(RPC,), **kw))


def fake_facts(monkeypatch, rwa: set[str]):
    """Substitui a leitura de beacon: `rwa` recebem o beacon da Robinhood, o resto nenhum."""
    calls = []

    async def facts(self, addresses):
        calls.append(sorted(addresses))
        return {a.lower(): {"beacon": ROBINHOOD_STOCK_BEACON if a.lower() in rwa else None,
                            "name": f"{a[-4:]} – Robinhood Token" if a.lower() in rwa else "Meme",
                            "symbol": a[-4:]} for a in addresses}

    monkeypatch.setattr(OnChain, "token_facts", facts)
    return calls


async def test_token_facts_real_rpc_decode(respx_mock, settings):
    replay(respx_mock, "onchain_token_facts.json")
    rec = fx("onchain_token_facts.json")
    http = HttpClient(settings)
    facts = await OnChain(http, [RPC]).token_facts(list(rec["tokens"].values()))
    await http.aclose()
    t = rec["tokens"]
    assert facts[t["te_rwa"]]["beacon"] == ROBINHOOD_STOCK_BEACON
    assert facts[t["mu_rwa"]]["beacon"] == ROBINHOOD_STOCK_BEACON
    assert "Micron Technology" in facts[t["mu_rwa"]]["name"]
    assert facts[t["mog_meme"]]["beacon"] is None
    # um "USDC" que só copia o símbolo: sem beacon, e não está na lista de stables → fora
    assert facts[t["fake_usdc"]]["symbol"] == "USDC" and facts[t["fake_usdc"]]["beacon"] is None


async def test_classify_by_contract_not_symbol(tmp_path, respx_mock):
    replay(respx_mock, "onchain_token_facts.json")
    t = fx("onchain_token_facts.json")["tokens"]
    c = scoped(tmp_path)
    got = await c.scope.classify([NATIVE, WETH, USDG, *t.values()])
    assert got[NATIVE].kind == "bluechip" and got[WETH].kind == "bluechip" and got[USDG].kind == "stable"
    assert got[t["te_rwa"]].kind == "rwa" and got[t["mu_rwa"]].kind == "rwa"
    assert got[t["mog_meme"]].kind is None and got[t["fake_usdc"]].kind is None
    # segunda vez vem do cache: nenhuma ida ao RPC (o replay levantaria se houvesse outra chamada)
    again = await c.scope.classify(list(t.values()))
    assert again[t["te_rwa"]].kind == "rwa"
    await c.aclose()


@pytest.mark.parametrize("a,b,cat", [
    ("rwa", "stable", "rwa"), ("rwa", "bluechip", "rwa"), ("bluechip", "stable", "bluechip"),
    ("bluechip", "bluechip", "bluechip"), ("stable", "stable", None), (None, "bluechip", None),
    (None, "stable", None), ("rwa", None, None),
])
def test_pool_category(a, b, cat):
    tc = lambda k: TokenClass(address="0x", kind=k, evidence="t")  # noqa: E731
    assert Scope.pool_category(tc(a), tc(b)) == cat


async def test_rwa_pool_in_scope_te_eth(tmp_path, mock_pool, monkeypatch):
    fake_facts(monkeypatch, {TE})
    mock_pool.post(RPC).respond(503)  # leitura do pool falha; a de beacon está simulada
    c = scoped(tmp_path)
    snap = await c.get_pool(c.ref(SLUG))
    await c.aclose()
    assert snap.category == "rwa"
    assert snap.base_token.kind == "rwa" and snap.quote_token.kind == "bluechip"
    assert snap.base_token.name.endswith("Robinhood Token")


async def test_meme_pool_out_of_scope_and_cached(tmp_path, mock_pool, monkeypatch):
    calls = fake_facts(monkeypatch, set())  # TE deixa de ser RWA → TE/ETH vira "memecoin/ETH"
    mock_pool.post(RPC).respond(503)
    c = scoped(tmp_path)
    with pytest.raises(OutOfScope, match="não é token RWA da Robinhood"):
        await c.get_pool(c.ref(SLUG))
    n = mock_pool.calls.call_count
    with pytest.raises(OutOfScope):
        await c.get_pool(c.ref(SLUG))
    assert mock_pool.calls.call_count == n and len(calls) == 1  # 2ª vez: cache, sem rede
    await c.aclose()


async def test_other_chain_rejected(tmp_path):
    c = scoped(tmp_path)
    with pytest.raises(OutOfScope, match="só a Robinhood Chain"):
        c.ref("base_0x" + "1" * 40)
    with pytest.raises(OutOfScope):
        await c.list_pools(chain="ethereum")
    await c.aclose()


async def test_search_drops_out_of_scope(tmp_path, respx_mock, monkeypatch):
    fake_facts(monkeypatch, set())  # o token base do fixture (…00b1) não é RWA
    respx_mock.get(GT_SEARCH).respond(json=fx("gt_search.json"))
    c = scoped(tmp_path)
    res = await c.search_pools("TE", chain=None)
    await c.aclose()
    assert res.count == 0 and res.chain == "robinhood"
    assert any("fora do escopo" in n for n in res.notes)


async def test_scoped_list_filters_and_categorizes(tmp_path, respx_mock, monkeypatch):
    rwa = {"0x00000000000000000000000000000000000000a1", "0x00000000000000000000000000000000000000c1"}
    fake_facts(monkeypatch, rwa)
    lista = fx("gt_pools_list.json")
    respx_mock.get(GT_TOKEN_POOLS.format(USDG)).respond(json=lista)
    respx_mock.get(url__regex=r".*/tokens/0x(?!5fc5).*/pools").respond(json={"data": [], "included": []})
    respx_mock.get(GT_LIST).respond(json={"data": [], "included": []})
    c = scoped(tmp_path)
    res = await c.list_pools(sort_by="tvl", limit=10)
    only_rwa = await c.list_pools(sort_by="tvl", category="rwa")
    with pytest.raises(InvalidPoolId):
        await c.list_pools(category="meme")
    await c.aclose()
    names = [i.name for i in res.items]
    assert names == ["BIG / USDG", "AMAT / USDG"]          # b1 e d1 não são RWA → fora
    assert all(i.category == "rwa" and i.rwa_asset for i in res.items)
    assert [i.name for i in only_rwa.items] == names
