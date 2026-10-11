"""DexScreener — https://docs.dexscreener.com/api/reference (sem auth, 300 req/min em /pairs)."""

from __future__ import annotations

from typing import Any

from ..exceptions import NotFoundHTTP
from ..http import HttpClient
from ..ids import PoolRef
from ..models import Token
from .base import SourcePool, fnum, guard, inum, ts_ms

BASE = "https://api.dexscreener.com"
NAME = "dexscreener"


def parse_pair(pair: dict[str, Any], chain: str) -> SourcePool:
    def _p() -> SourcePool:
        labels = pair.get("labels") or []
        base, quote = pair["baseToken"], pair["quoteToken"]
        vol = pair.get("volume") or {}
        tx = (pair.get("txns") or {}).get("h24") or {}
        return SourcePool(
            source=NAME,
            chain=chain,
            pool_id=str(pair["pairAddress"]).lower(),
            url=pair.get("url"),
            name=f"{base.get('symbol')} / {quote.get('symbol')}",
            dex=pair.get("dexId"),
            dex_version=next((l for l in labels if l.lower().startswith("v")), None),
            created_at=ts_ms(pair.get("pairCreatedAt")),
            base_token=Token(symbol=base.get("symbol"), name=base.get("name"),
                             address=(base.get("address") or "").lower() or None),
            quote_token=Token(symbol=quote.get("symbol"), name=quote.get("name"),
                              address=(quote.get("address") or "").lower() or None),
            price_usd=fnum(pair.get("priceUsd")),
            price_quote_per_base=fnum(pair.get("priceNative")),
            tvl_usd=fnum((pair.get("liquidity") or {}).get("usd")),
            volume_5m_usd=fnum(vol.get("m5")),
            volume_1h_usd=fnum(vol.get("h1")),
            volume_6h_usd=fnum(vol.get("h6")),
            volume_24h_usd=fnum(vol.get("h24")),
            price_change_24h_pct=fnum((pair.get("priceChange") or {}).get("h24")),
            txns_24h_buys=inum(tx.get("buys")),
            txns_24h_sells=inum(tx.get("sells")),
        )
    return guard(NAME, _p)


class DexScreener:
    def __init__(self, http: HttpClient):
        self.http = http

    async def get_pool(self, ref: PoolRef) -> SourcePool:
        data = await self.http.get_json(f"{BASE}/latest/dex/pairs/{ref.chain_for(NAME)}/{ref.pool_id}")
        pairs = guard(NAME, lambda: data.get("pairs") if isinstance(data, dict) else None) or []
        match = [p for p in pairs if str(p.get("pairAddress", "")).lower() == ref.pool_id]
        if not match:
            raise NotFoundHTTP(f"{NAME}: pool não encontrado")
        return parse_pair(match[0], ref.chain)

    async def search(self, query: str, chain: str | None) -> list[SourcePool]:
        data = await self.http.get_json(f"{BASE}/latest/dex/search", params={"q": query})
        pairs = guard(NAME, lambda: data.get("pairs")) or []
        out = []
        for p in pairs:
            ds_chain = p.get("chainId")
            if chain and ds_chain != chain:
                continue
            out.append(parse_pair(p, ds_chain))
        return out
