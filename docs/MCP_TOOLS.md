# Tools MCP

Escopo (padrão, `SCOPE=rwa_bluechip`): **só Robinhood Chain**, só pools de **tokens RWA da Robinhood**
(ações/ETFs tokenizados) e **cripto blue chip** (ETH, WETH, cbBTC), com USDG como contraparte.
Memecoin, par stable/stable e outras chains → erro "fora do escopo". `SCOPE=all` volta ao comportamento amplo.

Classificação por contrato, nunca por símbolo (a chain tem ~20 tokens falsos chamados "USDC"):

| tipo | regra |
|---|---|
| `rwa` | slot de beacon EIP-1967 do token = `0xe10b6f6b…1b00` (o beacon de todos os "… – Robinhood Token") |
| `bluechip` | ETH nativo, WETH `0x0bd7d308…ad73`, cbBTC `0xcec185eb…0be4` (`BLUECHIP_TOKENS`) |
| `stable` | USDG `0x5fc5360d…d168` (`STABLE_TOKENS`); só como contraparte |

Todas as respostas têm: `sources`, `retrieved_at`, `cache {hit, age_seconds, stale}`, `derived_fields`, `notes`.
Erros voltam como mensagem legível (pool inválido, fora do escopo, não encontrado, rate limit, fonte fora).

## Identificação de pool (get_pool, get_pool_history, compare)

- `pool="https://dexscreener.com/robinhood/0x9666…"` (também URL do GeckoTerminal ou slug `robinhood_0x…`)
- `pool="robinhood_0x9666…"` ou `pool="0x9666…"`
- `chain="robinhood", pool_id="0x9666…"`

`pool_id` com 64 hex = PoolId do Uniswap v4; com 40 hex = endereço do pool (v3).

## defime_get_pool

Snapshot atual. `category` (rwa | bluechip), tokens com `kind` e, no RWA, o nome do ativo
("Micron Technology – Robinhood Token").

- `fee_pct` = taxa nominal (agregador). `lp_fee_pct` = taxa que **chega ao LP**, lida do contrato:
  V3 com `feeProtocol=102` → 5/6 da nominal (0,30% → 0,25%); V4 → `lpFee × (1 − protocolFee/1e6)`.
- `metrics`: price_usd, tvl_usd, volume 5m/1h/6h/24h, `fees_24h_usd` e `apr_24h_pct` (nominal, conta usual de
  agregadores) e **`fees_24h_lp_usd` / `apr_24h_lp_pct`** (com a taxa do LP — use esta para rendimento de LP).
- `onchain`: bloco lido, protocolo, token0/1 com decimais, tick, tickSpacing, liquidez ativa, sqrtPrice,
  preço pelo sqrtPrice, hook e taxa dinâmica (V4). Tudo do MESMO bloco.
- `cross_check`: TVL, volume e **preço** por fonte (DexScreener, GeckoTerminal, on-chain) + divergência %.
- `missing_fields`: o que nenhuma fonte forneceu.

## defime_get_pool_history

`timeframe`: `1d` / `7d` (horário), `30d` / `90d` (diário). `limit` ≤ 200.
Cada ponto: volume_usd, fees_usd, apr_pct, **apr_lp_pct**, close_price_usd, **no_swaps**.
O GeckoTerminal omite períodos sem swap; eles entram com volume 0 e `no_swaps=true` — senão o APR médio
sai inflado (no pool de teste: 9 candles em 16 dias → a média antiga saía 1,78x maior). Períodos depois do
último candle não são preenchidos (pode ser atraso do indexador) e isso vai em `notes`.
APR por período usa o TVL atual como proxy (aproximação usual de agregadores).

## defime_compare_pools

`pools`: lista de 2 a 5. Lado a lado: categoria, taxa nominal e do LP, TVL, volume, giro (volume/TVL),
fees 24h do LP, APR bruto e do LP, preço, divergência entre fontes, e `leaders` por métrica.
Um pool que falhar (fora do escopo, inválido) vira linha com `error`, sem derrubar os outros.

## defime_list_pools

`sort_by` = volume | tvl | apr, `category` = rwa | bluechip, `limit` ≤ 50, `min_tvl_usd`.
Universo: pools com USDG (3 páginas), WETH e cbBTC (1 cada) e o topo geral (1) no GeckoTerminal, filtrados
pelo escopo. 6 chamadas por `CACHE_TTL_LIST` (600s). Pools muito pequenos podem ficar de fora.

## defime_search_pools

`query` (símbolo, nome, endereço), `limit` ≤ 50. GeckoTerminal com fallback para DexScreener; resultados fora
do escopo são descartados e contados em `notes`.
