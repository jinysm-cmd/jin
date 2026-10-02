"""짧은 고정 익절 + 하루 목표 수익 달성 시 중단 규칙 검증.

  python3 tp_lab.py 2>&1 | tee tp_result.txt

ROI 는 바이낸스 앱 표시 기준(증거금 대비)이라 가격 변동 = ROI / 레버리지.
모든 방법에 실거래와 같은 '일일 손실 3% 도달 시 중단' 규칙을 똑같이 적용해 비교한다.
"""
import argparse
import logging

import pandas as pd

from bot.backtest_engine import Costs, portfolio, simulate_symbol, summarize
from bot.config import load_config
from bot.data import load_klines, top_symbols
from bot.exchange import BinanceFutures
from bot.strategy import StrategyParams


def variants(lev: float):
    """이름: (전략 덮어쓰기, 하루 목표 중단, 고정 증거금 비율, 최대 포지션 수)"""
    roi = lambda r: r / lev  # ROI → 가격 변동
    return {
        "기준(현재 설정)": ({}, 0, 0, None),
        "기준 + 하루 +3% 달성 시 중단": ({}, 0.03, 0, None),
        f"익절 ROI 3% (가격 {roi(0.03) * 100:.1f}%)": ({"tp_price_pct": roi(0.03)}, 0, 0, None),
        f"익절 ROI 4% (가격 {roi(0.04) * 100:.1f}%)": ({"tp_price_pct": roi(0.04)}, 0, 0, None),
        "익절 ROI 4% + 하루 +3% 중단": ({"tp_price_pct": roi(0.04)}, 0.03, 0, None),
        "익절 가격 3%": ({"tp_price_pct": 0.03}, 0, 0, None),
        "익절 가격 4%": ({"tp_price_pct": 0.04}, 0, 0, None),
        "익절 가격 4% + 하루 +3% 중단": ({"tp_price_pct": 0.04}, 0.03, 0, None),
        "[요청안] 증거금10%x5종목 + ROI4% 익절": ({"tp_price_pct": roi(0.04)}, 0, 0.10, 5),
        "[요청안] + 하루 +3% 중단": ({"tp_price_pct": roi(0.04)}, 0.03, 0.10, 5),
        "증거금10%x5종목 + 기본 익절": ({}, 0, 0.10, 5),
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
    lev = float(cfg["leverage"])
    print(f"\n{cfg['interval']} / {len(data)}개 코인 / 레버리지 {lev:g}배 / 모든 방법에 일일 손실 3% 중단 적용")

    rows = []
    for name, (over, profit_stop, margin, max_pos) in variants(lev).items():
        p = StrategyParams.from_dict({**cfg.get("strategy", {}), **over})
        trades = [t for s, df in data.items()
                  for t in simulate_symbol(s, df, p, costs, cooldown_bars=cfg.get("cooldown_bars", 3))]
        tdf, curve = portfolio(trades, cfg["risk_per_trade"], lev, max_pos or cfg["max_positions"],
                               max_daily_loss=cfg["max_daily_loss"], daily_profit_stop=profit_stop,
                               margin_per_trade=margin)
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

    pd.set_option("display.width", 220)
    print("\n===== 짧은 익절 / 하루 목표 중단 실험 (수수료 포함, 3년) =====")
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
