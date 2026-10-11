"""Grava as respostas REAIS do RPC para os testes offline. Uso: python scripts/record_onchain.py"""
import asyncio
import json
from pathlib import Path

from defime_mcp.config import Settings
from defime_mcp.http import HttpClient
from defime_mcp.ids import parse_pool_ref
from defime_mcp.sources.onchain import OnChain

POOLS = {
    "onchain_v4_te_eth.json": "robinhood_0x9666ba19430380456bff5ac9d35a7ee2a6c6bc895bcc7ab92d3b64c8ec5cb611",
    "onchain_v3_pltr_usdg.json": "robinhood_0x851680416a4f4e1c463d45171d61acddbc8554c0",
}


async def main() -> None:
    http = HttpClient(Settings())
    log: list = []
    orig = http.post_json

    async def rec(url, body, headers=None):
        res = await orig(url, body, headers)
        log.append({"request": body, "response": res})
        return res

    http.post_json = rec
    oc = OnChain(http, ["https://rpc-robinhood.blockmachine.io"])
    out = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    for name, pool in POOLS.items():
        log.clear()
        st = await oc.read(parse_pool_ref(pool))
        (out / name).write_text(json.dumps({"exchanges": log, "decoded": st.model_dump(mode="json")}, indent=1))
        print(name, st.block, st.lp_fee_pct)
    await http.aclose()


asyncio.run(main())


# Classificação de tokens: dois RWA reais, uma memecoin e um falso "USDC" (copia o símbolo).
TOKENS = {
    "te_rwa": "0xb1969f6604ca1ae7a2cd3f1827876e914594ca2d",
    "mu_rwa": "0xff080c8ce2e5feadaca0da81314ae59d232d4afd",
    "mog_meme": "0xfeff40b3881f17159d63cd8c48eb2f001ee90e48",
    "fake_usdc": "0x2abbf33c23b934b4bf694dbaef59684cf07d6784",
}


async def record_tokens() -> None:
    http = HttpClient(Settings())
    log: list = []
    orig = http.post_json

    async def rec(url, body, headers=None):
        res = await orig(url, body, headers)
        log.append({"request": body, "response": res})
        return res

    http.post_json = rec
    facts = await OnChain(http, ["https://rpc-robinhood.blockmachine.io"]).token_facts(list(TOKENS.values()))
    out = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "onchain_token_facts.json"
    out.write_text(json.dumps({"exchanges": log, "decoded": facts, "tokens": TOKENS}, indent=1))
    for k, a in TOKENS.items():
        print(k, facts[a])
    await http.aclose()


asyncio.run(record_tokens())
