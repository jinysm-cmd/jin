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
