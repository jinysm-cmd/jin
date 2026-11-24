"""조용한 강세주 로테이션 검증.

python krx_rotation.py              # 벤치마크 비교 + 설정 민감도 + 요소 제거 실험 + 연도별 수익
"""
import argparse
import itertools
from dataclasses import replace

import numpy as np
import pandas as pd

from krx import rotation_bt as rb
from krx.data import fetch_daily, list_stocks, load_all
from krx.engine import stats

PERIODS = {"IS 16~21": ("2016-01-01", "2021-12-31"), "OOS 22~": ("2022-01-01", "2100-01-01")}


def line(s):
    return (f"CAGR {s['cagr']:+6.1%}  MDD {s['mdd']:6.1%}  샤프 {s['sharpe']:4.2f}"
            + (f"  회전율 {s['turnover_yr']:4.1f}x/년" if "turnover_yr" in s else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--rebal", type=int, default=20)
    ap.add_argument("--buffer", type=int, default=10)
    args = ap.parse_args()

    uni = list_stocks()
    data = {k: v for k, v in load_all(list(uni["code"])).items() if len(v) > 0}
    o, h, c, v = rb.wide(data)
    print(f"종목 {c.shape[1]}개, {c.index[0].date()} ~ {c.index[-1].date()}\n")
    base = rb.RotCfg(top_n=args.top_n, rebal=args.rebal, buffer=args.buffer)

    # ---- 벤치마크 ----
    print("== 벤치마크 ==")
    idx = {s: fetch_daily(s)["close"] for s in ("KOSPI", "KOSDAQ")}
    tv20 = (c * v).rolling(20).mean()
    elig = (tv20 >= base.min_tv20) & (c >= base.min_price) & (c.notna().cumsum() >= 250)
    ew_ret = c.pct_change(fill_method=None).where(elig.shift(1)).mean(axis=1).fillna(0)
    ew = (1 + ew_ret).cumprod()
    for name, (a, b) in PERIODS.items():
        for s, ser in idx.items():
            print(f"{name:9s} {s:24s} {line(stats(ser[a:b], pd.DataFrame()))}")
        print(f"{name:9s} {'전체종목 동일비중(비용X)':20s} {line(stats(ew[a:b], pd.DataFrame()))}")
    print()

    def evaluate(cfg):
        sc = rb.score_panel(o, h, c, v, cfg)
        return {n: rb.run(o, c, sc, replace(cfg, start=a, end=b))[1] for n, (a, b) in PERIODS.items()}

    # ---- 기본 설정 ----
    print(f"== 기본 설정 (상위 {base.top_n}종목, {base.rebal}거래일마다 교체, 버퍼 {base.buffer}) ==")
    for n, s in evaluate(base).items():
        print(f"{n:9s} {line(s)}")
    print()

    # ---- 요소 제거 ----
    print("== 요소 하나씩 빼기 (어떤 요소가 실제로 기여하나) ==")
    for w in ("w_high", "w_mom", "w_spike", "w_ret5", "w_vol"):
        r = evaluate(replace(base, **{w: 0.0}))
        print(f"{w[2:]:6s} 제거  IS {line(r['IS 16~21'])}  |  OOS {line(r['OOS 22~'])}")
    print()

    # ---- 민감도 ----
    print("== 설정 민감도 ==")
    rows = []
    for top_n, rebal, buf in itertools.product((10, 20, 30), (10, 20, 40, 60), (0, 10)):
        r = evaluate(replace(base, top_n=top_n, rebal=rebal, buffer=buf))
        rows.append({"top_n": top_n, "rebal": rebal, "buffer": buf,
                     **{f"{k}_{m}": r[k][m] for k in r for m in ("cagr", "mdd", "sharpe")}})
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(df.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print(f"\nIS 샤프 1위 설정의 OOS 샤프: {df.loc[df['IS 16~21_sharpe'].idxmax(), 'OOS 22~_sharpe']:.2f}"
          f"  / 전체 평균 OOS 샤프: {df['OOS 22~_sharpe'].mean():.2f}")
    df.to_csv("krx_rotation_grid.csv", index=False)
    print()

    # ---- 연도별 ----
    sc = rb.score_panel(o, h, c, v, base)
    eq, _ = rb.run(o, c, sc, base)
    yr = pd.DataFrame({
        "전략": eq.resample("YE").last().pct_change(),
        "동일비중": ew.resample("YE").last().pct_change(),
        "KOSPI": idx["KOSPI"].resample("YE").last().pct_change(),
        "KOSDAQ": idx["KOSDAQ"].resample("YE").last().pct_change(),
    })
    first = eq.index[0].year
    yr.loc[pd.Timestamp(f"{first}-12-31"), "전략"] = eq[str(first)].iloc[-1] / eq.iloc[0] - 1
    yr.index = yr.index.year
    print("== 연도별 수익률 (기본 설정) ==")
    print((yr * 100).round(1).to_string())


if __name__ == "__main__":
    main()
