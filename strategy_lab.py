"""대표 지표 전략 5종을 여러 타임프레임에서 한 번에 비교 검증.

  python3 strategy_lab.py 2>&1 | tee lab_result.txt
  python3 strategy_lab.py --days 365 --top 30 --tf 15m,1h,4h

모든 전략은 교과서 기본값 그대로(튜닝 없음), 수수료 0.05% + 슬리피지 0.02% 반영.
앞 70% / 뒤 30% 기간의 avg_R 이 둘 다 플러스여야 의미가 있다.
"""
import argparse
import logging

import pandas as pd

from bot.backtest_engine import Costs, portfolio, simulate_symbol, summarize
from bot.classic import ENGINES
from bot.config import load_config
from bot.data import load_klines, top_symbols
from bot.exchange import BinanceFutures
from bot.strategy import StrategyParams


def split_r(tdf, frac=0.7):
    t0, t1 = tdf.entry_time.min(), tdf.entry_time.max()
    cut = t0 + (t1 - t0) * frac
    a, b = tdf[tdf.entry_time < cut], tdf[tdf.entry_time >= cut]
    return (round(a.r_multiple.mean(), 3) if len(a) else None,
            round(b.r_multiple.mean(), 3) if len(b) else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--tf", default="15m,1h,4h")
    ap.add_argument("--engines", default=",".join(ENGINES))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = load_config()
    costs = Costs(**cfg.get("costs", {}))
    client = BinanceFutures()
    u = cfg["universe"]
    symbols = top_symbols(client, args.top, u["min_quote_volume"], u.get("exclude"))
    print("심볼:", ", ".join(symbols))

    rows = []
    for tf in args.tf.split(","):
        data = {}
        for s in symbols:
            df = load_klines(client, s, tf, args.days)
            if len(df) >= 400:
                data[s] = df
        for eng in args.engines.split(","):
            p = StrategyParams.from_dict({**cfg.get("strategy", {}), "engine": eng, "trend_ema_len": 0})
            trades = []
            for s, df in data.items():
                trades += simulate_symbol(s, df, p, costs, cooldown_bars=cfg.get("cooldown_bars", 3))
            tdf, curve = portfolio(trades, cfg["risk_per_trade"], cfg["leverage"], cfg["max_positions"])
            st = summarize(tdf, curve)
            if st["trades"] == 0:
                continue
            r_is, r_oos = split_r(tdf)
            rows.append({
                "tf": tf, "strategy": eng, "trades": st["trades"], "per_day": st["trades_per_day"],
                "win%": st["win_rate"], "avg_R": st["avg_R"], "PF": st["profit_factor"],
                "return%": st["total_return_%"], "MDD%": st["max_drawdown_%"],
                "R_앞70%": r_is, "R_뒤30%": r_oos,
            })
            print(f"  완료: {tf} {eng}  거래 {st['trades']}  avg_R {st['avg_R']}")

    res = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print("\n===== 전략 비교 (수수료·슬리피지 포함, 1년) =====")
    print(res.to_string(index=False))
    ok = res[(res["R_앞70%"] > 0) & (res["R_뒤30%"] > 0) & (res["trades"] >= 100)]
    print("\n===== 통과: 앞·뒤 기간 모두 플러스 & 거래 100회 이상 =====")
    print(ok.to_string(index=False) if len(ok) else "없음")
    res.to_csv("lab_result.csv", index=False)


if __name__ == "__main__":
    main()
