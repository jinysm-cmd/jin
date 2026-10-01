import pytest

from bot.exchange import BinanceError
from bot.trader import LiveBroker


class FakeLive:
    def __init__(self, hedge=False, can_switch=True, iso_ok=True):
        self.hedge, self.can_switch, self.iso_ok = hedge, can_switch, iso_ok
        self.calls = []

    def is_hedge_mode(self):
        return self.hedge

    def set_one_way_mode(self):
        if not self.can_switch:
            raise BinanceError(400, -4068, "position exists")
        self.hedge = False

    def set_isolated(self, s):
        if not self.iso_ok:
            raise BinanceError(400, -4168, "multi-assets mode")
        self.calls.append(("iso", s))

    def set_leverage(self, s, lev):
        self.calls.append(("lev", s, lev))

    def market_order(self, s, side, qty, reduce_only=False):
        self.calls.append(("mkt", s, side, qty, reduce_only))
        return {"avgPrice": "100.5"}


def test_switches_hedge_to_one_way():
    c = FakeLive(hedge=True)
    LiveBroker(c, 5, True)
    assert c.hedge is False


def test_hedge_switch_failure_stops_bot():
    with pytest.raises(SystemExit):
        LiveBroker(FakeLive(hedge=True, can_switch=False), 5, True)


def test_isolated_failure_does_not_block_entry():
    c = FakeLive(iso_ok=False)
    b = LiveBroker(c, 5, True)
    assert b.open("XUSDT", 1, 1.0, 100) == 100.5
    assert ("lev", "XUSDT", 5) in c.calls and c.calls[-1][0] == "mkt"
