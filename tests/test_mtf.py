import numpy as np
import pandas as pd

from bot import mtf_heikin as m
from bot.backtest_engine import Costs
from tests.test_strategy import synthetic


def test_mtf_no_lookahead_and_runs():
    df = synthetic(n=12000, seed=21)
    p = m.MTFParams()
    full = m.signals(df, p)
    part = m.signals(df.iloc[:9000], p)
    pd.testing.assert_series_equal(full.signal.iloc[:9000], part.signal, check_names=False)
    tr = m.simulate("X", df, p, Costs())
    for t in tr:
        assert t.exit_time > t.entry_time and np.isfinite(t.r_multiple)
        assert t.r_multiple > -2.5  # 손절은 대략 -1R(+비용) 근처


def test_partial_take_profit_math():
    # 진입 100, 손절폭 1: TP1(101) 30%, TP2(102) 40%, 나머지 30% 는 손절(99)로 끝나는 경우
    idx = pd.date_range("2026-01-01", periods=10, freq="5min", tz="UTC")
    f = pd.DataFrame({"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0}, index=idx)
    f.loc[idx[2], "high"] = 102.5      # TP1, TP2 동시 도달
    f.loc[idx[3], "low"] = 98.5        # 이후 손절
    f["signal"] = 0
    f.iloc[0, f.columns.get_loc("signal")] = 1
    f["sl_dist"] = 1.0
    f["size"] = 1.0
    f["ha_red"] = False
    f["ha_green"] = True
    f["ema20"] = 100.0
    orig = m.signals
    m.signals = lambda df, p: f
    try:
        tr = m.simulate("X", f, m.MTFParams(), Costs(0, 0))
    finally:
        m.signals = orig
    gross = 0.3 * 0.01 + 0.4 * 0.02 + 0.3 * -0.01
    assert abs(tr[0].gross_ret - gross) < 1e-12
