"""Token bucket assíncrono por host."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int = 1):
        self.rate = max(rate_per_sec, 0.01)
        self.capacity = max(burst, 1)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class HostLimiter:
    """Um bucket por host. Limites conhecidos das APIs públicas ficam abaixo do documentado."""

    KNOWN = {
        "api.geckoterminal.com": 0.33,  # doc: 30/min grátis → usamos 20/min (429 medido a 24/min)
        "api.dexscreener.com": 2.0,     # doc: 300/min em /pairs
        # RPCs da Robinhood Chain: o 429 é por frequência e por IP (medido ~3 req/s no primário).
        "rpc-robinhood.blockmachine.io": 2.0,
        "rpc.mainnet.chain.robinhood.com": 1.0,
    }

    def __init__(self, default_rps: float):
        self.default_rps = default_rps
        self._buckets: dict[str, TokenBucket] = {}

    def for_host(self, host: str) -> TokenBucket:
        if host not in self._buckets:
            rps = self.KNOWN.get(host, self.default_rps)
            self._buckets[host] = TokenBucket(rps)
        return self._buckets[host]
