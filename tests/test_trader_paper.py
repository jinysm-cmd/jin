"""네트워크 없이 paper 모드 루프 한 사이클을 돌려보는 스모크 테스트."""
import os

import pandas as pd
import yaml

from bot import trader as tr
from tests.test_strategy import synthetic


class FakeClient:
    def __init__(self, *a, **k):
        self.df = synthetic(n=800, seed=1)

    def sync_time(self):
        pass

    def load_filters(self):
        return {"AAAUSDT": self.filters("AAAUSDT")}

    def filters(self, s):
        return {"status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                "tick": 0.0001, "step": 0.001, "min_qty": 0.001, "max_qty": 1e9, "min_notional": 5}

    def round_qty(self, s, q):
        return round(q, 3)

    def klines(self, symbol, interval, limit=500, **k):
        df = self.df.copy()
        df.index = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("5min") - pd.Timedelta("5min"),
                                 periods=len(df), freq="5min")
        df["close_time"] = df.index + pd.Timedelta("5min") - pd.Timedelta("1ms")
        return df.tail(limit)

    def premium_index(self):
        return [{"symbol": "AAAUSDT", "markPrice": str(self.df.close.iloc[-1]), "lastFundingRate": "0.0001"}]


def test_paper_cycle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tr, "BinanceFutures", FakeClient)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = yaml.safe_load(open(os.path.join(here, "config.example.yaml"), encoding="utf-8"))
    cfg["symbols"] = ["AAAUSDT"]
    t = tr.Trader(cfg)
    t._refresh_universe()
    marks = t._marks()
    t.manage_positions(marks)
    t.scan(marks)
    # 강제로 신호 없는 상황이어도 예외 없이 끝나야 하고, 포지션이 있으면 손절/익절 체크가 동작해야 함
    t.state["positions"]["AAAUSDT"] = {"side": 1, "qty": 1.0, "entry": 1e9, "sl": 1e8, "tp": 2e9,
                                       "setup": "TEST", "entry_time": "", "expire_ms": 0}
    t.manage_positions(marks)
    assert "AAAUSDT" not in t.state["positions"]
    assert t.state["trades"][-1]["reason"] == "SL"


def test_late_start_skips_stale_signal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tr, "BinanceFutures", FakeClient)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = yaml.safe_load(open(os.path.join(here, "config_4h.yaml"), encoding="utf-8"))
    cfg["symbols"] = ["AAAUSDT"]
    t = tr.Trader(cfg)
    t._refresh_universe()
    calls = []
    t.scan = lambda marks: calls.append(1)
    bar = 4 * 3600 * 1000
    base = 1_000 * bar
    assert t.maybe_scan({}, base + 3 * 3600 * 1000) is False   # 마감 3시간 뒤 시작 → 건너뜀
    assert calls == []
    assert t.maybe_scan({}, base + bar + 10_000) is True       # 다음 봉 마감 10초 뒤 → 스캔
    assert calls == [1]
    assert t.maybe_scan({}, base + bar + 60_000) is False      # 같은 봉 중복 스캔 없음


def _trader_with_signal(tmp_path, monkeypatch, side, **opts):
    """scan 이 반드시 한 번 신호를 보도록 generate_signals 를 고정한 Trader."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tr, "BinanceFutures", FakeClient)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = yaml.safe_load(open(os.path.join(here, "config.example.yaml"), encoding="utf-8"))
    cfg.update(symbols=["AAAUSDT"], **opts)
    t = tr.Trader(cfg)
    t._refresh_universe()

    def fake_signals(df, p, funding=None):
        f = df.copy()
        f["signal"], f["setup"] = 0, ""
        f["sl_dist"], f["tp_dist"] = f["close"] * 0.02, f["close"] * 0.04
        f.iloc[-1, f.columns.get_loc("signal")] = side
        f.iloc[-1, f.columns.get_loc("setup")] = "TEST"
        return f
    monkeypatch.setattr(tr, "generate_signals", fake_signals)
    return t


def test_side_cap_blocks_extra_same_direction(tmp_path, monkeypatch):
    t = _trader_with_signal(tmp_path, monkeypatch, 1, max_per_side=1)
    t.state["positions"]["BBBUSDT"] = {"side": 1, "qty": 1, "entry": 1, "sl": 0.5, "tp": 2, "setup": "X"}
    t.scan(t._marks())
    assert "AAAUSDT" not in t.state["positions"]          # 롱이 이미 1개 → 추가 롱 금지
    t.state["positions"]["BBBUSDT"]["side"] = -1
    t.scan(t._marks())
    assert t.state["positions"]["AAAUSDT"]["side"] == 1   # 반대 방향만 있으면 진입


def test_btc_trend_filter_blocks_counter_trend(tmp_path, monkeypatch):
    t = _trader_with_signal(tmp_path, monkeypatch, 1, btc_trend_filter=True)
    monkeypatch.setattr(t, "_btc_side", lambda now: -1)   # BTC 하락 추세 → 롱 금지
    t.scan(t._marks())
    assert "AAAUSDT" not in t.state["positions"]
    monkeypatch.setattr(t, "_btc_side", lambda now: 1)
    t.scan(t._marks())
    assert "AAAUSDT" in t.state["positions"]
    assert t._btc_side.__name__ == "<lambda>" and tr.Trader._btc_side(t, pd.Timestamp.now(tz="UTC")) in (-1, 0, 1)


def test_min_listing_days_skips_new_coin(tmp_path, monkeypatch):
    t = _trader_with_signal(tmp_path, monkeypatch, 1, min_listing_days=180)
    now_ms = int(pd.Timestamp.now(tz="UTC").value // 1_000_000)
    base = t.client.filters("AAAUSDT")
    monkeypatch.setattr(t.client, "filters", lambda s: {**base, "onboard_ms": now_ms - 30 * 86_400_000})
    t.scan(t._marks())
    assert "AAAUSDT" not in t.state["positions"]          # 상장 30일 → 제외
    monkeypatch.setattr(t.client, "filters", lambda s: {**base, "onboard_ms": now_ms - 400 * 86_400_000})
    t.scan(t._marks())
    assert "AAAUSDT" in t.state["positions"]
