# Fontes externas usadas

| Fonte | Endpoint | Uso | Limite |
|---|---|---|---|
| DexScreener | `GET https://api.dexscreener.com/latest/dex/pairs/{chain}/{pool_id}` | snapshot (primária p/ TVL, volume, preço, tokens) | 300/min (usamos 120/min) |
| DexScreener | `GET https://api.dexscreener.com/latest/dex/search?q=` | fallback de busca | 300/min |
| GeckoTerminal | `GET /api/v2/networks/{net}/pools/{pool_id}?include=base_token,quote_token,dex` | fee tier, TVL/volume secundários | 30/min grátis (usamos 24/min) |
| GeckoTerminal | `GET /api/v2/networks/{net}/pools/{pool_id}/ohlcv/{hour\|day}?limit=&currency=usd` | histórico | idem |
| GeckoTerminal | `GET /api/v2/networks/{net}/pools?sort=h24_volume_usd_desc&page=` | listagem | idem |
| GeckoTerminal | `GET /api/v2/search/pools?query=&network=` | busca | idem |

Leitura on-chain (Robinhood Chain): só `eth_call` em RPC público — nenhuma chave, nenhuma assinatura.
