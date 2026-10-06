"""PDF '하이킨아시 다중 시간프레임 전략' 백테스트 (1H 추세 → 15M 눌림 → 5M 진입, 부분익절 + 추적청산).

  python3 mtf_lab.py 2>&1 | tee mtf_result.txt
  python3 mtf_lab.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --days 365

5분봉 1년치를 받는다 (처음 실행 시 코인당 1~2분).
수수료 2가지 경우로 비교: 시장가(0.05%+슬리피지 0.02%) / 지정가 위주(0.02%).
"""
import argparse
import logging

import pandas as pd

from bot import mtf_heikin as m
from bot.backtest_engine import Costs, portfolio, summarize
from bot.config import load_config
from bot.data import load_klines
from bot.exchange import BinanceFutures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_4h.yaml")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_config(args.config)
    client = BinanceFutures()
    data = {s: load_klines(client, s, "5m", args.days) for s in args.symbols.split(",")}

    rows = []
    for cname, costs in (("시장가 수수료", Costs(0.0005, 0.0002)), ("지정가 수수료", Costs(0.0002, 0.0))):
        p = m.MTFParams()
        trades = [t for s, df in data.items() for t in m.simulate(s, df, p, costs)]
        tdf, curve = portfolio(trades, cfg["risk_per_trade"], cfg["leverage"], cfg["max_positions"],
                               max_daily_loss=cfg["max_daily_loss"])
        st = summarize(tdf, curve)
        if st["trades"] == 0:
            print(f"{cname}: 거래 없음")
            continue
        t0, t1 = tdf.entry_time.min(), tdf.entry_time.max()
        cut = t0 + (t1 - t0) * 0.7
        rc = tdf.reason.value_counts(normalize=True) * 100
        rows.append({
            "수수료": cname, "거래": st["trades"], "하루": st["trades_per_day"], "승률%": st["win_rate"],
            "TP1도달%": round(rc.get("TP1", 0) + rc.get("TP2", 0), 1), "TP2도달%": round(rc.get("TP2", 0), 1),
            "avg_R": st["avg_R"], "PF": st["profit_factor"], "수익%": st["total_return_%"],
            "MDD%": st["max_drawdown_%"],
            "R_앞70%": round(tdf[tdf.entry_time < cut].r_multiple.mean(), 3),
            "R_뒤30%": round(tdf[tdf.entry_time >= cut].r_multiple.mean(), 3),
        })
        if cname == "시장가 수수료":
            by = tdf.groupby(["symbol", "side"]).agg(거래=("pnl", "size"),
                                                    승률=("net_ret", lambda x: round((x > 0).mean() * 100, 1)),
                                                    avg_R=("r_multiple", lambda x: round(x.mean(), 3)))
            print("\n[코인/방향별 (시장가 수수료)]  side 1=롱, -1=숏")
            print(by.to_string())

    pd.set_option("display.width", 220)
    print(f"\n===== PDF 하이킨아시 MTF 전략 ({args.symbols}, {args.days}일) =====")
    print(pd.DataFrame(rows).to_string(index=False))
    print("\n비교용 현재 봇(4H 하이킨+눌림목): 3년 수익 +37~104%, 최대낙폭 -22~31%, 승률 약 40%")


if __name__ == "__main__":
    main()
