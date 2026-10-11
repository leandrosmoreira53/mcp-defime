"""PoolClient — orquestra fontes, cache e normalização. Independente do MCP (reusável em scripts, API, notebooks)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .cache import Cache
from .config import Settings
from .exceptions import (
    FeatureUnavailable,
    InvalidPoolId,
    NotFoundHTTP,
    OutOfScope,
    PoolNotFound,
    UpstreamError,
    DefimeError,
)
from .http import HttpClient
from .ids import PoolRef, normalize_chain, parse_pool_ref
from .models import (
    APR_FORMULA,
    APR_LP_FORMULA,
    FEES_FORMULA,
    FEES_LP_FORMULA,
    CacheInfo,
    CrossCheck,
    DerivedField,
    HistoryPoint,
    Metrics,
    ComparisonRow,
    PoolComparison,
    PoolHistory,
    PoolList,
    PoolSnapshot,
    PoolSummary,
    apr_pct,
    divergence_pct,
)
from .sources.base import SourcePool
from .sources.dexscreener import DexScreener
from .sources.geckoterminal import TIMEFRAMES, GeckoTerminal
from .sources.onchain import OnChain, OnchainState
from .universe import CATEGORIES, DEFAULT_BLUECHIP, DEFAULT_STABLE, Scope, out_of_scope_reason

log = logging.getLogger("defime_mcp.client")

SORT_KEYS = {"tvl": "tvl_usd", "volume": "volume_24h_usd", "apr": "apr_24h_pct"}


def _first(*vals: Any) -> Any:
    return next((v for v in vals if v is not None), None)


class PoolClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 cache: Cache | None = None):
        self.settings = settings or Settings.from_env()
        self.http = HttpClient(self.settings, transport=transport)
        self.cache = cache or Cache(self.settings.cache_path)
        self.dexscreener = DexScreener(self.http)
        self.gecko = GeckoTerminal(self.http)
        # Só a Robinhood Chain tem RPC e contratos conferidos aqui.
        self.onchain = (OnChain(self.http, list(self.settings.robinhood_rpc_urls))
                        if self.settings.onchain_enabled and self.settings.robinhood_rpc_urls else None)
        self.scope = (Scope(self.onchain, self.cache, self.settings.ttl_metadata,
                            self.settings.bluechip_tokens or DEFAULT_BLUECHIP,
                            self.settings.stable_tokens or DEFAULT_STABLE)
                      if self.settings.scope == "rwa_bluechip" else None)

    async def aclose(self) -> None:
        await self.http.aclose()
        self.cache.close()

    async def __aenter__(self) -> "PoolClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def ref(self, pool: str | None = None, chain: str | None = None, pool_id: str | None = None) -> PoolRef:
        ref = parse_pool_ref(pool, chain, pool_id, default_chain=self.settings.default_chain)
        self._check_chain(ref.chain)
        return ref

    def _check_chain(self, chain: str | None) -> None:
        if self.scope and chain and chain != "robinhood":
            raise OutOfScope(f"chain '{chain}' fora do escopo: este MCP cobre só a Robinhood Chain "
                             "(tokens RWA da Robinhood e cripto blue chip).")

    # ---------- cache helper ----------

    async def _cached(self, key: str, ttl: int, fetch, model):
        """Fresh cache → devolve. Senão busca. Se a busca falhar e houver cache velho → stale-if-error."""
        entry = await self.cache.get(key, ttl)
        if entry and entry.fresh:
            obj = model.model_validate(entry.value)
            obj.cache = CacheInfo(hit=True, age_seconds=int(entry.age))
            return obj
        try:
            obj = await fetch()
        except (UpstreamError, asyncio.TimeoutError) as exc:
            if entry:
                log.warning("fonte falhou (%s); devolvendo cache velho de %ds", exc, entry.age)
                obj = model.model_validate(entry.value)
                obj.cache = CacheInfo(hit=True, age_seconds=int(entry.age), stale=True)
                obj.notes.append(f"Fonte indisponível ({exc}); dado do cache com {int(entry.age)}s.")
                return obj
            raise
        obj.cache = CacheInfo(hit=False, age_seconds=0)
        await self.cache.set(key, obj.model_dump(mode="json"))
        return obj

    # ---------- get_pool ----------

    async def get_pool(self, ref: PoolRef) -> PoolSnapshot:
        self._check_chain(ref.chain)
        snap = await self._cached(f"pool:{ref.slug}", self.settings.ttl_snapshot,
                                  lambda: self._fetch_pool(ref), PoolSnapshot)
        if self.scope and snap.category is None:
            # O snapshot fica no cache mesmo fora do escopo: repetir a pergunta não custa outra ida às fontes.
            motivo = next((n for n in snap.notes if "não é token RWA" in n or "só de stablecoins" in n), "")
            raise OutOfScope(f"pool {snap.name or ref.pool_id} fora do escopo: {motivo}")
        return snap

    async def _fetch_pool(self, ref: PoolRef) -> PoolSnapshot:
        use_chain = self.onchain is not None and ref.chain == "robinhood"
        tasks = [self.dexscreener.get_pool(ref), self.gecko.get_pool(ref),
                 self.onchain.read(ref) if use_chain else _none()]
        ds, gt, oc = await asyncio.gather(*tasks, return_exceptions=True)

        found: dict[str, SourcePool] = {}
        notes: list[str] = []
        errors: list[Exception] = []
        for name, r in (("dexscreener", ds), ("geckoterminal", gt)):
            if isinstance(r, SourcePool):
                found[name] = r
            elif isinstance(r, NotFoundHTTP):
                notes.append(f"{name}: pool não encontrado")
            elif isinstance(r, Exception):
                errors.append(r)
                notes.append(f"{name}: indisponível ({r})")

        chain_state: OnchainState | None = oc if isinstance(oc, OnchainState) else None
        if use_chain and chain_state is None:
            motivo = "pool não encontrado no contrato" if isinstance(oc, NotFoundHTTP) else f"indisponível ({oc})"
            notes.append(f"onchain: {motivo}")

        if not found and chain_state is None:
            if errors:
                raise UpstreamError("; ".join(str(e) for e in errors))
            raise PoolNotFound(f"pool {ref.pool_id} não encontrado na chain {ref.chain}")
        if not found:
            notes.append("Nenhum agregador indexa este pool: só há o estado on-chain (sem TVL/volume em USD).")

        d, g = found.get("dexscreener"), found.get("geckoterminal")
        derived: list[DerivedField] = []

        fee = g.fee_pct if g else None
        if g and g.fee_pct_derived:
            derived.append(DerivedField(field="fee_pct", formula="extraído do nome do pool no GeckoTerminal"))
        if fee is None and chain_state and chain_state.swap_fee_total_pct is not None:
            fee = chain_state.swap_fee_total_pct
            derived.append(DerivedField(field="fee_pct", formula="lido on-chain (taxa total cobrada do swap)"))
        lp_fee = chain_state.lp_fee_pct if chain_state else None

        # Primário: DexScreener; fallback: GeckoTerminal
        tvl = _first(d and d.tvl_usd, g and g.tvl_usd)
        vol24 = _first(d and d.volume_24h_usd, g and g.volume_24h_usd)
        metrics = Metrics(
            price_usd=_first(d and d.price_usd, g and g.price_usd),
            price_quote_per_base=_first(d and d.price_quote_per_base, g and g.price_quote_per_base),
            tvl_usd=tvl,
            volume_5m_usd=_first(d and d.volume_5m_usd, g and g.volume_5m_usd),
            volume_1h_usd=_first(d and d.volume_1h_usd, g and g.volume_1h_usd),
            volume_6h_usd=_first(d and d.volume_6h_usd, g and g.volume_6h_usd),
            volume_24h_usd=vol24,
            price_change_24h_pct=_first(d and d.price_change_24h_pct, g and g.price_change_24h_pct),
            txns_24h_buys=_first(d and d.txns_24h_buys, g and g.txns_24h_buys),
            txns_24h_sells=_first(d and d.txns_24h_sells, g and g.txns_24h_sells),
        )
        if vol24 is not None and fee is not None:
            metrics.fees_24h_usd = vol24 * fee / 100
            derived.append(DerivedField(field="metrics.fees_24h_usd", formula=FEES_FORMULA))
        metrics.apr_24h_pct = apr_pct(vol24, fee, tvl)
        if metrics.apr_24h_pct is not None:
            derived.append(DerivedField(field="metrics.apr_24h_pct", formula=APR_FORMULA))
        if vol24 is not None and lp_fee is not None:
            metrics.fees_24h_lp_usd = vol24 * lp_fee / 100
            derived.append(DerivedField(field="metrics.fees_24h_lp_usd", formula=FEES_LP_FORMULA))
        metrics.apr_24h_lp_pct = apr_pct(vol24, lp_fee, tvl)
        if metrics.apr_24h_lp_pct is not None:
            derived.append(DerivedField(field="metrics.apr_24h_lp_pct", formula=APR_LP_FORMULA))
        if fee is not None and lp_fee is not None and lp_fee < fee * 0.99:
            notes.append(f"A taxa nominal é {fee:.4g}% mas o LP recebe {lp_fee:.4g}% ({lp_fee / fee:.1%}); "
                         "use apr_24h_lp_pct para o rendimento de quem prove liquidez.")

        cc = CrossCheck(
            tvl_usd={k: v.tvl_usd for k, v in found.items() if v.tvl_usd is not None},
            volume_24h_usd={k: v.volume_24h_usd for k, v in found.items() if v.volume_24h_usd is not None},
        )
        cc.tvl_divergence_pct = divergence_pct(cc.tvl_usd)
        cc.volume_divergence_pct = divergence_pct(cc.volume_24h_usd)
        base_tok = _merge_token(d and d.base_token, g and g.base_token)
        quote_tok = _merge_token(d and d.quote_token, g and g.quote_token)
        for k, v in found.items():
            if v.price_quote_per_base is not None:
                cc.price_quote_per_base[k] = v.price_quote_per_base
        if chain_state:
            p_chain = _chain_price_quote_per_base(chain_state, base_tok.address, quote_tok.address)
            if p_chain is not None:
                cc.price_quote_per_base["onchain"] = p_chain
        cc.price_divergence_pct = divergence_pct(cc.price_quote_per_base)

        if not base_tok.address and chain_state:  # sem agregador: tokens do contrato
            base_tok = _merge_token(base_tok, _tok(chain_state.token0))
            quote_tok = _merge_token(quote_tok, _tok(chain_state.token1))
        category = None
        if self.scope:
            classes = await self.scope.classify([base_tok.address, quote_tok.address])
            cb = classes.get((base_tok.address or "").lower())
            cq = classes.get((quote_tok.address or "").lower())
            base_tok.kind, quote_tok.kind = cb and cb.kind, cq and cq.kind
            category = Scope.pool_category(cb, cq)
            if category is None:
                notes.append(out_of_scope_reason(cb, cq))
            for tok, cls in ((base_tok, cb), (quote_tok, cq)):
                if cls and cls.kind == "rwa" and cls.name:
                    tok.name = cls.name

        sources = list(found) + (["onchain"] if chain_state else [])
        urls = {k: v.url for k, v in found.items() if v.url}

        if metrics.apr_24h_pct is not None and tvl is not None and tvl < 10_000:
            notes.append("TVL abaixo de US$10k: APR muito sensível a poucos swaps.")

        snap = PoolSnapshot(
            sources=sources,
            source_urls=urls,
            chain=ref.chain,
            pool_id=ref.pool_id,
            name=_first(d and d.name, g and g.name, _chain_name(chain_state)),
            dex=_first(g and g.dex, d and d.dex, chain_state and chain_state.protocol),
            dex_version=_first(chain_state and chain_state.protocol.rsplit("_", 1)[-1],
                               d and d.dex_version, "v4" if ref.is_v4_pool_id else None),
            category=category,
            fee_pct=fee,
            lp_fee_pct=lp_fee,
            created_at=_first(d and d.created_at, g and g.created_at),
            base_token=base_tok,
            quote_token=quote_tok,
            metrics=metrics,
            cross_check=cc,
            onchain=chain_state.model_dump(mode="json", exclude={"notes"}) if chain_state else None,
            derived_fields=derived,
            notes=notes + (chain_state.notes if chain_state else []),
        )
        return snap.compute_missing()

    # ---------- history ----------

    async def get_pool_history(self, ref: PoolRef, timeframe: str = "30d", limit: int = 30) -> PoolHistory:
        if timeframe not in TIMEFRAMES:
            raise InvalidPoolId(f"timeframe inválido: {timeframe}. Use: {', '.join(TIMEFRAMES)}")
        limit = max(1, min(limit, 200))
        hist = await self._cached(f"hist:{ref.slug}:{timeframe}", self.settings.ttl_history,
                                  lambda: self._fetch_history(ref, timeframe), PoolHistory)
        if len(hist.points) > limit:
            hist.notes.append(f"Mostrando os últimos {limit} de {len(hist.points)} pontos.")
            hist.points = hist.points[-limit:]
        return hist

    async def _fetch_history(self, ref: PoolRef, timeframe: str) -> PoolHistory:
        snap, rows = await asyncio.gather(self.get_pool(ref), self.gecko.ohlcv(ref, timeframe))
        tf, _, n_periods, per_year = TIMEFRAMES[timeframe]
        step = 3600 if tf == "hour" else 86400
        fee, lp_fee, tvl = snap.fee_pct, snap.lp_fee_pct, snap.metrics.tvl_usd

        def point(ts: int, vol: float | None, close: float | None, no_swaps: bool = False) -> HistoryPoint:
            return HistoryPoint(
                timestamp=ts,
                date=datetime.fromtimestamp(ts, tz=timezone.utc),
                volume_usd=vol,
                fees_usd=vol * fee / 100 if vol is not None and fee is not None else None,
                apr_pct=apr_pct(vol, fee, tvl, per_year),
                apr_lp_pct=apr_pct(vol, lp_fee, tvl, per_year),
                close_price_usd=close,
                no_swaps=no_swaps,
            )

        by_ts: dict[int, HistoryPoint] = {}
        for row in rows:
            ts, _o, _h, _l, close, vol = (list(row) + [None] * 6)[:6]
            by_ts[int(ts)] = point(int(ts), float(vol) if vol is not None else None,
                                   float(close) if close is not None else None)

        # O GeckoTerminal OMITE o candle de período sem swap. Se a média for tirada só dos candles
        # presentes, um pool com swaps em 10 de 30 dias publica APR médio 3x maior. Lacuna ENTRE
        # candles (e antes do 1º, a partir da criação do pool) entra como volume 0, marcada.
        # Lacuna DEPOIS do último candle não é preenchida: pode ser só atraso do indexador.
        notes: list[str] = []
        filled = 0
        if by_ts:
            last = max(by_ts)
            first = last - (n_periods - 1) * step
            if snap.created_at:
                born = int(snap.created_at.timestamp()) // step * step
                first = max(first, born)
            for ts in range(first, last, step):
                if ts not in by_ts:
                    by_ts[ts] = point(ts, 0.0, None, no_swaps=True)
                    filled += 1
            lag = (int(datetime.now(timezone.utc).timestamp()) // step * step - last) // step
            if lag > 0:
                notes.append(f"Último candle publicado há {lag} período(s); esses períodos ficam FORA da "
                             "série (pode ser atraso do GeckoTerminal, não necessariamente zero swap).")
        points = [by_ts[t] for t in sorted(by_ts)]
        if filled:
            notes.append(f"{filled} período(s) sem candle entraram com volume 0 (no_swaps=true): o "
                         "GeckoTerminal omite períodos sem swap, e ignorá-los inflaria o APR médio.")

        vols = [p.volume_usd for p in points if p.volume_usd is not None]
        aprs = [p.apr_pct for p in points if p.apr_pct is not None]
        aprs_lp = [p.apr_lp_pct for p in points if p.apr_lp_pct is not None]
        summary = {
            "total_volume_usd": sum(vols) if vols else None,
            "total_fees_usd": sum(p.fees_usd for p in points if p.fees_usd is not None) if fee is not None else None,
            "total_fees_lp_usd": sum(vols) * lp_fee / 100 if vols and lp_fee is not None else None,
            "avg_apr_pct": sum(aprs) / len(aprs) if aprs else None,
            "avg_apr_lp_pct": sum(aprs_lp) / len(aprs_lp) if aprs_lp else None,
            "max_apr_pct": max(aprs) if aprs else None,
            "periods_with_volume": float(len([v for v in vols if v > 0])),
            "periods_total": float(len(points)),
        }
        if timeframe in ("7d", "30d", "90d") and aprs:
            notes.append("APR por período usa o TVL ATUAL como proxy (aproximação usual de agregadores): "
                         "se o TVL mudou no período, o APR histórico está distorcido na mesma proporção.")
        return PoolHistory(
            sources=["geckoterminal"] + [s for s in snap.sources if s != "geckoterminal"][:1],
            chain=ref.chain,
            pool_id=ref.pool_id,
            timeframe=timeframe,
            granularity=tf,
            fee_pct=fee,
            lp_fee_pct=lp_fee,
            tvl_reference_usd=tvl,
            points=points,
            summary=summary,
            derived_fields=[
                DerivedField(field="points[].fees_usd", formula="volume_usd × fee_pct/100"),
                DerivedField(field="points[].apr_pct",
                             formula=f"fees_usd ÷ tvl_reference_usd × {per_year} períodos/ano × 100"),
                DerivedField(field="points[].apr_lp_pct",
                             formula=f"volume_usd × lp_fee_pct/100 ÷ tvl_reference_usd × {per_year} × 100"),
            ],
            notes=notes,
        )

    # ---------- compare ----------

    async def compare_pools(self, pools: list[str], chain: str | None = None) -> PoolComparison:
        """Lado a lado de 2–5 pools. Um pool que falha vira linha com `error`, não derruba os outros."""
        uniq = list(dict.fromkeys(p.strip() for p in pools if p and p.strip()))
        if not 2 <= len(uniq) <= 5:
            raise InvalidPoolId("informe de 2 a 5 pools distintos para comparar")
        rows: list[ComparisonRow] = []
        for raw in uniq:  # sequencial: respeita o rate limit do GeckoTerminal
            try:
                snap = await self.get_pool(self.ref(raw, chain=chain))
            except DefimeError as exc:
                rows.append(ComparisonRow(pool=raw, error=str(exc)))
                continue
            m = snap.metrics
            rows.append(ComparisonRow(
                pool=raw, chain=snap.chain, pool_id=snap.pool_id, name=snap.name, category=snap.category,
                dex_version=snap.dex_version, fee_pct=snap.fee_pct, lp_fee_pct=snap.lp_fee_pct,
                tvl_usd=m.tvl_usd, volume_24h_usd=m.volume_24h_usd,
                turnover_24h=m.volume_24h_usd / m.tvl_usd if m.volume_24h_usd is not None and m.tvl_usd else None,
                fees_24h_lp_usd=m.fees_24h_lp_usd, apr_24h_pct=m.apr_24h_pct, apr_24h_lp_pct=m.apr_24h_lp_pct,
                price_usd=m.price_usd, price_change_24h_pct=m.price_change_24h_pct,
                tvl_divergence_pct=snap.cross_check.tvl_divergence_pct, cache_hit=snap.cache.hit,
            ))
        ok = [r for r in rows if r.error is None]
        leaders: dict[str, str] = {}
        for field in ("tvl_usd", "volume_24h_usd", "turnover_24h", "apr_24h_lp_pct", "apr_24h_pct"):
            cand = [r for r in ok if getattr(r, field) is not None]
            if cand:
                best = max(cand, key=lambda r: getattr(r, field))
                leaders[field] = best.name or best.pool
        notes = ["APR de 24h é volume de UM dia anualizado: um dia atípico decide a ordem. "
                 "Confirme com defime_get_pool_history antes de concluir qual pool paga mais.",
                 "Mudar de pool tem custo (taxa das duas pools + impacto de preço + taxa de interface "
                 "do frontend, se houver); a diferença de APR precisa pagar isso no horizonte pretendido."]
        if any(r.tvl_divergence_pct and r.tvl_divergence_pct > 25 for r in ok):
            notes.append("Algum pool tem TVL divergindo >25% entre fontes: o APR dele é incerto.")
        return PoolComparison(
            rows=rows, leaders=leaders, notes=notes,
            derived_fields=[DerivedField(field="rows[].turnover_24h", formula="volume_24h_usd ÷ tvl_usd"),
                            DerivedField(field="rows[].apr_24h_lp_pct", formula=APR_LP_FORMULA)],
        )

    # ---------- list / search ----------

    async def list_pools(self, chain: str | None = None, sort_by: str = "volume", limit: int = 20,
                         min_tvl_usd: float = 0, category: str | None = None) -> PoolList:
        chain = normalize_chain(chain or self.settings.default_chain)
        self._check_chain(chain)
        if sort_by not in SORT_KEYS:
            raise InvalidPoolId(f"sort_by inválido: {sort_by}. Use: {', '.join(SORT_KEYS)}")
        if category:
            category = category.strip().lower()
            if not self.scope:
                raise FeatureUnavailable("Filtro por categoria exige SCOPE=rwa_bluechip.")
            if category not in CATEGORIES:
                raise InvalidPoolId(f"category inválida: {category}. Use: {', '.join(CATEGORIES)}")
        limit = max(1, min(limit, 50))
        key = f"list:{chain}:{'scoped' if self.scope else 'all'}"
        base = await self._cached(key, self.settings.ttl_list, lambda: self._fetch_list(chain), PoolList)
        items = [i for i in base.items if (i.tvl_usd or 0) >= min_tvl_usd
                 and (not category or i.category == category)]
        k = SORT_KEYS[sort_by]
        items.sort(key=lambda i: getattr(i, k) or 0, reverse=True)
        base.items = items[:limit]
        base.count = len(base.items)
        return base

    async def universe(self, chain: str | None = None) -> PoolList:
        """O universo inteiro da listagem (sem o corte de 50 da tool), com o mesmo cache."""
        chain = normalize_chain(chain or self.settings.default_chain)
        self._check_chain(chain)
        key = f"list:{chain}:{'scoped' if self.scope else 'all'}"
        return await self._cached(key, self.settings.ttl_list, lambda: self._fetch_list(chain), PoolList)

    async def _fetch_list(self, chain: str) -> PoolList:
        if self.scope:
            return await self._fetch_scoped_list(chain)
        pools: list[SourcePool] = []
        for page in (1, 2, 3):  # 60 pools mais negociados; limite de rate do GT
            batch = await self.gecko.list_pools(chain, page)
            pools.extend(batch)
            if len(batch) < 20:
                break
        return PoolList(
            sources=["geckoterminal"],
            chain=chain,
            items=[_summary(p) for p in pools],
            count=len(pools),
            derived_fields=[DerivedField(field="items[].apr_24h_pct", formula=APR_FORMULA)],
            notes=["Universo = até 60 pools com maior volume 24h no GeckoTerminal, reordenados localmente."],
        )

    # Universo do escopo: pools que contêm USDG (onde vivem os RWA) + os de cada blue chip + o topo
    # geral (pega RWA/ETH nativo, como TE/ETH). 6 chamadas ao GeckoTerminal por CACHE_TTL_LIST.
    SCOPED_PAGES = (("stable", 3), ("bluechip", 1), ("top", 1))  # 6 chamadas

    async def _fetch_scoped_list(self, chain: str) -> PoolList:
        assert self.scope is not None
        seen: dict[str, SourcePool] = {}
        notes: list[str] = []

        async def take(fetch, label: str, pages: int) -> None:
            for page in range(1, pages + 1):
                try:
                    batch = await fetch(page)
                except (UpstreamError, NotFoundHTTP) as exc:
                    notes.append(f"{label} página {page}: {exc} (universo incompleto)")
                    return
                for p in batch:
                    seen.setdefault(p.pool_id, p)
                if len(batch) < 20:
                    return

        for kind, pages in self.SCOPED_PAGES:
            if kind == "top":
                await take(lambda pg: self.gecko.list_pools(chain, pg), "topo geral", pages)
                continue
            addrs = self.scope.stable if kind == "stable" else self.scope.bluechip
            for addr, sym in addrs.items():
                if addr == "0x0000000000000000000000000000000000000000":
                    continue  # ETH nativo não tem página de token; aparece no topo geral
                await take(lambda pg, a=addr: self.gecko.token_pools(chain, a, pg), f"pools de {sym}", pages)

        if not seen:
            raise UpstreamError("geckoterminal: nenhuma página do universo respondeu; " + "; ".join(notes))
        classes = await self.scope.classify([t for p in seen.values()
                                             for t in (p.base_token.address, p.quote_token.address)])
        items = []
        for p in seen.values():
            s = _scoped_summary(p, classes)
            if s:
                items.append(s)
        notes.append(f"Universo = {len(seen)} pools lidos do GeckoTerminal (pools com USDG, WETH, cbBTC e o "
                     f"topo geral); {len(items)} passam no escopo RWA/blue chip. Pools muito pequenos ou sem "
                     "volume podem ficar de fora.")
        notes.append("apr_24h_pct aqui usa a taxa NOMINAL; a taxa que chega ao LP sai em defime_get_pool.")
        return PoolList(sources=["geckoterminal", "onchain"], chain=chain, items=items, count=len(items),
                        derived_fields=[DerivedField(field="items[].apr_24h_pct", formula=APR_FORMULA),
                                        DerivedField(field="items[].category",
                                                     formula="beacon on-chain (RWA) ou lista de endereços")],
                        notes=notes)

    async def search_pools(self, query: str, chain: str | None = None, limit: int = 10) -> PoolList:
        q = query.strip()
        if len(q) < 2:
            raise InvalidPoolId("query precisa de pelo menos 2 caracteres")
        chain_c = normalize_chain(chain) if chain else None
        if self.scope:
            self._check_chain(chain_c)
            chain_c = "robinhood"
        limit = max(1, min(limit, 50))
        res = await self._cached(f"search:{chain_c}:{q.lower()}", self.settings.ttl_search,
                                 lambda: self._fetch_search(q, chain_c), PoolList)
        res.items = res.items[:limit]
        res.count = len(res.items)
        return res

    async def _fetch_search(self, q: str, chain: str | None) -> PoolList:
        notes: list[str] = []
        try:
            pools = await self.gecko.search(q, chain)
            src = "geckoterminal"
        except UpstreamError as exc:
            notes.append(f"geckoterminal indisponível ({exc}); usando dexscreener")
            ds_chain = _ds_chain(chain)
            pools = await self.dexscreener.search(q, ds_chain)
            src = "dexscreener"
        if self.scope:
            classes = await self.scope.classify([t for p in pools
                                                 for t in (p.base_token.address, p.quote_token.address)])
            kept = [s for s in (_scoped_summary(p, classes) for p in pools) if s]
            if len(kept) < len(pools):
                notes.append(f"{len(pools) - len(kept)} resultado(s) fora do escopo RWA/blue chip removidos "
                             "(ex.: memecoins e tokens que só copiam o símbolo USDC/USDT/SOL).")
            items = sorted(kept, key=lambda i: i.tvl_usd or 0, reverse=True)
            src = f"{src}+onchain"
        else:
            items = sorted((_summary(p) for p in pools), key=lambda i: i.tvl_usd or 0, reverse=True)
        return PoolList(sources=[src], chain=chain, query=q, items=items, count=len(items),
                        derived_fields=[DerivedField(field="items[].apr_24h_pct", formula=APR_FORMULA)],
                        notes=notes)


async def _none() -> None:
    return None


def _chain_name(st: OnchainState | None) -> str | None:
    if st and st.token0.symbol and st.token1.symbol:
        return f"{st.token0.symbol} / {st.token1.symbol}"
    return None


def _chain_price_quote_per_base(st: OnchainState, base: str | None, quote: str | None) -> float | None:
    """Preço on-chain na MESMA orientação dos agregadores (quote por base), casando por endereço.

    Endereços que não casam (ex.: agregador mostra WETH onde a V4 tem ETH nativo) → None, nunca chute.
    """
    t0, t1 = st.token0.address, st.token1.address
    if not base or not quote or not t0 or not t1:
        return None
    if (base, quote) == (t0, t1):
        return st.price_token1_per_token0
    if (base, quote) == (t1, t0):
        return st.price_token0_per_token1
    return None


def _ds_chain(chain: str | None) -> str | None:
    from .ids import CHAINS
    return CHAINS[chain]["dexscreener"] if chain else None


def _tok(t):
    from .models import Token
    return Token(symbol=t.symbol, address=t.address)


def _scoped_summary(p: SourcePool, classes) -> PoolSummary | None:
    cb = classes.get((p.base_token.address or "").lower())
    cq = classes.get((p.quote_token.address or "").lower())
    cat = Scope.pool_category(cb, cq)
    if cat is None:
        return None
    s = _summary(p)
    s.category = cat
    s.rwa_asset = next((c.name for c in (cb, cq) if c and c.kind == "rwa"), None)
    return s


def _merge_token(a, b):
    from .models import Token
    a = a or Token()
    b = b or Token()
    return Token(symbol=_first(a.symbol, b.symbol), name=_first(a.name, b.name),
                 address=_first(a.address, b.address))


def _summary(p: SourcePool) -> PoolSummary:
    return PoolSummary(
        chain=p.chain,
        pool_id=p.pool_id,
        name=p.name,
        dex=p.dex,
        fee_pct=p.fee_pct,
        tvl_usd=p.tvl_usd,
        volume_1h_usd=p.volume_1h_usd,
        volume_24h_usd=p.volume_24h_usd,
        apr_24h_pct=apr_pct(p.volume_24h_usd, p.fee_pct, p.tvl_usd),
        created_at=p.created_at,
        url=p.url,
    )


__all__ = ["PoolClient", "DefimeError"]
