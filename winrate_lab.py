"""승률을 높이는 방법들을 같은 조건(4시간봉, 3년, 상위 30개)에서 한 번에 비교.

  python3 winrate_lab.py --config config_4h.yaml 2>&1 | tee winrate_result.txt

승률이 올라가도 '평균 R(거래당 기대값)'과 '최대 낙폭'이 나빠지면 의미가 없다.
앞 70% / 뒤 30% 기간 모두 플러스인지도 함께 본다.
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
    "익절 가깝게 x0.7": {"tp_mult": 0.7, "min_rr": 0.5},
    "익절 가깝게 x0.5": {"tp_mult": 0.5, "min_rr": 0.5},
    "손절 넓게 x1.5": {"sl_mult": 1.5},
    "본전손절 +1R": {"breakeven_r": 1.0},
    "본전손절 +0.7R": {"breakeven_r": 0.7},
    "ADX>20 추세장만": {"adx_min": 20},
    "ADX>25 추세장만": {"adx_min": 25},
    "ADX>20 + 본전손절 +1R": {"adx_min": 20, "breakeven_r": 1.0},
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
    symbols = top_symbols(client, u["top_n"], u["min_quote_volume"], u.get("exclude"))
    data = {}
    for s in symbols:
        df = load_klines(client, s, cfg["interval"], args.days)
        if len(df) >= 1000:
            data[s] = df
    print(f"\n{cfg['interval']} / {len(data)}개 코인 / 전략={cfg['strategy'].get('engine')}")

    rows = []
    for name, over in VARIANTS.items():
        p = StrategyParams.from_dict({**cfg.get("strategy", {}), **over})
        trades = [t for s, df in data.items()
                  for t in simulate_symbol(s, df, p, costs, cooldown_bars=cfg.get("cooldown_bars", 3))]
        tdf, curve = portfolio(trades, cfg["risk_per_trade"], cfg["leverage"], cfg["max_positions"])
        st = summarize(tdf, curve)
        t0, t1 = tdf.entry_time.min(), tdf.entry_time.max()
        cut = t0 + (t1 - t0) * 0.7
        rows.append({
            "방법": name, "거래": st["trades"], "하루": st["trades_per_day"], "승률%": st["win_rate"],
            "avg_R": st["avg_R"], "PF": st["profit_factor"], "수익%": st["total_return_%"],
            "MDD%": st["max_drawdown_%"],
            "R_앞70%": round(tdf[tdf.entry_time < cut].r_multiple.mean(), 3),
            "R_뒤30%": round(tdf[tdf.entry_time >= cut].r_multiple.mean(), 3),
        })
        print(f"  완료: {name}")

    res = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print("\n===== 승률 개선 실험 (수수료 포함) =====")
    print(res.to_string(index=False))
    res.to_csv("winrate_result.csv", index=False)


if __name__ == "__main__":
    main()
