from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from ..exceptions import UpstreamSchemaChanged
from ..models import Token

log = logging.getLogger("defime_mcp.sources")
T = TypeVar("T")


class SourcePool(BaseModel):
    source: str
    chain: str
    pool_id: str
    url: str | None = None
    name: str | None = None
    dex: str | None = None
    dex_version: str | None = None
    fee_pct: float | None = None
    fee_pct_derived: bool = False
    created_at: datetime | None = None
    base_token: Token = Token()
    quote_token: Token = Token()
    price_usd: float | None = None
    price_quote_per_base: float | None = None
    tvl_usd: float | None = None
    volume_5m_usd: float | None = None
    volume_1h_usd: float | None = None
    volume_6h_usd: float | None = None
    volume_24h_usd: float | None = None
    price_change_24h_pct: float | None = None
    txns_24h_buys: int | None = None
    txns_24h_sells: int | None = None
    extra: dict[str, Any] = {}


def fnum(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def inum(v: Any) -> int | None:
    f = fnum(v)
    return int(f) if f is not None else None


def ts_ms(v: Any) -> datetime | None:
    f = fnum(v)
    return datetime.fromtimestamp(f / 1000, tz=timezone.utc) if f else None


def iso(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def guard(source: str, fn: Callable[[], T]) -> T:
    """Converte KeyError/TypeError/ValidationError de parsing em UpstreamSchemaChanged."""
    try:
        return fn()
    except (KeyError, TypeError, IndexError, ValidationError) as exc:
        log.error("schema inesperado em %s: %r", source, exc)
        raise UpstreamSchemaChanged(f"{source}: formato da resposta mudou ({type(exc).__name__}: {exc})") from exc
