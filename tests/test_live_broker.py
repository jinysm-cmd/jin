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

    def cancel_all(self, s):
        self.calls.append(("cancel_all", s))

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


class FakeExchange:
    """BinanceFutures 의 _request 만 흉내 내서 손절 등록/취소 경로를 검증."""

    def __init__(self, algo_only=True):
        from bot.exchange import BinanceFutures
        self.bf = BinanceFutures()
        self.bf._filters = {"XUSDT": {"tick": 0.01, "step": 0.001, "min_qty": 0.001, "max_qty": 1e9,
                                      "min_notional": 5}}
        self.calls = []
        self.algo_only = algo_only
        self.bf._request = self._request

    def _request(self, method, path, params=None, signed=False, retries=3):
        self.calls.append((method, path, dict(params or {})))
        if method == "POST" and path == "/fapi/v1/order" and params.get("type") == "STOP_MARKET" and self.algo_only:
            raise BinanceError(400, -4120, "use algo")
        if path == "/fapi/v1/algoOrder" and method == "POST":
            return {"algoId": 777}
        if path == "/fapi/v1/order" and method == "POST":
            return {"orderId": 55}
        return {}


def test_protective_stop_uses_algo_once_and_cancels_by_id():
    fx = FakeExchange(algo_only=True)
    ref = fx.bf.place_protective_stop("XUSDT", "SELL", 99.123)
    assert ref == {"kind": "algo", "id": 777}
    ref2 = fx.bf.place_protective_stop("XUSDT", "SELL", 98.0)
    # 두 번째부터는 일반 주문 시도 없이 바로 Algo
    posts = [c for c in fx.calls if c[0] == "POST"]
    assert [p[1] for p in posts] == ["/fapi/v1/order", "/fapi/v1/algoOrder", "/fapi/v1/algoOrder"]
    assert fx.bf.cancel_protective_stop("XUSDT", ref2) is True
    assert fx.calls[-1] == ("DELETE", "/fapi/v1/algoOrder", {"algoId": 777})


def test_live_close_cancels_stop_and_open_clears_stale_orders():
    c = FakeLive()
    c.cancel_protective_stop = lambda s, ref: c.calls.append(("cancel_stop", s, ref)) or True
    c.place_protective_stop = lambda s, side, px: {"kind": "algo", "id": 9}
    b = LiveBroker(c, 5, True)
    b.open("XUSDT", 1, 1.0, 100)
    assert ("cancel_all", "XUSDT") in c.calls
    assert c.calls.index(("cancel_all", "XUSDT")) < [i for i, x in enumerate(c.calls) if x[0] == "mkt"][0]
    ref = b.protect("XUSDT", 1, 95)
    b.close("XUSDT", 1, 1.0, 105, 100, ref)
    assert ("cancel_stop", "XUSDT", {"kind": "algo", "id": 9}) in c.calls
