"""백테스트 엔진. 라이브 봇과 같은 strategy.generate_signals 를 사용한다.

체결 가정 (보수적으로):
- 신호는 봉 마감 시 확정 → 다음 봉 시가에 시장가 진입
- 한 봉 안에서 손절가와 익절가가 모두 닿으면 '손절 먼저' 로 처리
- 수수료(테이커) + 슬리피지를 진입/청산 양쪽에 부과
- 펀딩비 지불/수취는 반영하지 않음 (보유시간이 짧아 영향 작음)
"""
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .risk import notional_fraction
from .strategy import StrategyParams, generate_signals


@dataclass
class Costs:
    fee: float = 0.0005       # 바이낸스 선물 테이커 0.05% (BNB 할인/VIP 미적용)
    slippage: float = 0.0002  # 편도 0.02%


@dataclass
class Trade:
    symbol: str
    setup: str
    side: int
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    exit: float
    sl_dist: float
    reason: str
    bars: int
    gross_ret: float
    net_ret: float
    r_multiple: float


def simulate_symbol(symbol: str, df: pd.DataFrame, p: StrategyParams, costs: Costs,
                    funding: pd.Series | None = None, cooldown_bars: int = 3, flip: bool = False) -> list[Trade]:
    """flip=True 면 신호 방향을 반대로 뒤집어 시뮬레이션 (진단용)."""
    return simulate_signals(symbol, generate_signals(df, p, funding), p, costs, cooldown_bars, flip)


def simulate_signals(symbol: str, f: pd.DataFrame, p: StrategyParams, costs: Costs,
                     cooldown_bars: int = 3, flip: bool = False) -> list[Trade]:
    """이미 계산된 신호 프레임(signal/setup/sl_dist/tp_dist 컬럼)으로 체결 시뮬레이션.

    p 에서는 max_hold_bars, breakeven_r 만 사용한다.
    """
    o, h, l, c = (f[k].to_numpy() for k in ("open", "high", "low", "close"))
    sig = f["signal"].to_numpy()
    sl_d, tp_d = f["sl_dist"].to_numpy(), f["tp_dist"].to_numpy()
    setups = f["setup"].to_numpy()
    idx = f.index
    close_t = pd.DatetimeIndex(f["close_time"])
    n = len(f)
    trades: list[Trade] = []
    rt_cost = 2 * (costs.fee + costs.slippage)

    i = 0
    while i < n - 1:
        if sig[i] == 0:
            i += 1
            continue
        d = -int(sig[i]) if flip else int(sig[i])
        e_i = i + 1
        entry = o[e_i]
        sl = entry - d * sl_d[i]
        tp = entry + d * tp_d[i]
        exit_px, reason, j = None, "", e_i
        be_trigger = entry + d * p.breakeven_r * sl_d[i] if p.breakeven_r > 0 else None
        for j in range(e_i, n):
            hit_sl = l[j] <= sl if d == 1 else h[j] >= sl
            hit_tp = h[j] >= tp if d == 1 else l[j] <= tp
            if hit_sl:
                # 갭으로 손절가를 넘어서 시작했다면 시가에 체결
                exit_px = min(o[j], sl) if d == 1 else max(o[j], sl)
                reason = "SL"
                break
            if hit_tp:
                exit_px, reason = tp, "TP"
                break
            if j - e_i + 1 >= p.max_hold_bars:
                exit_px, reason = c[j], "TIME"
                break
            # 본전 손절: 이 봉에서 목표 수익에 닿았으면 다음 봉부터 손절을 본전(+왕복비용)으로
            if be_trigger is not None and ((h[j] >= be_trigger) if d == 1 else (l[j] <= be_trigger)):
                sl = entry * (1 + d * rt_cost)
                be_trigger = None
        if exit_px is None:  # 데이터 끝
            exit_px, reason, j = c[n - 1], "END", n - 1
        gross = d * (exit_px - entry) / entry
        net = gross - rt_cost
        trades.append(Trade(
            symbol=symbol, setup=setups[i], side=d, entry_time=idx[e_i], exit_time=close_t[j],
            entry=entry, exit=exit_px, sl_dist=sl_d[i], reason=reason, bars=j - e_i + 1,
            gross_ret=gross, net_ret=net, r_multiple=net / (sl_d[i] / entry),
        ))
        i = j + max(0, cooldown_bars)  # 청산 봉 마감 이후(쿨다운 반영)부터 다시 신호 탐색
    return trades


def portfolio(trades: list[Trade], risk_per_trade: float, leverage: float, max_positions: int,
              start_equity: float = 1000.0) -> tuple[pd.DataFrame, pd.Series]:
    """모든 심볼 거래를 시간순으로 합쳐 동시 포지션 제한과 복리 사이징을 적용."""
    if not trades:
        return pd.DataFrame(), pd.Series(dtype=float)
    tdf = pd.DataFrame([asdict(t) for t in trades]).sort_values(["entry_time", "symbol"]).reset_index(drop=True)
    events = []  # (time, order, kind, idx)  청산(0)을 진입(1)보다 먼저 처리
    for k, r in tdf.iterrows():
        events.append((r.entry_time, 1, "in", k))
        events.append((r.exit_time, 0, "out", k))
    events.sort(key=lambda x: (x[0], x[1], x[3]))

    equity = start_equity
    open_pos: dict[int, float] = {}  # trade idx -> 진입 시 명목가(USDT)
    open_syms: set[str] = set()
    accepted = np.zeros(len(tdf), dtype=bool)
    pnl = np.zeros(len(tdf))
    curve_t, curve_v = [], []
    for t, _, kind, k in events:
        r = tdf.loc[k]
        if kind == "in":
            if len(open_pos) >= max_positions or r.symbol in open_syms:
                continue
            frac = notional_fraction(r.entry, r.sl_dist, risk_per_trade, leverage, max_positions)
            open_pos[k] = frac * equity
            open_syms.add(r.symbol)
            accepted[k] = True
        elif k in open_pos:
            notional = open_pos.pop(k)
            open_syms.discard(r.symbol)
            pnl[k] = notional * r.net_ret
            equity += pnl[k]
            curve_t.append(t)
            curve_v.append(equity)
    tdf["accepted"] = accepted
    tdf["pnl"] = pnl
    out = tdf[tdf.accepted].copy()
    curve = pd.Series(curve_v, index=pd.DatetimeIndex(curve_t), name="equity")
    return out, curve


def summarize(tdf: pd.DataFrame, curve: pd.Series, start_equity: float = 1000.0) -> dict:
    if tdf.empty:
        return {"trades": 0}
    days = max((tdf.exit_time.max() - tdf.entry_time.min()).total_seconds() / 86400, 1e-9)
    wins = tdf[tdf.pnl > 0].pnl.sum()
    losses = -tdf[tdf.pnl < 0].pnl.sum()
    eq = pd.concat([pd.Series([start_equity]), curve.reset_index(drop=True)])
    dd = (eq / eq.cummax() - 1).min()
    return {
        "trades": int(len(tdf)),
        "trades_per_day": round(len(tdf) / days, 2),
        "win_rate": round((tdf.net_ret > 0).mean() * 100, 1),
        "avg_R": round(tdf.r_multiple.mean(), 3),
        "profit_factor": round(wins / losses, 2) if losses > 0 else float("inf"),
        "total_return_%": round((curve.iloc[-1] / start_equity - 1) * 100, 2) if len(curve) else 0.0,
        "max_drawdown_%": round(dd * 100, 2),
        "days": round(days, 1),
    }


def breakdown(tdf: pd.DataFrame, by: str) -> pd.DataFrame:
    if tdf.empty:
        return pd.DataFrame()
    g = tdf.groupby(by)
    return pd.DataFrame({
        "trades": g.size(),
        "win_%": g.apply(lambda x: (x.net_ret > 0).mean() * 100).round(1),
        "avg_R": g.r_multiple.mean().round(3),
        "pnl": g.pnl.sum().round(2),
    }).sort_values("pnl", ascending=False)
