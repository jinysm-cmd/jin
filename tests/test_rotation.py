import numpy as np
import pandas as pd

from bot.rotation import DEFAULTS, Rotator, pick_targets, rank_symbols


def _daily(n_sym=40, n=100, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    return {f"S{i}USDT": pd.DataFrame({"close": 100 * np.exp(np.cumsum(rng.normal(0, .03, n))),
                                       "volume": np.full(n, 1000.0 - i),
                                       "close_time": idx + pd.Timedelta("1D")}, index=idx)
            for i in range(n_sym)}


def test_rank_excludes_young_and_low_volume():
    d = _daily()
    d["NEWUSDT"] = d["S0USDT"].iloc[-30:]
    r = rank_symbols(d, DEFAULTS)
    assert len(r) == 30 and "NEWUSDT" not in r.index and r.is_monotonic_increasing


def test_pick_targets_balanced_and_skips_disallowed():
    r = pd.Series(np.arange(12.0), index=[f"S{i}" for i in range(12)])
    t = pick_targets(r, 3, lambda s: s != "S11")
    assert [s for s, d in t.items() if d == 1] == ["S10", "S9", "S8"]
    assert [s for s, d in t.items() if d == -1] == ["S0", "S1", "S2"]
    assert pick_targets(r.iloc[:4], 3, lambda s: True) == {}


class FakeClient:
    def __init__(self, daily):
        self.daily = daily
        self.px = {s: float(df["close"].iloc[-1]) for s, df in daily.items()}

    def load_filters(self):
        return {s: {"status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                    "min_notional": 5.0} for s in self.daily}

    def filters(self, s):
        return self.load_filters()[s]

    def ticker_24h(self):
        return [{"symbol": s, "quoteVolume": "1e9"} for s in self.daily]

    def klines(self, s, interval, limit):
        return self.daily[s]

    def premium_index(self):
        return [{"symbol": s, "markPrice": p} for s, p in self.px.items()]

    def round_qty(self, s, q):
        return round(q, 6)

    def round_price(self, s, p):
        return p


def _paper(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = object.__new__(Rotator)
    r.cfg, r.mode, r.state_path = dict(DEFAULTS), "paper", "rotation_state_paper.json"
    r.c = FakeClient(_daily())
    r.state = r._load()
    return r


def test_paper_rebalance_and_stop(tmp_path, monkeypatch):
    r = _paper(tmp_path, monkeypatch)
    r.rebalance()
    pos = r.state["positions"]
    assert len(pos) == 10 and sum(p["side"] for p in pos.values()) == 0
    eq0 = r.equity(r.c.px)
    assert 349 < eq0 < 350        # 진입 수수료만큼만 줄어듦
    # 숏 하나가 40% 급등 → 비상손절
    short = next(s for s, p in pos.items() if p["side"] == -1)
    r.c.px[short] *= 1.4
    r.check_stops(r.c.px)
    assert short not in r.state["positions"]
    assert r.state["equity"] < eq0 - 0.3 * 350 / 10 * 0.9
    # 같은 순위로 다시 리밸런스하면 남은 포지션은 그대로 유지(재진입 수수료 없음)
    kept = dict(r.state["positions"])
    r.rebalance()
    for s, p in kept.items():
        assert r.state["positions"][s]["entry"] == p["entry"]


class FakeLive(FakeClient):
    def __init__(self, daily, foreign):
        super().__init__(daily)
        self.pos = {s: {"amt": 1.0, "entry": self.px[s], "mark": self.px[s]} for s in foreign}
        self.orders, self.stops = [], []

    def positions(self):
        return dict(self.pos)

    def balance_usdt(self):
        return 350.0

    def set_isolated(self, s):
        pass

    def set_leverage(self, s, lev):
        assert lev == 2

    def cancel_all(self, s):
        assert s not in self.foreign_syms

    def cancel_protective_stop(self, s, ref):
        pass

    def market_order(self, s, side, qty, reduce_only=False):
        self.orders.append((s, side, qty, reduce_only))
        sign = 1 if side == "BUY" else -1
        amt = self.pos.get(s, {"amt": 0})["amt"] + sign * qty
        if abs(amt) < 1e-12:
            self.pos.pop(s, None)
        else:
            self.pos[s] = {"amt": amt, "entry": self.px[s], "mark": self.px[s]}
        return {"avgPrice": str(self.px[s])}

    def place_protective_stop(self, s, close_side, stop_px):
        self.stops.append((s, close_side, stop_px))
        return {"kind": "algo", "id": len(self.stops)}


def test_live_rebalance_respects_capital_and_foreign_positions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    daily = _daily()
    ranked = rank_symbols(daily, DEFAULTS)
    foreign = [ranked.index[-1], ranked.index[0]]          # 1등 롱 후보와 1등 숏 후보를 다른 봇이 들고 있음
    r = object.__new__(Rotator)
    r.cfg, r.mode, r.state_path = {**DEFAULTS, "capital_usdt": 100}, "live", "rotation_state_live.json"
    r.c = FakeLive(daily, foreign)
    r.c.foreign_syms = set(foreign)
    r.state = r._load()
    r.rebalance()
    held = dict(r.state["positions"])
    assert len(held) == 10 and not set(foreign) & set(held)
    assert all(not ro for *_, ro in r.c.orders)            # 신규 진입만, 남의 포지션 청산 없음
    assert all(abs(p["qty"] * p["entry"] - 10) < 0.01 for p in held.values())   # 100 x 1 / 2 / 5
    for s, side, stop in r.c.stops:                        # 비상손절 방향 확인
        p = held[s]
        assert side == ("SELL" if p["side"] == 1 else "BUY")
        assert abs(stop / p["entry"] - (1 - 0.3 * p["side"])) < 1e-9
    # 다음 회차: 모두 정리되면 우리 것만 reduce-only 로 닫는다
    r.state["last_rebalance"] = None
    monkeypatch.setattr("bot.rotation.pick_targets", lambda *a: {})
    r.rebalance()
    closes = [o for o in r.c.orders if o[3]]
    assert {o[0] for o in closes} == set(held) and not r.state["positions"]
    assert set(r.c.pos) == set(foreign)
