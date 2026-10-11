"""Modelos normalizados entregues ao agente.

Convenções:
- Unidades no nome do campo (`_usd`, `_pct`). `fee_pct=4.88` significa 4,88%. `apr_24h_pct=12.5` = 12,5%.
- Valor que a fonte não forneceu = None e aparece em `missing_fields`.
- Valor calculado por nós aparece em `derived_fields` com a fórmula.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class CacheInfo(BaseModel):
    hit: bool = False
    age_seconds: int = 0
    stale: bool = False


class Token(BaseModel):
    symbol: str | None = None
    name: str | None = None
    address: str | None = None
    kind: str | None = None     # rwa | bluechip | stable (classificação por contrato, ver universe.py)


class Metrics(BaseModel):
    price_usd: float | None = None
    price_quote_per_base: float | None = None
    tvl_usd: float | None = None
    volume_5m_usd: float | None = None
    volume_1h_usd: float | None = None
    volume_6h_usd: float | None = None
    volume_24h_usd: float | None = None
    fees_24h_usd: float | None = None
    apr_24h_pct: float | None = None
    # Mesma conta com a taxa que CHEGA ao LP (lida on-chain). É a que importa para quem prove liquidez.
    fees_24h_lp_usd: float | None = None
    apr_24h_lp_pct: float | None = None
    price_change_24h_pct: float | None = None
    txns_24h_buys: int | None = None
    txns_24h_sells: int | None = None


class DerivedField(BaseModel):
    field: str
    formula: str


class CrossCheck(BaseModel):
    """Mesma métrica em fontes diferentes, para o agente julgar a confiabilidade."""

    tvl_usd: dict[str, float] = Field(default_factory=dict)
    volume_24h_usd: dict[str, float] = Field(default_factory=dict)
    tvl_divergence_pct: float | None = None
    volume_divergence_pct: float | None = None
    price_quote_per_base: dict[str, float] = Field(default_factory=dict)
    price_divergence_pct: float | None = None


class PoolSnapshot(BaseModel):
    kind: str = "current"
    sources: list[str]
    source_urls: dict[str, str] = Field(default_factory=dict)
    retrieved_at: datetime = Field(default_factory=utcnow)
    cache: CacheInfo = Field(default_factory=CacheInfo)
    chain: str
    pool_id: str
    name: str | None = None
    dex: str | None = None
    dex_version: str | None = None
    category: str | None = None          # rwa | bluechip (SCOPE=rwa_bluechip)
    fee_pct: float | None = None
    lp_fee_pct: float | None = None
    created_at: datetime | None = None
    base_token: Token = Field(default_factory=Token)
    quote_token: Token = Field(default_factory=Token)
    metrics: Metrics = Field(default_factory=Metrics)
    cross_check: CrossCheck = Field(default_factory=CrossCheck)
    onchain: dict[str, Any] | None = None
    derived_fields: list[DerivedField] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def compute_missing(self) -> "PoolSnapshot":
        missing = [f"metrics.{k}" for k, v in self.metrics.model_dump().items() if v is None]
        for k in ("name", "dex", "fee_pct", "lp_fee_pct", "created_at"):
            if getattr(self, k) is None:
                missing.append(k)
        for side in ("base_token", "quote_token"):
            for k, v in getattr(self, side).model_dump(exclude={"kind"}).items():
                if v is None:
                    missing.append(f"{side}.{k}")
        self.missing_fields = missing
        return self


class HistoryPoint(BaseModel):
    timestamp: int
    date: datetime
    volume_usd: float | None = None
    fees_usd: float | None = None
    apr_pct: float | None = None
    apr_lp_pct: float | None = None
    close_price_usd: float | None = None
    no_swaps: bool = False


class PoolHistory(BaseModel):
    kind: str = "historical"
    sources: list[str]
    retrieved_at: datetime = Field(default_factory=utcnow)
    cache: CacheInfo = Field(default_factory=CacheInfo)
    chain: str
    pool_id: str
    timeframe: str
    granularity: str
    fee_pct: float | None = None
    lp_fee_pct: float | None = None
    tvl_reference_usd: float | None = None
    points: list[HistoryPoint] = Field(default_factory=list)
    summary: dict[str, float | None] = Field(default_factory=dict)
    derived_fields: list[DerivedField] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PoolSummary(BaseModel):
    chain: str
    pool_id: str
    name: str | None = None
    category: str | None = None
    rwa_asset: str | None = None         # ex.: "Micron Technology – Robinhood Token"
    dex: str | None = None
    fee_pct: float | None = None
    tvl_usd: float | None = None
    volume_1h_usd: float | None = None
    volume_24h_usd: float | None = None
    apr_24h_pct: float | None = None
    created_at: datetime | None = None
    url: str | None = None


class PoolList(BaseModel):
    sources: list[str]
    retrieved_at: datetime = Field(default_factory=utcnow)
    cache: CacheInfo = Field(default_factory=CacheInfo)
    chain: str | None = None
    query: str | None = None
    count: int = 0
    items: list[PoolSummary] = Field(default_factory=list)
    derived_fields: list[DerivedField] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ComparisonRow(BaseModel):
    pool: str
    chain: str | None = None
    pool_id: str | None = None
    name: str | None = None
    category: str | None = None
    dex_version: str | None = None
    fee_pct: float | None = None
    lp_fee_pct: float | None = None
    tvl_usd: float | None = None
    volume_24h_usd: float | None = None
    turnover_24h: float | None = None       # volume_24h ÷ TVL
    fees_24h_lp_usd: float | None = None
    apr_24h_pct: float | None = None
    apr_24h_lp_pct: float | None = None
    price_usd: float | None = None
    price_change_24h_pct: float | None = None
    tvl_divergence_pct: float | None = None
    cache_hit: bool | None = None
    error: str | None = None


class PoolComparison(BaseModel):
    kind: str = "comparison"
    retrieved_at: datetime = Field(default_factory=utcnow)
    rows: list[ComparisonRow] = Field(default_factory=list)
    leaders: dict[str, str] = Field(default_factory=dict)
    derived_fields: list[DerivedField] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


APR_FORMULA ="volume_24h_usd × fee_pct/100 ÷ tvl_usd × 365 × 100 (fórmula usual de agregadores)"
FEES_FORMULA = "volume_24h_usd × fee_pct/100"
APR_LP_FORMULA = ("volume_24h_usd × lp_fee_pct/100 ÷ tvl_usd × 365 × 100 — lp_fee_pct lido on-chain "
                  "(V3: fee × (1 − 1/feeProtocol); V4: lpFee × (1 − protocolFee/1e6))")
FEES_LP_FORMULA = "volume_24h_usd × lp_fee_pct/100"


def apr_pct(volume_usd: float | None, fee_pct: float | None, tvl_usd: float | None,
            periods_per_year: float = 365) -> float | None:
    if volume_usd is None or fee_pct is None or not tvl_usd:
        return None
    return volume_usd * fee_pct / 100 / tvl_usd * periods_per_year * 100


def divergence_pct(values: dict[str, float]) -> float | None:
    vals = [v for v in values.values() if v]
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    return round((hi - lo) / lo * 100, 2)
