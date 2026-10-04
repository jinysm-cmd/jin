"""저항(최근 고점) 고려 익절 검증.

  python3 resistance_lab.py 2>&1 | tee resistance_result.txt

'익절가가 최근 N일 고점보다 위면 고점까지로 낮추고, 그러면 손익비가 1 미만인 거래는 건너뛰기' 등을
현재 설정과 같은 3년 데이터로 비교한다. (4시간봉 기준 6봉 = 1일)
"""
import argparse
import logging

import pandas as pd

from bot.backtest_engine import Costs, portfolio, simulate_symbol, summarize
from bot.config import load_config
from bot.data import load_klines, top_symbols
from bot.exchange import BinanceFutures
from bot.strategy import StrategyParams

VARIANTS = {
    "기준(현재 설정)": {},
    "최근 10일 고점까지 익절 (손익비<1 건너뜀)": {"tp_cap_bars": 60},
    "최근 20일 고점까지 익절 (손익비<1 건너뜀)": {"tp_cap_bars": 120},
    "최근 30일 고점까지 익절 (손익비<1 건너뜀)": {"tp_cap_bars": 180},
    "최근 60일 고점까지 익절 (손익비<1 건너뜀)": {"tp_cap_bars": 360},
    "최근 20일 고점까지 익절 (진입은 유지)": {"tp_cap_bars": 120, "tp_cap_skip": False},
    "최근 30일 고점, 손익비<1.5 건너뜀": {"tp_cap_bars": 180, "tp_cap_min_rr": 1.5},
    "보유기간 3일로 단축": {"max_hold_bars": 18},
    "보유기간 10일로 연장": {"max_hold_bars": 60},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_4h.yaml")
    ap.add_argument("--days", type=int, default=1095)
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
    print(f"\n{cfg['interval']} / {len(data)}개 코인")

    rows = []
    for name, over in VARIANTS.items():
        p = StrategyParams.from_dict({**cfg.get("strategy", {}), **over})
        trades = [t for s, df in data.items()
                  for t in simulate_symbol(s, df, p, costs, cooldown_bars=cfg.get("cooldown_bars", 3))]
        tdf, curve = portfolio(trades, cfg["risk_per_trade"], cfg["leverage"], cfg["max_positions"],
                               max_daily_loss=cfg["max_daily_loss"])
        st = summarize(tdf, curve)
        t0, t1 = tdf.entry_time.min(), tdf.entry_time.max()
        cut = t0 + (t1 - t0) * 0.7
        reasons = tdf.reason.value_counts(normalize=True) * 100
        rows.append({
            "방법": name, "거래": st["trades"], "하루": st["trades_per_day"], "승률%": st["win_rate"],
            "익절%": round(reasons.get("TP", 0), 1), "시간청산%": round(reasons.get("TIME", 0), 1),
            "수익%": st["total_return_%"], "MDD%": st["max_drawdown_%"],
            "R_앞70%": round(tdf[tdf.entry_time < cut].r_multiple.mean(), 3),
            "R_뒤30%": round(tdf[tdf.entry_time >= cut].r_multiple.mean(), 3),
        })
        print(f"  완료: {name}")

    pd.set_option("display.width", 220)
    print("\n===== 저항 고려 익절 비교 (수수료 포함, 3년) =====")
    print(pd.DataFrame(rows).to_string(index=False))
    print("\n익절% = 익절가에 도달해 청산된 비율, 시간청산% = 6일(또는 설정 기간) 지나 정리된 비율")


if __name__ == "__main__":
    main()
