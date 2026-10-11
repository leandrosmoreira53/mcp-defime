import pytest

from defime_mcp.exceptions import InvalidPoolId
from defime_mcp.ids import parse_pool_ref

from .conftest import POOL, SLUG


@pytest.mark.parametrize("inp", [
    f"https://dexscreener.com/robinhood/{POOL}",
    f"https://www.geckoterminal.com/robinhood/pools/{POOL}",
    SLUG,
    POOL,
    POOL.upper().replace("0X", "0x"),
])
def test_formats(inp):
    ref = parse_pool_ref(inp)
    assert ref.chain == "robinhood" and ref.pool_id == POOL and ref.is_v4_pool_id


def test_chain_and_id_separate():
    ref = parse_pool_ref(chain="ETH", pool_id="0x" + "a" * 40)
    assert ref.chain == "ethereum" and ref.chain_for("geckoterminal") == "eth" and not ref.is_v4_pool_id


@pytest.mark.parametrize("bad", ["0x123", "robinhood_0xzz", "0x" + "a" * 50, "https://example.com/x"])
def test_invalid(bad):
    with pytest.raises(InvalidPoolId):
        parse_pool_ref(bad)


def test_unknown_chain():
    with pytest.raises(InvalidPoolId, match="chain desconhecida"):
        parse_pool_ref(f"solanax_{POOL}")


def test_conflicting_chain():
    with pytest.raises(InvalidPoolId, match="conflitante"):
        parse_pool_ref(SLUG, chain="base")


def test_nothing():
    with pytest.raises(InvalidPoolId):
        parse_pool_ref()
