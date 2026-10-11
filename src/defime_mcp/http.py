"""Única camada que fala HTTP. Timeout, retry com backoff+jitter, 429/Retry-After e rate limit por host."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings
from .exceptions import NotFoundHTTP, RateLimited, UpstreamError
from .ratelimit import HostLimiter

log = logging.getLogger("defime_mcp.http")

RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.limiter = HostLimiter(settings.rate_limit_rps)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.total_timeout, connect=settings.connect_timeout),
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _retry_after(self, resp: httpx.Response) -> float | None:
        raw = resp.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return min(float(raw), self.settings.max_retry_after)
        except ValueError:
            return None

    async def get_json(self, url: str, params: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None) -> Any:
        return await self._request("GET", url, params=params, headers=headers)

    async def post_json(self, url: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        """POST com corpo JSON (usado pelo JSON-RPC da chain). Mesma política de retry do GET."""
        return await self._request("POST", url, json_body=body, headers=headers)

    async def _request(self, method: str, url: str, params: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None, json_body: Any = None) -> Any:
        host = urlparse(url).netloc
        attempts = self.settings.max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            await self.limiter.for_host(host).acquire()
            try:
                resp = await self._client.request(method, url, params=params, headers=headers,
                                                  json=json_body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                log.warning("%s %s falhou (%s), tentativa %d/%d", method, url, type(exc).__name__, attempt, attempts)
                if attempt < attempts:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise UpstreamError(f"{host}: sem resposta ({type(exc).__name__})") from exc

            if resp.status_code == 404:
                raise NotFoundHTTP(f"{host}: 404")

            if resp.status_code in RETRY_STATUS:
                wait = self._retry_after(resp) if resp.status_code == 429 else None
                if resp.status_code == 429 and wait is None:
                    # Sem Retry-After (caso do GeckoTerminal): a janela dele é de 1 minuto, e esperar 1–4s
                    # só gasta a cota de novo (medido: 4 tentativas seguidas em 429). 5s, 10s, 20s.
                    wait = min(5.0 * 2 ** (attempt - 1), self.settings.max_retry_after)
                log.warning("%s %s → %d, tentativa %d/%d", method, url, resp.status_code, attempt, attempts)
                if attempt < attempts:
                    await asyncio.sleep(wait if wait is not None else self._backoff(attempt))
                    continue
                if resp.status_code == 429:
                    raise RateLimited(f"{host}: rate limit atingido", retry_after=self._retry_after(resp))
                raise UpstreamError(f"{host}: HTTP {resp.status_code}")

            if resp.status_code >= 400:
                raise UpstreamError(f"{host}: HTTP {resp.status_code}")

            try:
                return resp.json()
            except ValueError as exc:
                raise UpstreamError(f"{host}: resposta não é JSON") from exc

        raise UpstreamError(f"{host}: falhou após {attempts} tentativas") from last_exc

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2 ** (attempt - 1), 8) + random.uniform(0, 0.5)
