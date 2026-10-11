"""Escopo do MCP: só Robinhood Chain, só pools de tokens RWA da Robinhood e cripto blue chip.

Classificação por ENDEREÇO/CONTRATO, nunca por símbolo — na Robinhood Chain existem ~20 tokens
chamados "USDC", vários "USDT" e "SOL" que não são os de verdade.

  rwa       token de ação/ETF da Robinhood. Prova: o slot de beacon EIP-1967 aponta para o beacon
            oficial (todos os "… – Robinhood Token" medidos compartilham o mesmo). Lido on-chain.
  bluechip  lista fechada de endereços (ETH nativo, WETH, cbBTC). Configurável em BLUECHIP_TOKENS.
  stable    lista fechada (USDG). Só entra como contraparte: pool stable/stable fica fora.

Pool está no escopo se os DOIS tokens são rwa/bluechip/stable e ao menos um é rwa ou bluechip.
Categoria do pool: "rwa" se tiver um token RWA; senão "bluechip".
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .cache import Cache
from .exceptions import UpstreamError
from .sources.onchain import NATIVE, OnChain

# Beacon dos tokens de ação da Robinhood (conferido em 19/09 em USO, MU, UPS, TE: mesmo beacon).
ROBINHOOD_STOCK_BEACON = "0xe10b6f6b275de231345c20d14ab812db62151b00"

DEFAULT_BLUECHIP = {
    NATIVE: "ETH",
    "0x0bd7d308f8e1639fab988df18a8011f41eacad73": "WETH",
    "0xcec185eb182c47d1ba1efc84e6959e18cd620be4": "cbBTC",
}
DEFAULT_STABLE = {
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168": "USDG",
}

CATEGORIES = ("rwa", "bluechip")


class TokenClass(BaseModel):
    address: str
    kind: str | None            # "rwa" | "bluechip" | "stable" | None (fora do escopo)
    symbol: str | None = None
    name: str | None = None     # para RWA: "Micron Technology – Robinhood Token"
    evidence: str               # como foi decidido


@dataclass
class Scope:
    onchain: OnChain | None
    cache: Cache
    ttl: int
    bluechip: dict[str, str]
    stable: dict[str, str]
    beacon: str = ROBINHOOD_STOCK_BEACON

    async def classify(self, addresses: list[str | None]) -> dict[str, TokenClass]:
        out: dict[str, TokenClass] = {}
        unknown: list[str] = []
        for raw in addresses:
            if not raw:
                continue
            a = raw.lower()
            if a in self.bluechip:
                out[a] = TokenClass(address=a, kind="bluechip", symbol=self.bluechip[a], evidence="lista blue chip")
            elif a in self.stable:
                out[a] = TokenClass(address=a, kind="stable", symbol=self.stable[a], evidence="lista stable")
            else:
                hit = await self.cache.get(f"tokclass:{a}", self.ttl)
                if hit and hit.fresh:
                    out[a] = TokenClass.model_validate(hit.value)
                else:
                    unknown.append(a)
        if unknown:
            if self.onchain is None:
                raise UpstreamError("não dá para verificar se o token é RWA da Robinhood: leitura on-chain "
                                    "desligada (ONCHAIN_ENABLED=false)")
            facts = await self.onchain.token_facts(unknown)
            for a in unknown:
                f = facts.get(a) or {}
                is_rwa = f.get("beacon") == self.beacon
                tc = TokenClass(address=a, kind="rwa" if is_rwa else None, symbol=f.get("symbol"),
                                name=f.get("name"),
                                evidence=(f"beacon {self.beacon} (token de ação da Robinhood)" if is_rwa
                                          else f"beacon {f.get('beacon') or 'nenhum'}: não é token da Robinhood"))
                out[a] = tc
                await self.cache.set(f"tokclass:{a}", tc.model_dump())
        return out

    @staticmethod
    def pool_category(a: TokenClass | None, b: TokenClass | None) -> str | None:
        kinds = {a.kind if a else None, b.kind if b else None}
        if None in kinds or kinds == {"stable"}:
            return None
        return "rwa" if "rwa" in kinds else "bluechip"


def out_of_scope_reason(a: TokenClass | None, b: TokenClass | None) -> str:
    bad = [t for t in (a, b) if t is None or t.kind is None]
    if not bad:
        return "par só de stablecoins"
    names = ", ".join((t.symbol or t.address) if t else "token desconhecido" for t in bad)
    return f"{names} não é token RWA da Robinhood nem cripto blue chip (ETH/WETH/cbBTC)"
