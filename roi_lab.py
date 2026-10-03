"""바이낸스 앱 ROI 기준 익절/손절 조합을 3년 데이터로 비교.

  python3 roi_lab.py 2>&1 | tee roi_result.txt

ROI = 가격 변동 × 레버리지. 진입 금액은 기존과 같이 '손절 시 계좌의 0.5%' 기준으로 자동 계산.
"""
import argparse
import itertools
import logging

import pandas as pd

from bot.backtest_engine import Costs, portfolio, simulate_symbol, summarize
from bot.config import load_config
from bot.data import load_klines, top_symbols
from bot.exchange import BinanceFutures
from bot.strategy import StrategyParams

TP_ROI = [0.08, 0.10, 0.15, 0.20, 0.30]
SL_ROI = [0.08, 0.10, 0.15, 0.20]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_4h.yaml")
    ap.add_argument("--days", type=int, default=1095)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = load_config(args.config)
    costs = Costs(**cfg.get("costs", {}))
    lev = float(cfg["leverage"])
    client = BinanceFutures()
    u = cfg["universe"]
    data = {}
    for s in top_symbols(client, u["top_n"], u["min_quote_volume"], u.get("exclude")):
        df = load_klines(client, s, cfg["interval"], args.days)
        if len(df) >= 1000:
            data[s] = df
    print(f"\n{cfg['interval']} / {len(data)}개 코인 / 레버리지 {lev:g}배 (ROI 10% = 가격 {10 / lev:g}%)")

    combos = [("기준(ATR, 현재 설정)", 0, 0)] + [(f"익절 +{t * 100:g}% / 손절 -{s * 100:g}%", t, s)
                                             for t, s in itertools.product(TP_ROI, SL_ROI)]
    rows = []
    for name, tp, sl in combos:
        over = {}
        if tp:
            over["tp_price_pct"] = tp / lev
        if sl:
            over["sl_price_pct"] = sl / lev
        p = StrategyParams.from_dict({**cfg.get("strategy", {}), **over})
        trades = [t for s, df in data.items()
                  for t in simulate_symbol(s, df, p, costs, cooldown_bars=cfg.get("cooldown_bars", 3))]
        tdf, curve = portfolio(trades, cfg["risk_per_trade"], lev, cfg["max_positions"],
                               max_daily_loss=cfg["max_daily_loss"])
        st = summarize(tdf, curve)
        t0, t1 = tdf.entry_time.min(), tdf.entry_time.max()
        cut = t0 + (t1 - t0) * 0.7
        rows.append({
            "ROI 익절/손절": name, "거래": st["trades"], "하루": st["trades_per_day"], "승률%": st["win_rate"],
            "수익%": st["total_return_%"], "MDD%": st["max_drawdown_%"],
            "R_앞70%": round(tdf[tdf.entry_time < cut].r_multiple.mean(), 3),
            "R_뒤30%": round(tdf[tdf.entry_time >= cut].r_multiple.mean(), 3),
        })
        print(f"  완료: {name}")

    res = pd.DataFrame(rows).sort_values("수익%", ascending=False)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 100)
    print("\n===== ROI 기준 익절/손절 비교 (수익 순, 수수료 포함, 3년) =====")
    print(res.to_string(index=False))


if __name__ == "__main__":
    main()
