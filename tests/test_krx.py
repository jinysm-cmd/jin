import numpy as np
import pandas as pd

from krx import engine, rotation_bt as rb, strategy


def synthetic_daily(n=900, seed=0, drift=0.0003):
    rng = np.random.default_rng(seed)
    ret = rng.normal(drift, 0.025, n)
    close = 10_000 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.005, n))
    spread = np.abs(rng.normal(0, 0.01, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = np.exp(rng.normal(13, 0.5, n)) * (1 + 20 * np.abs(ret))
    idx = pd.bdate_range("2016-01-04", periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def test_signals_no_lookahead():
    df = synthetic_daily(seed=1)
    p = strategy.Params(min_tv20=0)
    full = strategy.compute(df, p)
    cut = 700
    part = strategy.compute(df.iloc[:cut], p)
    cols = ["sigA", "sigB", "sigC", "dist", "stopA", "stopB", "stopC"]
    pd.testing.assert_frame_equal(full.iloc[:cut][cols], part[cols])


def test_rotation_score_no_lookahead():
    data = {f"{i:06d}": synthetic_daily(seed=i) for i in range(30)}
    o, h, c, v = rb.wide(data)
    cfg = rb.RotCfg(min_tv20=0)
    full = rb.score_panel(o, h, c, v, cfg)
    cut = 700
    part = rb.score_panel(o.iloc[:cut], h.iloc[:cut], c.iloc[:cut], v.iloc[:cut], cfg)
    pd.testing.assert_frame_equal(full.iloc[:cut], part)


def test_rotation_flat_market_loses_only_costs():
    """가격이 전혀 안 움직이면 수익은 0 이하(비용만큼 손실)여야 한다."""
    idx = pd.bdate_range("2016-01-04", periods=600)
    data = {}
    for i in range(30):
        px = 5000.0 + i
        data[f"{i:06d}"] = pd.DataFrame({"open": px, "high": px, "low": px, "close": px,
                                         "volume": 1e6 * (1 + (np.arange(600) % (i + 2)))}, index=idx)
    o, h, c, v = rb.wide(data)
    cfg = rb.RotCfg(min_tv20=0, top_n=5)
    eq, st = rb.run(o, c, rb.score_panel(o, h, c, v, cfg), cfg)
    assert st["total"] <= 0
    assert st["total"] > -0.5


def test_engine_runs_and_charges_costs():
    data = {f"{i:06d}": synthetic_daily(seed=10 + i) for i in range(20)}
    p = strategy.Params(min_tv20=0, brk_tv_mult=1.5, brk_min_ret=0.02)
    sigs = {c: strategy.compute(df, p) for c, df in data.items()}
    res = engine.run(engine.prepare(data, sigs), engine.signal_book(sigs, ("B", "C")), engine.EngineCfg(setups=("B", "C")))
    assert len(res.trades) > 0
    assert (res.trades["exit_date"] >= res.trades["entry_date"]).all()
    # 진입가에 매수 비용이 들어갔는지
    assert res.equity.iloc[0] <= engine.EngineCfg().init_cash
