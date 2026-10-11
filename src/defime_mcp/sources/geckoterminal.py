"""GeckoTerminal — https://www.geckoterminal.com/dex-api (sem auth, ~30 req/min no plano grátis)."""

from __future__ import annotations

import re
from typing import Any

from ..http import HttpClient
from ..ids import CHAINS, PoolRef
from ..models import Token
from .base import SourcePool, fnum, guard, inum, iso

BASE = "https://api.geckoterminal.com/api/v2"
NAME = "geckoterminal"
HEADERS = {"Accept": "application/json;version=20230302"}
_FEE_IN_NAME = re.compile(r"(\d+(?:\.\d+)?)%\s*$")

# timeframe da tool → (endpoint ohlcv, aggregate, limit, períodos por ano)
TIMEFRAMES = {
    "1d": ("hour", 1, 24, 24 * 365),
    "7d": ("hour", 1, 168, 24 * 365),
    "30d": ("day", 1, 30, 365),
    "90d": ("day", 1, 90, 365),
}


def _canonical_chain(gt_network: str) -> str:
    for canon, ids in CHAINS.items():
        if ids["geckoterminal"] == gt_network:
            return canon
    return gt_network


def _tokens(included: list[dict[str, Any]] | None) -> dict[str, Token]:
    out: dict[str, Token] = {}
    for item in included or []:
        if item.get("type") == "token":
            a = item.get("attributes") or {}
            out[item["id"]] = Token(symbol=a.get("symbol"), name=a.get("name"),
                                    address=(a.get("address") or "").lower() or None)
    return out


def parse_pool(item: dict[str, Any], included: list[dict[str, Any]] | None = None) -> SourcePool:
    def _p() -> SourcePool:
        a = item["attributes"]
        rel = item.get("relationships") or {}
        network, _, _ = item["id"].partition("_")
        toks = _tokens(included)
        base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id")
        quote_id = ((rel.get("quote_token") or {}).get("data") or {}).get("id")
        dex_id = ((rel.get("dex") or {}).get("data") or {}).get("id")
        vol = a.get("volume_usd") or {}
        tx = (a.get("transactions") or {}).get("h24") or {}

        fee = fnum(a.get("pool_fee_percentage"))
        fee_derived = False
        if fee is None:
            m = _FEE_IN_NAME.search(a.get("name") or "")
            if m:
                fee, fee_derived = float(m.group(1)), True

        base_tok = toks.get(base_id) or Token(address=base_id.partition("_")[2] if base_id else None)
        quote_tok = toks.get(quote_id) or Token(address=quote_id.partition("_")[2] if quote_id else None)
        pool_name = a.get("pool_name") or _FEE_IN_NAME.sub("", a.get("name") or "").strip() or None
        address = str(a["address"]).lower()
        return SourcePool(
            source=NAME,
            chain=_canonical_chain(network),
            pool_id=address,
            url=f"https://www.geckoterminal.com/{network}/pools/{address}",
            name=pool_name,
            dex=dex_id,
            fee_pct=fee,
            fee_pct_derived=fee_derived,
            created_at=iso(a.get("pool_created_at")),
            base_token=base_tok,
            quote_token=quote_tok,
            price_usd=fnum(a.get("base_token_price_usd")),
            price_quote_per_base=fnum(a.get("base_token_price_quote_token")),
            tvl_usd=fnum(a.get("reserve_in_usd")),
            volume_5m_usd=fnum(vol.get("m5")),
            volume_1h_usd=fnum(vol.get("h1")),
            volume_6h_usd=fnum(vol.get("h6")),
            volume_24h_usd=fnum(vol.get("h24")),
            price_change_24h_pct=fnum((a.get("price_change_percentage") or {}).get("h24")),
            txns_24h_buys=inum(tx.get("buys")),
            txns_24h_sells=inum(tx.get("sells")),
        )
    return guard(NAME, _p)


class GeckoTerminal:
    def __init__(self, http: HttpClient):
        self.http = http

    async def get_pool(self, ref: PoolRef) -> SourcePool:
        net = ref.chain_for(NAME)
        data = await self.http.get_json(
            f"{BASE}/networks/{net}/pools/{ref.pool_id}",
            params={"include": "base_token,quote_token,dex"}, headers=HEADERS,
        )
        return parse_pool(guard(NAME, lambda: data["data"]), data.get("included"))

    async def ohlcv(self, ref: PoolRef, timeframe: str) -> list[list[float]]:
        tf, agg, limit, _ = TIMEFRAMES[timeframe]
        net = ref.chain_for(NAME)
        data = await self.http.get_json(
            f"{BASE}/networks/{net}/pools/{ref.pool_id}/ohlcv/{tf}",
            params={"aggregate": agg, "limit": limit, "currency": "usd"}, headers=HEADERS,
        )
        return guard(NAME, lambda: data["data"]["attributes"]["ohlcv_list"])

    async def list_pools(self, chain: str, page: int = 1) -> list[SourcePool]:
        net = CHAINS[chain][NAME]
        data = await self.http.get_json(
            f"{BASE}/networks/{net}/pools",
            params={"page": page, "sort": "h24_volume_usd_desc", "include": "base_token,quote_token,dex"},
            headers=HEADERS,
        )
        items = guard(NAME, lambda: data["data"])
        return [parse_pool(i, data.get("included")) for i in items]

    async def token_pools(self, chain: str, token: str, page: int = 1) -> list[SourcePool]:
        """Pools que contêm `token`, por volume+liquidez. É assim que o universo RWA/USDG é montado."""
        net = CHAINS[chain][NAME]
        data = await self.http.get_json(
            f"{BASE}/networks/{net}/tokens/{token}/pools",
            params={"page": page, "include": "base_token,quote_token,dex"}, headers=HEADERS,
        )
        items = guard(NAME, lambda: data["data"])
        return [parse_pool(i, data.get("included")) for i in items]

    async def search(self, query: str, chain: str | None) -> list[SourcePool]:
        params: dict[str, Any] = {"query": query, "include": "base_token,quote_token,dex"}
        if chain:
            params["network"] = CHAINS[chain][NAME]
        data = await self.http.get_json(f"{BASE}/search/pools", params=params, headers=HEADERS)
        items = guard(NAME, lambda: data["data"])
        return [parse_pool(i, data.get("included")) for i in items]
