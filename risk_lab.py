"""거래당 위험(risk_per_trade)별 3년 성과 비교. 전략·신호는 그대로, 진입 금액만 다르게.

  python3 risk_lab.py 2>&1 | tee risk_result.txt
"""
import argparse
import logging

import pandas as pd

from bot.backtest_engine import Costs, portfolio, simulate_symbol, summarize
from bot.config import load_config
from bot.data import load_klines, top_symbols
from bot.exchange import BinanceFutures
from bot.strategy import StrategyParams

RISKS = [0.005, 0.0075, 0.01, 0.015]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_4h.yaml")
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--equity", type=float, default=352, help="현재 잔고(USDT), 금액 환산용")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = load_config(args.config)
    costs = Costs(**cfg.get("costs", {}))
    client = BinanceFutures()
    u = cfg["universe"]
    data = {}
    for s in top_symbols(client, u["top_n"], u["min_quote_volume"], u.get("exclude")):
        df = load_klines(client, s, cfg["interval"], args.days)
        if len(df) >= 1000:
            data[s] = df
    p = StrategyParams.from_dict(cfg.get("strategy", {}))
    trades = [t for s, df in data.items()
              for t in simulate_symbol(s, df, p, costs, cooldown_bars=cfg.get("cooldown_bars", 3))]

    rows = []
    for r in RISKS:
        tdf, curve = portfolio(trades, r, cfg["leverage"], cfg["max_positions"],
                               max_daily_loss=cfg["max_daily_loss"])
        st = summarize(tdf, curve)
        years = st["days"] / 365
        cagr = ((1 + st["total_return_%"] / 100) ** (1 / years) - 1) * 100 if years > 0 else 0
        worst = curve.pct_change().min() * 100 if len(curve) > 1 else 0
        rows.append({
            "거래당위험": f"{r * 100:g}%",
            "손절1번(USDT)": round(args.equity * r, 2),
            "거래": st["trades"], "승률%": st["win_rate"],
            "3년수익%": st["total_return_%"], "연평균%": round(cagr, 1),
            "최대낙폭%": st["max_drawdown_%"],
            f"낙폭금액(잔고{args.equity:g})": round(args.equity * st["max_drawdown_%"] / 100, 1),
            "최악1회%": round(worst, 2),
        })

    pd.set_option("display.width", 220)
    print(f"\n===== 거래당 위험별 비교 ({cfg['interval']}, {len(data)}개 코인, 일일손실 {cfg['max_daily_loss'] * 100:g}% 중단 포함) =====")
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
