"""Configuração via variáveis de ambiente (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # dependência opcional
        return
    load_dotenv()


def _token_map(name: str) -> dict[str, str] | None:
    """`0xabc…:WETH,0xdef…:cbBTC` → {endereço: símbolo}. Vazio = usar o padrão."""
    raw = _env(name, "")
    if not raw:
        return None
    out = {}
    for item in raw.split(","):
        addr, _, sym = item.strip().partition(":")
        if addr:
            out[addr.strip().lower()] = sym.strip() or addr.strip()
    return out


@dataclass(frozen=True)
class Settings:
    # Cache (segundos)
    cache_path: Path = Path("cache/defime.db")
    ttl_snapshot: int = 60
    ttl_history: int = 600
    ttl_list: int = 600
    ttl_search: int = 300
    ttl_metadata: int = 86400

    # HTTP
    rate_limit_rps: float = 1.0
    connect_timeout: float = 10.0
    total_timeout: float = 20.0
    max_retries: int = 3
    max_retry_after: float = 30.0
    user_agent: str = "mcp-defime/0.1 (+https://github.com/leandrosmoreira53/mcp-defime)"

    # Fontes
    default_chain: str = "robinhood"
    # Escopo: "rwa_bluechip" = só Robinhood Chain, só pools de tokens RWA da Robinhood e cripto blue
    # chip (ETH/WETH/cbBTC), com USDG como contraparte. "all" = qualquer pool das chains suportadas.
    scope: str = "rwa_bluechip"
    bluechip_tokens: dict[str, str] | None = None   # None = padrão de universe.py
    stable_tokens: dict[str, str] | None = None
    # Leitura on-chain (só eth_call). Primeiro RPC que responder vence.
    onchain_enabled: bool = True
    robinhood_rpc_urls: tuple[str, ...] = ("https://rpc-robinhood.blockmachine.io",
                                           "https://rpc.mainnet.chain.robinhood.com")


    # Servidor MCP
    transport: str = "stdio"  # stdio | http
    host: str = "127.0.0.1"
    port: int = 8000
    auth_token: str = ""
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    allow_no_auth: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        hosts = tuple(h.strip() for h in _env("MCP_ALLOWED_HOSTS", "").split(",") if h.strip())
        return cls(
            cache_path=Path(_env("CACHE_PATH", "cache/defime.db")),
            ttl_snapshot=int(_env("CACHE_TTL_SNAPSHOT", "60")),
            ttl_history=int(_env("CACHE_TTL_HISTORY", "600")),
            ttl_list=int(_env("CACHE_TTL_LIST", "600")),
            ttl_search=int(_env("CACHE_TTL_SEARCH", "300")),
            ttl_metadata=int(_env("CACHE_TTL_METADATA", "86400")),
            scope=_env("SCOPE", "rwa_bluechip").lower(),
            bluechip_tokens=_token_map("BLUECHIP_TOKENS"),
            stable_tokens=_token_map("STABLE_TOKENS"),
            rate_limit_rps=float(_env("RATE_LIMIT_RPS", "1")),
            connect_timeout=float(_env("HTTP_CONNECT_TIMEOUT", "10")),
            total_timeout=float(_env("HTTP_TOTAL_TIMEOUT", "20")),
            max_retries=int(_env("HTTP_MAX_RETRIES", "3")),
            user_agent=_env("HTTP_USER_AGENT", "mcp-defime/0.1 (+https://github.com/leandrosmoreira53/mcp-defime)"),
            default_chain=_env("DEFAULT_CHAIN", "robinhood").lower(),
            onchain_enabled=_bool("ONCHAIN_ENABLED", True),
            robinhood_rpc_urls=tuple(u.strip() for u in _env(
                "ROBINHOOD_RPC_URLS",
                "https://rpc-robinhood.blockmachine.io,https://rpc.mainnet.chain.robinhood.com",
            ).split(",") if u.strip()),
            transport=_env("MCP_TRANSPORT", "stdio").lower(),
            host=_env("MCP_HOST", "127.0.0.1"),
            port=int(_env("MCP_PORT", "8000")),
            auth_token=_env("MCP_AUTH_TOKEN", ""),
            allowed_hosts=hosts,
            allow_no_auth=_bool("MCP_ALLOW_NO_AUTH", False),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
        )
