"""HL spells its 1000x markets kPEPE; our tickers arrive upper-cased.

A queued OPEN for kPEPE retried every poll and died on ``KeyError: 'KPEPE'``
each time, so the trade was never copied and the intent never left the queue.
"""

from __future__ import annotations

from invo_agent.dex.hl_trader import HlTrader, asset_name_index


class _FakeInfo:
    """The SDK's two lookups: name → coin, then coin → asset index."""

    def __init__(self) -> None:
        # Spot first, perps after — the order the SDK builds them in.
        self.name_to_coin = {
            "PURR/USDC": "PURR/USDC",
            "BTC": "BTC",
            "kPEPE": "kPEPE",
            "kBONK": "kBONK",
        }
        self.coin_to_asset = {"PURR/USDC": 10000, "BTC": 0, "kPEPE": 163, "kBONK": 58}

    def name_to_asset(self, name: str) -> int:
        return self.coin_to_asset[self.name_to_coin[name]]


def _trader() -> HlTrader:
    """HlTrader without __init__ — the real one opens a network Info client."""
    trader = object.__new__(HlTrader)
    trader.info = _FakeInfo()  # type: ignore[attr-defined]
    trader._asset_names = None  # type: ignore[attr-defined]
    return trader


def test_upper_cased_k_market_resolves_to_hl_spelling() -> None:
    trader = _trader()
    assert trader.hl_asset_name("KPEPE") == "kPEPE"
    assert trader.asset_index("KPEPE") == 163


def test_every_k_market_survives_upper_casing() -> None:
    trader = _trader()
    assert trader.asset_index("KBONK") == 58


def test_plain_tickers_are_untouched() -> None:
    trader = _trader()
    assert trader.hl_asset_name("BTC") == "BTC"
    assert trader.asset_index("BTC") == 0


def test_unknown_coin_still_raises_on_the_exchange_lookup() -> None:
    """An unlisted coin must fail as before, not resolve to something else."""
    trader = _trader()
    assert trader.hl_asset_name("NOPE") == "NOPE"
    try:
        trader.asset_index("NOPE")
    except KeyError:
        return
    raise AssertionError("unknown coin must not resolve")


def test_name_index_lets_the_perp_win_a_case_collision() -> None:
    """Spot is declared first, so the perp's spelling is the one that stands."""
    assert asset_name_index(["KPEPE", "kPEPE"])["KPEPE"] == "kPEPE"


def test_name_index_is_built_once() -> None:
    trader = _trader()
    trader.hl_asset_name("KPEPE")
    trader.info.name_to_coin = {}  # type: ignore[attr-defined]
    assert trader.hl_asset_name("KBONK") == "kBONK"
