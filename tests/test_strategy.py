import numpy as np
import pandas as pd

from bot.backtest_engine import Costs, portfolio, simulate_symbol, summarize
from bot.risk import notional_fraction
from bot.strategy import StrategyParams, generate_signals


def synthetic(n=6000, seed=0):
    rng = np.random.default_rng(seed)
    vol_regime = np.exp(np.cumsum(rng.normal(0, 0.02, n)) * 0.2) * 0.002
    ret = rng.standard_t(4, n) * vol_regime
    close = 100 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 1, n)) * vol_regime * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = np.exp(rng.normal(5, 0.6, n)) * (1 + 50 * np.abs(ret))
    buy_share = np.clip(0.5 + np.sign(ret) * rng.uniform(0, 0.3, n) + rng.normal(0, 0.1, n), 0.02, 0.98)
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close, "volume": volume,
        "taker_buy_volume": volume * buy_share, "close_time": idx + pd.Timedelta("5min") - pd.Timedelta("1ms"),
    }, index=idx)


def test_signals_exist_and_are_well_formed():
    df = synthetic()
    f = generate_signals(df, StrategyParams())
    s = f[f.signal != 0]
    assert len(s) > 20
    assert set(s.setup.unique()) <= {"ABSORB", "TRAP", "SQUEEZE"}
    assert (s.sl_dist > 0).all() and (s.tp_dist > 0).all()


def test_no_lookahead():
    """미래 봉을 잘라내도 과거 시점의 신호가 바뀌면 안 된다."""
    df = synthetic(seed=3)
    p = StrategyParams()
    full = generate_signals(df, p)
    cut = 4000
    part = generate_signals(df.iloc[:cut], p)
    pd.testing.assert_series_equal(full.signal.iloc[:cut], part.signal, check_names=False)
    pd.testing.assert_series_equal(full.sl_dist.iloc[:cut], part.sl_dist, check_names=False)


def test_funding_filter_blocks_crowded_side():
    df = synthetic(seed=5)
    p = StrategyParams()
    hot = pd.Series(0.01, index=df.index)  # 극단적 양(+) 펀딩 → 롱 금지
    f = generate_signals(df, p, hot)
    assert (f.signal == 1).sum() == 0


def test_backtest_runs_and_respects_limits():
    datasets = {f"S{i}": synthetic(seed=i) for i in range(4)}
    trades = []
    for s, df in datasets.items():
        trades += simulate_symbol(s, df, StrategyParams(), Costs())
    tdf, curve = portfolio(trades, risk_per_trade=0.005, leverage=5, max_positions=2)
    stats = summarize(tdf, curve)
    assert stats["trades"] > 0
    # 동시 보유 수 확인
    ev = sorted([(t, 1) for t in tdf.entry_time] + [(t, -1) for t in tdf.exit_time], key=lambda x: (x[0], x[1]))
    open_n, peak = 0, 0
    for _, d in ev:
        open_n += d
        peak = max(peak, open_n)
    assert peak <= 2
    # 손절 R 은 비용 포함 -1R 근처보다 크게 나쁘지 않아야 함(갭 제외)
    sl = tdf[tdf.reason == "SL"]
    assert (sl.r_multiple > -3).all()


def test_sizing_caps():
    assert abs(notional_fraction(100, 1, 0.005, 5, 4) - 0.5) < 1e-12   # 1% 손절 → 계좌의 50%
    assert notional_fraction(100, 0.01, 0.005, 5, 4) == 5 / 4          # 너무 타이트한 손절 → 레버리지 상한


def test_same_bar_exit_does_not_block_portfolio():
    """진입 봉에서 바로 청산된 거래가 포지션 슬롯을 영구 점유하면 안 된다(회귀 테스트)."""
    datasets = {f"S{i}": synthetic(n=20000, seed=i) for i in range(3)}
    trades = []
    for s, df in datasets.items():
        trades += simulate_symbol(s, df, StrategyParams(), Costs())
    tdf, _ = portfolio(trades, 0.005, 5, max_positions=10)
    assert len(tdf) == len(trades)
    assert (tdf.exit_time > tdf.entry_time).all()


def test_classic_engines_no_lookahead():
    from bot.classic import ENGINES

    df = synthetic(seed=7)
    for eng in ENGINES:
        p = StrategyParams(engine=eng)
        full = generate_signals(df, p)
        part = generate_signals(df.iloc[:4000], p)
        assert (full.signal != 0).sum() > 0, eng
        assert set(full.setup[full.signal != 0]) == {eng.upper()}, eng
        pd.testing.assert_series_equal(full.signal.iloc[:4000], part.signal, check_names=False)


def test_multi_engine_combines_signals():
    df = synthetic(seed=8)
    single = {e: (generate_signals(df, StrategyParams(engine=e)).signal != 0) for e in ("heikin", "macd")}
    combo = generate_signals(df, StrategyParams(engine="heikin,macd"))
    assert ((combo.signal != 0) == (single["heikin"] | single["macd"])).all()
    assert set(combo.setup[combo.signal != 0]) <= {"HEIKIN", "MACD"}


def test_winrate_knobs():
    datasets = {f"S{i}": synthetic(n=8000, seed=i) for i in range(3)}

    def run(**kw):
        p = StrategyParams(engine="heikin,pullback", **kw)
        tr = [t for s, df in datasets.items() for t in simulate_symbol(s, df, p, Costs())]
        return pd.DataFrame([vars(t) for t in tr])

    base = run()
    near_tp = run(tp_mult=0.5, min_rr=0.3)
    be = run(breakeven_r=0.5)
    adx = run(adx_min=25)
    win = lambda d: (d.gross_ret > 0).mean()
    assert win(near_tp) > win(base)            # 익절이 가까우면 승률↑
    assert (be.r_multiple < -0.5).sum() < (base.r_multiple < -0.5).sum()  # 본전손절로 큰 손실 수↓
    assert len(adx) < len(base)                # ADX 필터로 진입↓


def test_signals_without_stop_distance_are_ignored():
    from bot.backtest_engine import simulate_signals

    df = synthetic(n=500, seed=3)
    f = df.copy()
    f["signal"] = 0
    f["setup"] = ""
    f["sl_dist"] = np.nan
    f["tp_dist"] = np.nan
    f.iloc[100, f.columns.get_loc("signal")] = 1          # 손절거리 NaN → 무시
    f.iloc[300, f.columns.get_loc("signal")] = 1
    f.iloc[300, f.columns.get_loc("sl_dist")] = 1.0
    f.iloc[300, f.columns.get_loc("tp_dist")] = 2.0
    tr = simulate_signals("X", f, StrategyParams(), Costs())
    assert len(tr) == 1 and np.isfinite(tr[0].r_multiple)


def test_fixed_tp_pct_and_daily_stops():
    from bot.backtest_engine import Trade

    df = synthetic(n=6000, seed=4)
    f = generate_signals(df, StrategyParams(engine="heikin,pullback", tp_price_pct=0.006))
    s = f[f.signal != 0]
    assert np.allclose(s.tp_dist, s.close * 0.006)

    t0 = pd.Timestamp("2026-01-01 00:00", tz="UTC")
    mk = lambda h, sym, ret: Trade(sym, "X", 1, t0 + pd.Timedelta(hours=h), t0 + pd.Timedelta(hours=h + 1),
                                   100, 100 * (1 + ret), 1.0, "TP", 1, ret, ret, ret / 0.01)
    trades = [mk(0, "A", 0.02), mk(2, "B", 0.02), mk(4, "C", 0.02), mk(26, "D", 0.02)]
    # 거래당 손절 1% → 명목가 50%, +2% 수익 → 계좌 +1% 씩
    full, _ = portfolio(trades, 0.005, 5, 4)
    capped, _ = portfolio(trades, 0.005, 5, 4, daily_profit_stop=0.015)
    assert len(full) == 4
    assert list(capped.symbol) == ["A", "B", "D"]   # +2% 도달 후 그날 C 는 건너뛰고, 다음날 D 는 진입


def test_fixed_margin_sizing_and_roi_config(tmp_path):
    import yaml
    from bot.config import load_config

    assert notional_fraction(100, 4, 0.005, 5, 5, margin_per_trade=0.10) == 0.5   # 증거금 10% x 5배
    assert notional_fraction(100, 4, 0.005, 5, 5, margin_per_trade=0.30) == 1.0   # 5종목이면 20% 상한
    cfg = {"leverage": 5, "take_profit_roi": 0.04, "strategy": {}}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg))
    assert abs(load_config(str(p))["strategy"]["tp_price_pct"] - 0.008) < 1e-12


def test_fixed_sl_pct():
    df = synthetic(n=6000, seed=6)
    f = generate_signals(df, StrategyParams(engine="heikin,pullback", sl_price_pct=0.02, tp_price_pct=0.03))
    s = f[f.signal != 0]
    assert np.allclose(s.sl_dist, s.close * 0.02) and np.allclose(s.tp_dist, s.close * 0.03)


def test_tp_cap_to_recent_high():
    df = synthetic(n=6000, seed=9)
    base = generate_signals(df, StrategyParams(engine="heikin,pullback"))
    cap = generate_signals(df, StrategyParams(engine="heikin,pullback", tp_cap_bars=120))
    assert (cap.signal != 0).sum() <= (base.signal != 0).sum()
    s = cap[cap.signal == 1]
    hh = df["high"].rolling(120).max().reindex(s.index)
    assert (s.close + s.tp_dist <= hh + 1e-9).all()          # 익절가가 최근 고점을 넘지 않음
    assert (s.tp_dist >= s.sl_dist * 1.0 - 1e-9).all()       # 손익비 1 미만은 진입 안 함
