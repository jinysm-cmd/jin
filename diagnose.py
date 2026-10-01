"""전략 진단: 왜 손실인지, 어디에 엣지가 있는지 확인한다.

backtest.py 를 한 번 돌려서 data/ 폴더에 5분봉 캐시가 생긴 뒤에 실행 (네트워크 불필요).

  python3 diagnose.py 2>&1 | tee diagnose_result.txt

각 [타임프레임 x 셋업 x 방향(정방향/역방향)] 조합별로
  - 수수료 전 평균 R (gross_R): 신호 자체에 방향성 엣지가 있는가?
  - 테이커 수수료 후 평균 R (taker_R): 시장가로 매매하면 남는가?
  - 메이커 수수료 후 평균 R (maker_R): 지정가(수수료 0.02%) 진입·청산이면 남는가?
를 앞 70%(IS) / 뒤 30%(OOS) 구간 따로 보여준다. 두 구간 모두 플러스여야 의미가 있다.
"""
import glob
import itertools
import os

import numpy as np
import pandas as pd

from bot.backtest_engine import Costs, simulate_symbol
from bot.config import load_config
from bot.data import read_cache, resample
from bot.strategy import StrategyParams

TIMEFRAMES = {"5m": None, "15m": "15min", "1h": "1h"}
SETUPS = ["ABSORB", "TRAP", "SQUEEZE"]
EMA_FILTERS = [0, 200]  # 0=필터 없음, 200=EMA200 추세 방향만
TAKER_RT = 2 * (0.0005 + 0.0002)
MAKER_RT = 2 * 0.0002


def stats(trades):
    if not trades:
        return None
    g = np.array([t.gross_ret for t in trades])
    risk = np.array([t.sl_dist / t.entry for t in trades])
    gross_r = g / risk
    taker = (g - TAKER_RT) / risk
    maker = (g - MAKER_RT) / risk
    return {
        "n": len(trades),
        "win%": round((g - TAKER_RT > 0).mean() * 100, 1),
        "gross_R": round(gross_r.mean(), 3),
        "taker_R": round(taker.mean(), 3),
        "maker_R": round(maker.mean(), 3),
        "avg_sl%": round(risk.mean() * 100, 2),
    }


def main():
    cfg = load_config()
    base = cfg.get("strategy", {})
    files = sorted(glob.glob("data/*_5m.csv"))
    symbols = [os.path.basename(f)[:-7] for f in files if os.path.basename(f)[:-7].isascii()]
    if not symbols:
        raise SystemExit("data/ 폴더에 5분봉 캐시가 없습니다. 먼저 python backtest.py --days 120 을 실행하세요.")
    print("심볼:", ", ".join(symbols))
    raw = {s: read_cache(s, "5m") for s in symbols}

    rows = []
    for tf, rule in TIMEFRAMES.items():
        data = {s: (df if rule is None else resample(df, rule)) for s, df in raw.items()}
        for setup, ema in itertools.product(SETUPS, EMA_FILTERS):
            p = StrategyParams.from_dict({**base, "trend_ema_len": ema,
                                          **{f"{x.lower()}_enabled": x == setup for x in SETUPS}})
            flt = f"EMA{ema}" if ema else "-"
            for flip in (False, True):
                is_tr, oos_tr = [], []
                for s, df in data.items():
                    cut = df.index[int(len(df) * 0.7)]
                    for t in simulate_symbol(s, df, p, Costs(0, 0), cooldown_bars=cfg.get("cooldown_bars", 3), flip=flip):
                        (is_tr if t.entry_time < cut else oos_tr).append(t)
                for part, tr in (("IS", is_tr), ("OOS", oos_tr)):
                    st = stats(tr)
                    if st:
                        rows.append({"tf": tf, "setup": setup, "filter": flt, "dir": "역방향" if flip else "정방향", "part": part, **st})
                print(f"  완료: {tf} {setup} {flt} {'역방향' if flip else '정방향'}")

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 500)
    print("\n===== 전체 결과 =====")
    print(df.to_string(index=False))

    piv = df.pivot_table(index=["tf", "setup", "filter", "dir"], columns="part", values=["taker_R", "maker_R", "n"])
    good = piv[(piv[("maker_R", "IS")] > 0) & (piv[("maker_R", "OOS")] > 0)]
    print("\n===== IS·OOS 모두 메이커 기준 플러스인 조합 =====")
    print(good.to_string() if len(good) else "없음 — 이 신호들로는 수수료를 이길 엣지가 확인되지 않음")
    df.to_csv("diagnose_result.csv", index=False)


if __name__ == "__main__":
    main()
