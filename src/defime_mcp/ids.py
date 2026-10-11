"""Parse de URL/slug/id → (chain, pool_id), com mapeamento de chain por fonte."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .exceptions import InvalidPoolId

# chain canônica → id em cada fonte
CHAINS: dict[str, dict[str, str]] = {
    "robinhood": {"dexscreener": "robinhood", "geckoterminal": "robinhood"},
    "base": {"dexscreener": "base", "geckoterminal": "base"},
    "ethereum": {"dexscreener": "ethereum", "geckoterminal": "eth"},
    "arbitrum": {"dexscreener": "arbitrum", "geckoterminal": "arbitrum"},
    "optimism": {"dexscreener": "optimism", "geckoterminal": "optimism"},
    "polygon": {"dexscreener": "polygon", "geckoterminal": "polygon_pos"},
    "bsc": {"dexscreener": "bsc", "geckoterminal": "bsc"},
}
ALIASES = {"eth": "ethereum", "mainnet": "ethereum", "polygon_pos": "polygon", "arb": "arbitrum", "op": "optimism"}

_HEX = re.compile(r"^0x[0-9a-fA-F]{40}$|^0x[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class PoolRef:
    chain: str
    pool_id: str  # 0x minúsculo; 40 hex = endereço (v2/v3), 64 hex = PoolId (Uniswap v4)

    @property
    def slug(self) -> str:
        return f"{self.chain}_{self.pool_id}"

    @property
    def is_v4_pool_id(self) -> bool:
        return len(self.pool_id) == 66

    def chain_for(self, source: str) -> str:
        return CHAINS[self.chain][source]


def normalize_chain(chain: str) -> str:
    c = chain.strip().lower()
    c = ALIASES.get(c, c)
    if c not in CHAINS:
        raise InvalidPoolId(f"chain desconhecida: '{chain}'. Suportadas: {', '.join(sorted(CHAINS))}")
    return c


def _validate_hex(pool_id: str) -> str:
    pid = pool_id.strip()
    if not _HEX.match(pid):
        raise InvalidPoolId(
            f"pool_id inválido: '{pool_id}'. Esperado 0x + 40 hex (endereço) ou 64 hex (PoolId Uniswap v4)."
        )
    return pid.lower()


def _from_url(url: str) -> tuple[str | None, str]:
    parts = [p for p in urlparse(url).path.split("/") if p]
    host = urlparse(url).netloc.lower()
    if "dexscreener" in host and len(parts) >= 2:
        return parts[0], parts[1]
    if "geckoterminal" in host and len(parts) >= 3 and parts[1] == "pools":
        return parts[0], parts[2]
    raise InvalidPoolId(f"URL não reconhecida: {url} (aceito DexScreener e GeckoTerminal)")


def _split_slug(slug: str) -> tuple[str | None, str]:
    if "_0x" in slug:
        chain, _, rest = slug.rpartition("_0x")
        return chain, "0x" + rest
    return None, slug


def parse_pool_ref(
    pool: str | None = None,
    chain: str | None = None,
    pool_id: str | None = None,
    default_chain: str = "robinhood",
) -> PoolRef:
    """Aceita: URL completa, slug `chain_0x…`, `0x…` + chain, ou chain + pool_id separados."""
    found_chain: str | None = None
    raw_id: str | None = pool_id

    if pool:
        p = pool.strip()
        if p.startswith("http://") or p.startswith("https://"):
            found_chain, raw_id = _from_url(p)
        else:
            found_chain, raw_id = _split_slug(p)

    if not raw_id:
        raise InvalidPoolId("informe `pool` (URL/slug) ou `pool_id`.")

    final_chain = chain or found_chain or default_chain
    if chain and found_chain and normalize_chain(chain) != normalize_chain(found_chain):
        raise InvalidPoolId(f"chain conflitante: '{chain}' vs '{found_chain}' no slug/URL")

    # GeckoTerminal usa 'eth' e 'polygon_pos'
    return PoolRef(chain=normalize_chain(final_chain), pool_id=_validate_hex(raw_id))
