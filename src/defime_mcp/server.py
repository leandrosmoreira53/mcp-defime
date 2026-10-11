"""Servidor MCP. Só traduz tools → PoolClient. Zero HTTP aqui.

Uso:
  python -m defime_mcp.server                      # stdio (Claude Code/Cursor local)
  MCP_TRANSPORT=http python -m defime_mcp.server   # HTTP (VPS), exige MCP_AUTH_TOKEN
"""

from __future__ import annotations

import hmac
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from .client import PoolClient
from .config import Settings
from .exceptions import (
    FeatureUnavailable,
    InvalidPoolId,
    OutOfScope,
    PoolNotFound,
    RateLimited,
    UpstreamError,
    UpstreamSchemaChanged,
    DefimeError,
)

log = logging.getLogger("defime_mcp.server")

INSTRUCTIONS = """\
Dados de pools de liquidez da ROBINHOOD CHAIN, restritos a tokens RWA da Robinhood (ações/ETFs tokenizados,
ex.: MU, PLTR, UPS, TSLA) e cripto blue chip (ETH, WETH, cbBTC), com USDG como contraparte. Memecoins e
outras chains ficam fora (erro "fora do escopo"). RWA é verificado pelo contrato (beacon da Robinhood), nunca
pelo símbolo — a chain tem tokens falsos chamados USDC/USDT/SOL.
Fontes: DexScreener + GeckoTerminal + leitura on-chain. Toda resposta traz `sources`, `retrieved_at`,
`cache`, `derived_fields` (o que foi calculado e como) e `missing_fields` (o que a fonte não forneceu).
Nunca preencha campos ausentes por conta própria. APR é anualizado a partir de 24h e é volátil em pools pequenos.
Na Robinhood Chain o estado é lido também do contrato (`onchain`): `lp_fee_pct` é a taxa que CHEGA ao LP
(V3 com feeProtocol entrega 5/6 da nominal; V4 desconta protocolFee). Para rendimento de LP use
`apr_24h_lp_pct`, não `apr_24h_pct`. Para comparar pools use defime_compare_pools."""

_client: PoolClient | None = None


def client() -> PoolClient:
    global _client
    if _client is None:
        _client = PoolClient()
    return _client


def _err(exc: Exception) -> ToolError:
    if isinstance(exc, RateLimited):
        wait = f" Tente em {int(exc.retry_after)}s." if exc.retry_after else " Tente em ~1 min."
        return ToolError(f"Rate limit da fonte atingido.{wait}")
    if isinstance(exc, UpstreamSchemaChanged):
        return ToolError(f"A fonte mudou o formato dos dados; o adapter precisa ser atualizado. ({exc})")
    if isinstance(exc, (InvalidPoolId, PoolNotFound, FeatureUnavailable, OutOfScope)):
        return ToolError(str(exc))
    if isinstance(exc, UpstreamError):
        return ToolError(f"Fonte indisponível: {exc}")
    if isinstance(exc, DefimeError):
        return ToolError(str(exc))
    log.exception("erro inesperado")
    return ToolError(f"Erro interno: {type(exc).__name__}")


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")


def build_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_app: FastMCP):
        # NÃO fechar o cliente aqui. Com stateless_http=True o FastMCP roda este lifespan a CADA
        # requisição: fechar o cliente no fim dela fazia a SEGUNDA chamada falhar com
        # "Cannot operate on a closed database" (medido). O cliente vive o processo inteiro.
        yield {}

    security = None
    if settings.transport == "http":
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", *settings.allowed_hosts]
        origins = [f"https://{h.split(':')[0]}" for h in settings.allowed_hosts] + ["http://127.0.0.1:*", "http://localhost:*"]
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                             allowed_hosts=hosts, allowed_origins=origins)

    mcp = FastMCP("defime", instructions=INSTRUCTIONS, host=settings.host, port=settings.port,
                  log_level=settings.log_level, lifespan=lifespan, transport_security=security,
                  stateless_http=True, json_response=True)

    @mcp.tool()
    async def defime_get_pool(pool: str | None = None, chain: str | None = None,
                           pool_id: str | None = None) -> dict[str, Any]:
        """Dados ATUAIS de um pool: preço, TVL, volume 5m/1h/6h/24h, fee, fees 24h, APR 24h, tokens,
        comparação entre fontes (cross_check de TVL, volume e preço). Na Robinhood Chain inclui o bloco
        `onchain` (lido do contrato no mesmo bloco): taxa nominal vs. taxa do LP, tick, liquidez ativa,
        preço pelo sqrtPrice, hook e taxa dinâmica (V4). `apr_24h_lp_pct` = APR com a taxa do LP.

        Aceita `pool` como URL (dexscreener.com, geckoterminal.com) ou slug `robinhood_0x…`,
        OU `chain` + `pool_id`. Chain padrão: robinhood.
        Ex.: pool="robinhood_0x9666ba19430380456bff5ac9d35a7ee2a6c6bc895bcc7ab92d3b64c8ec5cb611"
        """
        try:
            c = client()
            return _dump(await c.get_pool(c.ref(pool, chain, pool_id)))
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @mcp.tool()
    async def defime_get_pool_history(pool: str | None = None, chain: str | None = None,
                                   pool_id: str | None = None, timeframe: str = "30d",
                                   limit: int = 30) -> dict[str, Any]:
        """HISTÓRICO de um pool: volume, fees, APR e preço por período, + resumo (volume total, APR médio/máx).

        timeframe: "1d" (horário, 24 pts), "7d" (horário), "30d" (diário), "90d" (diário).
        limit: máximo de pontos devolvidos (mais recentes), padrão 30, máx 200.
        Períodos sem swap entram com volume 0 (`no_swaps=true`) para o APR médio não sair inflado.
        Identificação do pool igual a defime_get_pool.
        """
        try:
            c = client()
            return _dump(await c.get_pool_history(c.ref(pool, chain, pool_id), timeframe, limit))
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @mcp.tool()
    async def defime_compare_pools(pools: list[str], chain: str | None = None) -> dict[str, Any]:
        """Compara 2 a 5 pools lado a lado: TVL, volume 24h, giro (volume/TVL), taxa nominal e do LP,
        fees 24h do LP, APR 24h bruto e do LP, preço, divergência entre fontes, e o líder de cada métrica.
        Um pool que falhar aparece com `error` sem derrubar os outros.
        Ex.: pools=["robinhood_0x851680416a4f4e1c463d45171d61acddbc8554c0",
                    "https://dexscreener.com/robinhood/0x9666…"]
        """
        try:
            return _dump(await client().compare_pools(pools, chain))
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @mcp.tool()
    async def defime_list_pools(chain: str = "robinhood", sort_by: str = "volume", limit: int = 20,
                             min_tvl_usd: float = 0, category: str | None = None) -> dict[str, Any]:
        """Lista pools da Robinhood Chain no escopo (RWA da Robinhood e cripto blue chip), ordenados por
        sort_by = "volume" | "tvl" | "apr". category = "rwa" (ações/ETFs tokenizados) ou "bluechip"
        (ETH/WETH/cbBTC com USDG). Filtre ruído com min_tvl_usd (ex.: 10000). limit máx 50.
        Cada item traz `category` e, se RWA, `rwa_asset` (ex.: "Micron Technology – Robinhood Token").
        Ex.: "quais pools RWA existem?" → category="rwa", sort_by="tvl".
        """
        try:
            return _dump(await client().list_pools(chain, sort_by, limit, min_tvl_usd, category))
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @mcp.tool()
    async def defime_search_pools(query: str, chain: str | None = "robinhood", limit: int = 10) -> dict[str, Any]:
        """Busca pools da Robinhood Chain por símbolo, nome ou endereço (ex.: "MU", "PLTR", "cbBTC",
        "0xb196…"). Só devolve pools no escopo (RWA da Robinhood e cripto blue chip); o resto é
        descartado e contado em `notes`. limit máx 50.
        """
        try:
            return _dump(await client().search_pools(query, chain, limit))
        except Exception as exc:  # noqa: BLE001
            raise _err(exc) from exc

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):  # type: ignore[no-untyped-def]
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True, "service": "mcp-defime"})

    return mcp


class BearerAuth:
    """Middleware ASGI: exige `Authorization: Bearer <MCP_AUTH_TOKEN>` (exceto /healthz)."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            return await self.app(scope, receive, send)
        auth = dict(scope.get("headers") or []).get(b"authorization", b"")
        supplied = auth[7:] if auth.lower().startswith(b"bearer ") else b""
        if not supplied or not hmac.compare_digest(supplied, self.token):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        return await self.app(scope, receive, send)


def main() -> None:
    settings = Settings.from_env()
    if "--http" in sys.argv:
        settings = Settings(**{**settings.__dict__, "transport": "http"})
    logging.basicConfig(level=settings.log_level, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mcp = build_server(settings)

    if settings.transport == "stdio":
        mcp.run("stdio")
        return

    if settings.transport != "http":
        sys.exit(f"MCP_TRANSPORT inválido: {settings.transport} (use stdio ou http)")

    if not settings.auth_token and not settings.allow_no_auth:
        sys.exit("MCP_AUTH_TOKEN é obrigatório em modo http. Gere um: python -c 'import secrets;print(secrets.token_urlsafe(32))'")
    if settings.auth_token and len(settings.auth_token) < 24:
        sys.exit("MCP_AUTH_TOKEN muito curto (mínimo 24 caracteres).")

    import uvicorn

    app = mcp.streamable_http_app()
    if settings.auth_token:
        app = BearerAuth(app, settings.auth_token)
    log.info("mcp-defime HTTP em %s:%d/mcp", settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower(),
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
