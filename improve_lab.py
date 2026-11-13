"""실전 봇(4시간봉 heikin+pullback) 보완안 비교.

  python3 improve_lab.py 2>&1 | tee improve_result.txt

실전 150거래에서 보인 문제를 과거 3년 데이터로 재현하고, 보완안이 '앞 70% / 뒤 30%' 모두에서
나아지는지 본다. 실전과 같게 하려고 '그 시점 거래대금 상위 30개'만 거래한다(오늘 기준 상위가 아님).

보완안
  방향제한 : 같은 방향 포지션 최대 3개 (실전에서 롱/숏 5~7개가 한꺼번에 손절됨)
  BTC추세  : BTC 가 4시간봉 EMA200 위면 롱만, 아래면 숏만
  신규제외 : 상장 180일 미만 코인 제외 (실전에서 AGT, BTW, DOOD, Q 등 신규 코인 손실이 컸음)
"""
import argparse
import logging

import numpy as np
import pandas as pd

from bot.backtest_engine import Costs, portfolio, simulate_signals, summarize
from bot.classic import ema
from bot.config import load_config
from bot.data import load_klines, top_symbols
from bot.exchange import BinanceFutures
from bot.strategy import StrategyParams, generate_signals

EXCLUDE = ["PAXGUSDT", "XAUTUSDT"]


def universe_mask(data: dict, top_n: int) -> pd.DataFrame:
    """각 4시간봉 시점에 '어제까지 3일 평균 거래대금' 상위 top_n 이면 True (미래 정보 없음)."""
    qv = pd.DataFrame({s: (df["close"] * df["volume"]).resample("1D").sum() for s, df in data.items()})
    rank = qv.rolling(3, min_periods=1).mean().rank(axis=1, ascending=False).shift(1)
    return rank <= top_n


def r_mean(tdf, lo=None, hi=None):
    t = tdf
    if lo is not None:
        t = t[t.entry_time >= lo]
    if hi is not None:
        t = t[t.entry_time < hi]
    return round(t.r_multiple.mean(), 3) if len(t) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_4h.yaml")
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--wide", type=int, default=100)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-age", type=int, default=180)
    ap.add_argument("--side-cap", type=int, default=3)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_config(args.config)
    costs = Costs(**cfg.get("costs", {}))
    client = BinanceFutures()
    symbols = top_symbols(client, args.wide, 5e6, EXCLUDE)
    if "BTCUSDT" not in symbols:
        symbols.append("BTCUSDT")
    data = {}
    for s in symbols:
        df = load_klines(client, s, "4h", args.days)
        if len(df) >= 400:
            data[s] = df
    print("후보 코인 수:", len(data))

    in_univ = universe_mask(data, args.top)
    btc = data["BTCUSDT"]["close"]
    btc_up = btc > ema(btc, 200)                       # 봉 마감 시점의 BTC 추세
    t0 = min(df.index[0] for df in data.values())

    def trades_for(engine: str, use_btc: bool, use_age: bool) -> list:
        p = StrategyParams.from_dict({**cfg.get("strategy", {}), "engine": engine})
        out = []
        for s, df in data.items():
            f = generate_signals(df, p)
            day = f.index.floor("1D")
            ok = in_univ[s].reindex(day).fillna(False).to_numpy(dtype=bool).copy()
            if use_btc:
                up = btc_up.reindex(f.index).ffill().fillna(False).to_numpy(dtype=bool)
                sig = f["signal"].to_numpy()
                ok &= ((sig == 1) & up) | ((sig == -1) & ~up)
            if use_age and df.index[0] > t0 + pd.Timedelta("5D"):   # 기간 중 새로 상장된 코인만 해당
                ok &= np.asarray(f.index >= df.index[0] + pd.Timedelta(days=args.min_age))
            f = f.copy()
            f.loc[~ok, "signal"] = 0
            out += simulate_signals(s, f, p, costs, cooldown_bars=cfg.get("cooldown_bars", 3))
        return out

    variants = [  # (이름, 엔진, 방향제한, BTC추세, 신규제외)
        ("현재 설정 (heikin+pullback)", "heikin,pullback", 0, False, False),
        ("heikin 만", "heikin", 0, False, False),
        ("pullback 만", "pullback", 0, False, False),
        ("현재 + 방향제한", "heikin,pullback", args.side_cap, False, False),
        ("현재 + BTC추세", "heikin,pullback", 0, True, False),
        ("현재 + 신규제외", "heikin,pullback", 0, False, True),
        ("현재 + 방향제한 + BTC추세", "heikin,pullback", args.side_cap, True, False),
        ("현재 + 방향제한 + 신규제외", "heikin,pullback", args.side_cap, False, True),
        ("현재 + 3가지 모두", "heikin,pullback", args.side_cap, True, True),
        ("heikin 만 + 3가지 모두", "heikin", args.side_cap, True, True),
    ]
    cache, rows = {}, []
    for name, eng, cap, use_btc, use_age in variants:
        key = (eng, use_btc, use_age)
        if key not in cache:
            cache[key] = trades_for(eng, use_btc, use_age)
        tdf, curve = portfolio(cache[key], cfg["risk_per_trade"], cfg["leverage"], cfg["max_positions"],
                               max_daily_loss=cfg.get("max_daily_loss", 0) or 0, max_per_side=cap)
        st = summarize(tdf, curve)
        if not st["trades"]:
            continue
        a, b = tdf.entry_time.min(), tdf.entry_time.max()
        cut = a + (b - a) * 0.7
        rows.append({"보완안": name, "거래": st["trades"], "하루": st["trades_per_day"], "승률%": st["win_rate"],
                     "avg_R": st["avg_R"], "PF": st["profit_factor"], "수익%": st["total_return_%"],
                     "MDD%": st["max_drawdown_%"], "R_앞70%": r_mean(tdf, hi=cut), "R_뒤30%": r_mean(tdf, lo=cut),
                     "R_최근60일": r_mean(tdf, lo=b - pd.Timedelta(days=60))})
        print(f"  완료: {name}")
    res = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(f"\n===== 보완안 비교 (4시간봉, 최근 {args.days}일, 시점별 상위 {args.top}개, 수수료 포함) =====")
    print(res.to_string(index=False))
    ok = res[(res["R_앞70%"] > 0) & (res["R_뒤30%"] > 0)]
    print("\n===== 통과: 앞·뒤 기간 모두 플러스 =====")
    print(ok.to_string(index=False) if len(ok) else "없음")


if __name__ == "__main__":
    main()
