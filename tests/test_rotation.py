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
