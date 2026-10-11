"""Leitura on-chain (Robinhood Chain) via JSON-RPC público. Somente leitura: só `eth_call`.

Por que existe: os agregadores publicam a taxa NOMINAL do pool (0,30%), mas o LP não recebe
tudo isso. Na Robinhood Chain as pools V3 têm `feeProtocol = 102` (1/6 da fee vai ao protocolo,
o LP fica com 5/6 = 0,25%), e nas V4 o `protocolFee` é cobrado sobre a entrada antes da taxa do
LP. Um APR calculado com a taxa nominal sai ~20% maior do que o LP realmente ganha. Aqui a
fatia do LP é LIDA do contrato, não suposta.

Tudo é lido no MESMO bloco (um `eth_blockNumber` e depois um batch com `blockTag` fixo): preço,
liquidez e taxas de instantes diferentes não se misturam.

V3 (id de 40 hex = endereço do contrato do pool):
  token0() token1() fee() tickSpacing() slot0() liquidity()  + decimals()/symbol() dos tokens
  slot0: sqrtPriceX96, tick, …, feeProtocol (palavra 5; 4 bits por sentido, 0 = sem corte)

V4 (id de 64 hex = PoolId; o PoolManager é singleton):
  extsload(keccak(poolId ‖ 6))      → sqrtPriceX96 | tick<<160 | protocolFee<<184 | lpFee<<208
  extsload(keccak(poolId ‖ 6) + 3)  → liquidez ativa
  PositionManager.poolKeys(bytes25) → currency0, currency1, fee, tickSpacing, hooks
  (poolKeys só conhece pools em que alguém abriu posição por aquele PositionManager; se vier
  zerado, moedas/decimais/preço ficam null e isso é dito.)
"""

from __future__ import annotations

import logging
from typing import Any

from Crypto.Hash import keccak
from pydantic import BaseModel, Field

from ..exceptions import NotFoundHTTP, UpstreamError
from ..http import HttpClient
from ..ids import PoolRef

log = logging.getLogger("defime_mcp.onchain")
NAME = "onchain"

# Seletores (keccak dos assinaturas; conferidos em tests/test_onchain.py)
SEL = {
    "token0": "0x0dfe1681",
    "token1": "0xd21220a7",
    "fee": "0xddca3f43",
    "tickSpacing": "0xd0c93a7c",
    "slot0": "0x3850c7bd",
    "liquidity": "0x1a686502",
    "decimals": "0x313ce567",
    "symbol": "0x95d89b41",
    "extsload": "0x1e2eaeaf",
    "poolKeys": "0x86b6be7d",
}

# Robinhood Chain (chainId 4663) — conferidos contra a chain.
ROBINHOOD = {
    "pool_manager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    "position_manager": "0x58daec3116aae6d93017baaea7749052e8a04fa7",
}
NATIVE = "0x0000000000000000000000000000000000000000"
# EIP-1967: bytes32(uint256(keccak256("eip1967.proxy.beacon")) - 1)
BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
SEL_NAME = "0x06fdde03"
DYNAMIC_FEE_FLAG = 0x800000
Q96 = 2 ** 96


def keccak256(data: bytes) -> bytes:
    k = keccak.new(digest_bits=256)
    k.update(data)
    return k.digest()


def selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()[:8]


def _words(hexdata: str) -> list[int]:
    raw = hexdata[2:] if hexdata.startswith("0x") else hexdata
    return [int(raw[i:i + 64], 16) for i in range(0, len(raw) - len(raw) % 64, 64)]


def _signed(v: int, bits: int) -> int:
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >= 1 << (bits - 1) else v


def _addr(word: int) -> str:
    return "0x" + format(word & ((1 << 160) - 1), "040x")


def _decode_string(hexdata: str) -> str | None:
    """ABI `string` (ou `bytes32` legado). None se não der para ler."""
    w = _words(hexdata)
    raw = bytes.fromhex(hexdata[2:])
    try:
        if len(w) >= 2 and w[0] == 32:
            n = w[1]
            return raw[64:64 + n].decode("utf-8", "replace").strip("\x00") or None
        if len(w) == 1:
            return raw.rstrip(b"\x00").decode("utf-8", "replace") or None
    except (ValueError, IndexError):
        return None
    return None


def pool_state_slot(pool_id: str) -> int:
    """Slot do `Pool.State` no mapping `_pools` (índice 6) do PoolManager V4."""
    return int.from_bytes(keccak256(bytes.fromhex(pool_id[2:]) + (6).to_bytes(32, "big")), "big")


def v3_lp_share(fee_protocol: int) -> tuple[float, float]:
    """(fração da fee que fica com o LP, corte do protocolo) a partir do `feeProtocol` do slot0.

    4 bits por sentido; valor d > 0 = protocolo leva 1/d. Sem saber o sentido de cada swap,
    usa o MAIOR corte (conservador: nunca superestima a fee do LP).
    """
    cuts = [1 / d for d in (fee_protocol & 0x0F, fee_protocol >> 4) if d]
    cut = max(cuts) if cuts else 0.0
    return 1 - cut, cut


def v4_fees(protocol_fee_raw: int, lp_fee_pips: int) -> tuple[float, float, float]:
    """(taxa total cobrada do swap %, taxa que chega ao LP %, protocolFee % do maior sentido).

    Na V4 o protocolo cobra sobre a entrada e o LP sobre o que sobra:
      total = pf + lp × (1 − pf/1e6)      (em pips; 1e6 = 100%)
      LP    = lp × (1 − pf/1e6)
    `protocolFee` tem 12 bits por sentido; usa o maior (conservador para o LP).
    """
    pf = max(protocol_fee_raw & 0xFFF, (protocol_fee_raw >> 12) & 0xFFF)
    lp_net = lp_fee_pips * (1 - pf / 1e6)
    total = pf + lp_net
    return total / 1e4, lp_net / 1e4, pf / 1e4


class OnchainToken(BaseModel):
    address: str | None = None
    symbol: str | None = None
    decimals: int | None = None


class OnchainState(BaseModel):
    """Estado do pool lido do contrato num único bloco. Taxas em % (0.25 = 0,25%)."""

    source: str = NAME
    rpc: str
    block: int
    protocol: str                     # "uniswap_v3" | "uniswap_v4"
    token0: OnchainToken = Field(default_factory=OnchainToken)
    token1: OnchainToken = Field(default_factory=OnchainToken)
    fee_tier_pct: float | None = None           # taxa nominal declarada (V3 fee(), V4 lpFee)
    swap_fee_total_pct: float | None = None     # o que o trader paga à pool (V4: + protocolFee)
    lp_fee_pct: float | None = None             # o que efetivamente chega ao LP
    protocol_fee_share: float | None = None     # fração da fee do LP desviada ao protocolo (V3)
    protocol_fee_pct: float | None = None       # V4: protocolFee em % da entrada
    dynamic_fee: bool | None = None
    hooks: str | None = None
    tick: int | None = None
    tick_spacing: int | None = None
    sqrt_price_x96: str | None = None           # string: não cabe em float sem perder precisão
    active_liquidity: str | None = None
    price_token1_per_token0: float | None = None
    price_token0_per_token1: float | None = None
    notes: list[str] = Field(default_factory=list)


class OnChain:
    def __init__(self, http: HttpClient, rpc_urls: list[str], contracts: dict[str, str] | None = None):
        self.http = http
        self.rpc_urls = [u for u in rpc_urls if u]
        self.c = contracts or ROBINHOOD

    # ---------- JSON-RPC ----------

    async def _rpc_batch(self, url: str, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
        data = await self.http.post_json(url, body)
        if isinstance(data, dict):  # alguns nós devolvem um objeto de erro único para o batch
            raise UpstreamError(f"rpc: {data.get('error') or 'resposta de batch inválida'}")
        by_id = {r.get("id"): r for r in data if isinstance(r, dict)}
        out = []
        for i in range(len(calls)):
            r = by_id.get(i)
            if r is None:
                raise UpstreamError("rpc: batch veio incompleto")
            out.append(r.get("result") if "error" not in r else None)
        return out

    async def _with_fallback(self, fn):
        last: Exception | None = None
        for url in self.rpc_urls:
            try:
                return await fn(url)
            except UpstreamError as exc:
                last = exc
                log.warning("rpc %s falhou: %s", url, exc)
        raise UpstreamError(f"onchain: nenhum RPC respondeu ({last})")

    async def _calls_at_block(self, url: str, calls: list[tuple[str, str]]) -> tuple[int, list[str | None]]:
        head = await self._rpc_batch(url, [("eth_blockNumber", [])])
        if not head[0]:
            raise UpstreamError("rpc: eth_blockNumber sem resultado")
        block = int(head[0], 16)
        tag = hex(block)
        res = await self._rpc_batch(url, [("eth_call", [{"to": to, "data": data}, tag]) for to, data in calls])
        return block, res

    # ---------- tokens ----------

    async def token_facts(self, addresses: list[str]) -> dict[str, dict[str, Any]]:
        """{endereço: {"beacon": 0x…|None, "name": str|None, "symbol": str|None}} lidos da chain.

        O beacon (slot EIP-1967) é o que identifica um token de ação da Robinhood: todos apontam
        para o mesmo beacon. O nome NÃO serve de prova (qualquer um batiza um token de
        "X – Robinhood Token"); vai junto só para exibição.
        """
        todo = [a.lower() for a in dict.fromkeys(addresses) if a and a.lower() != NATIVE]
        out: dict[str, dict[str, Any]] = {}
        if not todo:
            return out

        async def run(url: str) -> dict[str, dict[str, Any]]:
            res: dict[str, dict[str, Any]] = {}
            for i in range(0, len(todo), 8):  # 3 chamadas por token → lotes de 24
                part = todo[i:i + 8]
                calls: list[tuple[str, list[Any]]] = []
                for a in part:
                    calls += [("eth_getStorageAt", [a, BEACON_SLOT, "latest"]),
                              ("eth_call", [{"to": a, "data": SEL_NAME}, "latest"]),
                              ("eth_call", [{"to": a, "data": SEL["symbol"]}, "latest"])]
                r = await self._rpc_batch(url, calls)
                for j, a in enumerate(part):
                    beacon_raw, name_raw, sym_raw = r[3 * j:3 * j + 3]
                    if beacon_raw is None:
                        raise UpstreamError(f"rpc: sem resposta de storage para {a}")
                    b = int(beacon_raw, 16) if beacon_raw not in ("0x", "") else 0
                    res[a] = {"beacon": _addr(b) if b else None,
                              "name": _decode_string(name_raw) if name_raw and len(name_raw) > 2 else None,
                              "symbol": _decode_string(sym_raw) if sym_raw and len(sym_raw) > 2 else None}
            return res

        out.update(await self._with_fallback(run))
        return out

    # ---------- leitura ----------

    async def read(self, ref: PoolRef) -> OnchainState:
        if not self.rpc_urls:
            raise UpstreamError("onchain: nenhum RPC configurado")
        return await self._with_fallback(lambda url: self._read_v4(url, ref) if ref.is_v4_pool_id
                                         else self._read_v3(url, ref))

    async def _tokens(self, url: str, block: int, addrs: list[str]) -> list[OnchainToken]:
        erc20 = [a for a in addrs if a != NATIVE]
        calls = []
        for a in erc20:
            calls += [("eth_call", [{"to": a, "data": SEL["decimals"]}, hex(block)]),
                      ("eth_call", [{"to": a, "data": SEL["symbol"]}, hex(block)])]
        res = await self._rpc_batch(url, calls) if calls else []
        meta: dict[str, OnchainToken] = {}
        for i, a in enumerate(erc20):
            dec_raw, sym_raw = res[2 * i], res[2 * i + 1]
            dec = _words(dec_raw)[0] if dec_raw and len(dec_raw) > 2 else None
            meta[a] = OnchainToken(address=a, decimals=dec if dec is not None and dec <= 36 else None,
                                   symbol=_decode_string(sym_raw) if sym_raw and len(sym_raw) > 2 else None)
        return [meta.get(a) or OnchainToken(address=a, symbol="ETH", decimals=18) for a in addrs]

    async def _read_v3(self, url: str, ref: PoolRef) -> OnchainState:
        p = ref.pool_id
        names = ["token0", "token1", "fee", "tickSpacing", "slot0", "liquidity"]
        block, res = await self._calls_at_block(url, [(p, SEL[n]) for n in names])
        r = dict(zip(names, res))
        if not r["fee"] or r["fee"] == "0x" or not r["slot0"] or r["slot0"] == "0x":
            raise NotFoundHTTP(f"{NAME}: {p} não responde como pool Uniswap V3 na chain {ref.chain}")
        s0 = _words(r["slot0"])
        if len(s0) < 7:
            # O slot0 da Uniswap V3 devolve 7 palavras. Medido em 19/09: pools "NVDA/USDG" e "AAPL/USDG"
            # respondem fee() mas o slot0 tem 1 palavra (outro AMM). Lê-los como V3 publicava taxa 0%.
            raise NotFoundHTTP(f"{NAME}: {p} não é pool Uniswap V3 (slot0 com {len(s0)} palavra(s), esperado 7)")
        fee_pips = _words(r["fee"])[0]
        share, cut = v3_lp_share(s0[5] if len(s0) > 5 else 0)
        t0, t1 = _addr(_words(r["token0"])[0]), _addr(_words(r["token1"])[0])
        tok0, tok1 = await self._tokens(url, block, [t0, t1])
        st = OnchainState(
            rpc=url, block=block, protocol="uniswap_v3", token0=tok0, token1=tok1,
            fee_tier_pct=fee_pips / 1e4, swap_fee_total_pct=fee_pips / 1e4,
            lp_fee_pct=fee_pips / 1e4 * share, protocol_fee_share=cut, dynamic_fee=False,
            tick=_signed(s0[1], 24), tick_spacing=_signed(_words(r["tickSpacing"])[0], 24),
            sqrt_price_x96=str(s0[0]), active_liquidity=str(_words(r["liquidity"])[0]),
        )
        if cut:
            st.notes.append(f"feeProtocol ativo: o protocolo leva {cut:.4g} da fee; o LP recebe "
                            f"{st.lp_fee_pct:.4g}% e não os {st.fee_tier_pct:.4g}% nominais.")
        return _with_price(st)

    async def _read_v4(self, url: str, ref: PoolRef) -> OnchainState:
        base = pool_state_slot(ref.pool_id)
        pm, posm = self.c["pool_manager"], self.c["position_manager"]
        calls = [
            (pm, SEL["extsload"] + format(base, "064x")),
            (pm, SEL["extsload"] + format(base + 3, "064x")),
            (posm, SEL["poolKeys"] + ref.pool_id[2:52].ljust(64, "0")),
        ]
        block, (slot0_raw, liq_raw, key_raw) = await self._calls_at_block(url, calls)
        slot0 = _words(slot0_raw)[0] if slot0_raw and len(slot0_raw) > 2 else 0
        sqrt_p = slot0 & ((1 << 160) - 1)
        if sqrt_p == 0:
            raise NotFoundHTTP(f"{NAME}: PoolId {ref.pool_id[:10]}… não inicializado no PoolManager "
                               f"{pm[:10]}… da chain {ref.chain}")
        tick = _signed(slot0 >> 160, 24)
        protocol_raw = (slot0 >> 184) & 0xFFFFFF
        lp_pips = (slot0 >> 208) & 0xFFFFFF
        total, lp_net, pf = v4_fees(protocol_raw, lp_pips)
        st = OnchainState(
            rpc=url, block=block, protocol="uniswap_v4", fee_tier_pct=lp_pips / 1e4,
            swap_fee_total_pct=total, lp_fee_pct=lp_net, protocol_fee_pct=pf, tick=tick,
            sqrt_price_x96=str(sqrt_p),
            active_liquidity=str(_words(liq_raw)[0] & ((1 << 128) - 1)) if liq_raw and len(liq_raw) > 2 else None,
        )
        key = _words(key_raw) if key_raw and len(key_raw) > 2 else []
        if len(key) >= 5 and (key[0] or key[1]):
            c0, c1 = _addr(key[0]), _addr(key[1])
            fee_key = key[2] & 0xFFFFFF
            st.tick_spacing = _signed(key[3], 24)
            st.hooks = _addr(key[4])
            st.dynamic_fee = fee_key == DYNAMIC_FEE_FLAG
            # A chave precisa reproduzir o PoolId; senão não é esta pool.
            enc = b"".join(w.to_bytes(32, "big") for w in (key[0], key[1], fee_key, key[3] & ((1 << 256) - 1), key[4]))
            if "0x" + keccak256(enc).hex() == ref.pool_id:
                st.token0, st.token1 = await self._tokens(url, block, [c0, c1])
            else:
                st.notes.append("poolKeys não reproduz o PoolId; moedas não confirmadas.")
            if st.hooks != NATIVE:
                st.notes.append(f"Pool com hook {st.hooks}: o hook pode alterar a taxa por swap; "
                                "lp_fee_pct é a do slot0 neste bloco.")
            if st.dynamic_fee:
                st.notes.append("Taxa dinâmica (flag 0x800000): o valor pode mudar a qualquer swap.")
        else:
            st.notes.append("PositionManager não conhece a PoolKey (nenhuma posição aberta por ele); "
                            "moedas, decimais e preço on-chain ficam null.")
        hooked = st.hooks is not None and st.hooks != NATIVE
        if lp_pips == 0 and (st.dynamic_fee or hooked or st.hooks is None):
            # slot0 com lpFee 0 numa pool com hook/taxa dinâmica NÃO é "taxa zero": o hook define a taxa a
            # cada swap e ela não fica gravada. Medido em 19/09 em SPY, NVDA e TSLA/USDG V4 — antes saíam
            # com APR 0,0% e volume de milhões.
            st.fee_tier_pct = st.swap_fee_total_pct = st.lp_fee_pct = None
            st.notes.append("lpFee = 0 no slot0 com hook/taxa dinâmica: a taxa é definida pelo hook a cada "
                            "swap e não é legível do estado; lp_fee_pct fica null.")
        if pf:
            st.notes.append(f"protocolFee de {pf:.4g}% cobrado sobre a entrada: o swap paga "
                            f"{total:.4g}% e o LP recebe {lp_net:.4g}%.")
        return _with_price(st)


def _with_price(st: OnchainState) -> OnchainState:
    d0, d1 = st.token0.decimals, st.token1.decimals
    if st.sqrt_price_x96 and d0 is not None and d1 is not None:
        sp = int(st.sqrt_price_x96)
        # int até o fim; float só na apresentação
        num, den = sp * sp * 10 ** d0, Q96 * Q96 * 10 ** d1
        if num:
            st.price_token1_per_token0 = num / den
            st.price_token0_per_token1 = den / num
    return st
